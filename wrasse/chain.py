"""Sending a transaction at most once, and finding out what happened to it.

The failure this module exists to prevent: broadcast `createDeal`, the RPC times out before
returning a receipt, a retry builds a *second* transaction, and two funded offers now exist
for one deal. Everything here follows from that.

Two rules carry the design.

**Action identity never comes from the terms.** `acceptBy` is re-derived from chain time
immediately before signing, so the policy hash differs on every attempt. An idempotency key
built from it would call a retry a new deal. The caller supplies a stable `intent_id`, minted
before any of this runs.

**A row is a claim about some bytes; the bytes are the authority.** Nothing is ever rebuilt
for an existing intent. A transaction is resolved by querying the hash it was recorded under,
and the only thing that may be sent again is the identical raw payload.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from eth_account import Account
from web3 import Web3
from web3.exceptions import BlockNotFound, TransactionNotFound


# --------------------------------------------------------------------------------------
# Status model
# --------------------------------------------------------------------------------------

SIGNED = "signed"
SEND_ATTEMPTED = "send_attempted"
PENDING = "pending"
INCLUDED_SUCCESS = "included_success"
INCLUDED_REVERTED = "included_reverted"
CONFIRMED_SUCCESS = "confirmed_success"
CONFIRMED_REVERTED = "confirmed_reverted"
REORGED = "reorged"
NONCE_CONFLICT_PENDING = "nonce_conflict_pending"
NONCE_CONSUMED_OR_REPLACED = "nonce_consumed_or_replaced"
STUCK = "stuck"
REJECTED = "rejected"

ALL_STATUSES = (
    SIGNED,
    SEND_ATTEMPTED,
    PENDING,
    INCLUDED_SUCCESS,
    INCLUDED_REVERTED,
    CONFIRMED_SUCCESS,
    CONFIRMED_REVERTED,
    REORGED,
    NONCE_CONFLICT_PENDING,
    NONCE_CONSUMED_OR_REPLACED,
    STUCK,
    REJECTED,
)

#: Nothing further will happen to a row in one of these states.
#:
#: `stuck` and `nonce_conflict_pending` are deliberately NOT terminal. Both mean "we do not
#: know whether this nonce is spent", and allowing the wallet to move on would open a nonce
#: gap that silently strands every later transaction. Clearing either is an operator decision.
TERMINAL_STATUSES = frozenset(
    {CONFIRMED_SUCCESS, CONFIRMED_REVERTED, NONCE_CONSUMED_OR_REPLACED, REJECTED}
)

#: Transitions are an explicit graph, not a monotonic ladder: a reorg moves a row backwards
#: from included to unmined, which is the whole reason inclusion and confirmation are
#: different states.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    SIGNED: frozenset({SEND_ATTEMPTED, STUCK, REJECTED}),
    SEND_ATTEMPTED: frozenset(
        {
            SEND_ATTEMPTED,
            PENDING,
            INCLUDED_SUCCESS,
            INCLUDED_REVERTED,
            NONCE_CONFLICT_PENDING,
            NONCE_CONSUMED_OR_REPLACED,
            STUCK,
            REJECTED,
        }
    ),
    PENDING: frozenset(
        {
            SEND_ATTEMPTED,
            PENDING,
            INCLUDED_SUCCESS,
            INCLUDED_REVERTED,
            NONCE_CONFLICT_PENDING,
            NONCE_CONSUMED_OR_REPLACED,
            STUCK,
        }
    ),
    INCLUDED_SUCCESS: frozenset({INCLUDED_SUCCESS, CONFIRMED_SUCCESS, REORGED}),
    INCLUDED_REVERTED: frozenset({INCLUDED_REVERTED, CONFIRMED_REVERTED, REORGED}),
    REORGED: frozenset(
        {
            SEND_ATTEMPTED,
            PENDING,
            INCLUDED_SUCCESS,
            INCLUDED_REVERTED,
            NONCE_CONSUMED_OR_REPLACED,
            STUCK,
        }
    ),
    NONCE_CONFLICT_PENDING: frozenset(
        {
            SEND_ATTEMPTED,
            PENDING,
            INCLUDED_SUCCESS,
            INCLUDED_REVERTED,
            NONCE_CONFLICT_PENDING,
            NONCE_CONSUMED_OR_REPLACED,
            STUCK,
        }
    ),
    STUCK: frozenset(
        {
            SEND_ATTEMPTED,
            PENDING,
            INCLUDED_SUCCESS,
            INCLUDED_REVERTED,
            NONCE_CONSUMED_OR_REPLACED,
            STUCK,
        }
    ),
    CONFIRMED_SUCCESS: frozenset(),
    CONFIRMED_REVERTED: frozenset(),
    NONCE_CONSUMED_OR_REPLACED: frozenset(),
    REJECTED: frozenset(),
}


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class LedgerError(RuntimeError):
    """The transaction ledger refused an operation."""


class WalletBusy(LedgerError):
    """This wallet already has a transaction that has not reached a terminal state.

    Sending anyway would either reuse a nonce or skip one. A skipped nonce strands every
    later transaction behind it, so the honest answer is to refuse and say which row is in
    the way.
    """


class LedgerCorrupt(LedgerError):
    """A stored row disagrees with the signed bytes it claims to describe."""


class IllegalTransition(LedgerError):
    """A status change that the state graph does not permit."""


class BroadcastNotAuthorised(RuntimeError):
    """Broadcasting was attempted without the deliberate opt-in.

    The barrier is at the send rather than at the signature because a signed payload already
    moves funds. Nobody needs the key again to replay it.
    """


class RpcUnavailable(RuntimeError):
    """A query failed. This is not the same as a query returning nothing."""


class SafeHeadUnavailable(RuntimeError):
    """The node cannot report a safe head, so nothing here can be called confirmed.

    Substituting the latest block would turn a confirmation rule into decoration.
    """


class DeterministicRejection(RuntimeError):
    """The node rejected this transaction for a reason that will not change on a retry."""


# --------------------------------------------------------------------------------------
# The deliberate crash simulation
# --------------------------------------------------------------------------------------

FAILPOINT_ENV = "WRASSE_FAILPOINT"

#: Deliberately awkward. A failpoint that could be armed by a value someone left in a dotfile
#: would eventually fire during a real run.
CRASH_AFTER_SEND = "crash-after-send-i-mean-it"

_armed_failpoint: str | None = None


def arm_failpoint(value: str | None) -> None:
    """Arm the crash simulation from a value read *before* any dotenv file was loaded.

    The caller passes the raw environment value it saw at startup, so a persistent `.env`
    cannot arm this. Arming is announced loudly, because a run that exits halfway on purpose
    must never be mistaken for a run that failed.
    """

    global _armed_failpoint
    _armed_failpoint = value or None
    if _armed_failpoint:
        print(
            f"!! failpoint armed: {_armed_failpoint} — this process will exit on purpose",
            file=sys.stderr,
        )


def _trip_failpoint(name: str) -> None:
    if _armed_failpoint == name:
        print(f"!! failpoint {name} tripped, exiting deliberately", file=sys.stderr)
        raise SystemExit(97)


# --------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SignedIntent:
    """Everything the ledger needs to describe one signed, unsent transaction."""

    nonce: int
    calldata: str
    value_wei: int
    max_fee_wei: int
    max_priority_wei: int
    gas_limit: int
    accept_by: int
    preimage: dict[str, Any]
    tx_hash: str
    raw: str


@dataclass(frozen=True)
class LedgerRow:
    chain_id: int
    wallet: str
    contract_address: str
    intent_id: str
    nonce: int
    calldata: str
    value_wei: int
    max_fee_wei: int
    max_priority_wei: int
    gas_limit: int
    accept_by: int
    preimage: dict[str, Any]
    tx_hash: str
    raw: str
    status: str
    block_number: int | None
    block_hash: str | None
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def canonical_address(value: str) -> str:
    """One spelling per address before it is ever used as a key.

    Checksum casing is cosmetic. Letting it through would let the same wallet own two rows
    for one nonce, which is exactly the uniqueness the ledger is here to enforce.
    """

    return Web3.to_checksum_address(value).lower()


def _now() -> str:
    return datetime.now(UTC).isoformat()


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS transactions (
    chain_id         INTEGER NOT NULL,
    wallet           TEXT    NOT NULL,
    contract_address TEXT    NOT NULL,
    intent_id        TEXT    NOT NULL,
    nonce            INTEGER NOT NULL,
    calldata         TEXT    NOT NULL,
    value_wei        TEXT    NOT NULL,
    max_fee_wei      TEXT    NOT NULL,
    max_priority_wei TEXT    NOT NULL,
    gas_limit        INTEGER NOT NULL,
    accept_by        INTEGER NOT NULL,
    preimage         TEXT    NOT NULL,
    tx_hash          TEXT    NOT NULL,
    raw              TEXT    NOT NULL,
    status           TEXT    NOT NULL CHECK (status IN ({','.join(repr(s) for s in ALL_STATUSES)})),
    block_number     INTEGER,
    block_hash       TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    PRIMARY KEY (chain_id, wallet, contract_address, intent_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS tx_nonce_unique ON transactions(chain_id, wallet, nonce);
CREATE UNIQUE INDEX IF NOT EXISTS tx_hash_unique  ON transactions(chain_id, tx_hash);
"""


class TransactionLedger:
    """Durable record of what has been signed, and of what became of it.

    Kept deliberately separate from the Sibyl store. Sibyl holds the agent's memory, where
    the rule is that ontology persists and statistics never do; broadcast bookkeeping is
    neither. This file also holds signed payloads that can move funds on their own, which is
    its own reason to keep it out of the memory database and behind restrictive permissions.
    """

    #: The write lock is held across a chain-time read, so contention is expected rather than
    #: exceptional. Waiting is correct; failing immediately is not.
    BUSY_TIMEOUT_MS = 15_000

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, stat.S_IRWXU)
        fresh = not self.path.exists()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
        if fresh:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
            yield connection
        finally:
            connection.close()

    # -- reads ---------------------------------------------------------------------------

    def find(self, *, chain_id: int, wallet: str, contract_address: str, intent_id: str) -> LedgerRow | None:
        with self._connect() as connection:
            return self._find(connection, chain_id, wallet, contract_address, intent_id)

    def rows(self, *, chain_id: int | None = None) -> list[LedgerRow]:
        with self._connect() as connection:
            if chain_id is None:
                cursor = connection.execute("SELECT * FROM transactions ORDER BY created_at")
            else:
                cursor = connection.execute(
                    "SELECT * FROM transactions WHERE chain_id = ? ORDER BY created_at", (chain_id,)
                )
            return [_row_from(record) for record in cursor.fetchall()]

    @staticmethod
    def _find(
        connection: sqlite3.Connection, chain_id: int, wallet: str, contract_address: str, intent_id: str
    ) -> LedgerRow | None:
        cursor = connection.execute(
            """SELECT * FROM transactions
               WHERE chain_id = ? AND wallet = ? AND contract_address = ? AND intent_id = ?""",
            (chain_id, canonical_address(wallet), canonical_address(contract_address), intent_id),
        )
        record = cursor.fetchone()
        return _row_from(record) if record is not None else None

    @staticmethod
    def _blocking_row(connection: sqlite3.Connection, chain_id: int, wallet: str) -> LedgerRow | None:
        cursor = connection.execute(
            "SELECT * FROM transactions WHERE chain_id = ? AND wallet = ?",
            (chain_id, canonical_address(wallet)),
        )
        for record in cursor.fetchall():
            row = _row_from(record)
            if not row.is_terminal:
                return row
        return None

    # -- the one write that matters --------------------------------------------------------

    def record_signed(
        self,
        *,
        chain_id: int,
        wallet: str,
        contract_address: str,
        intent_id: str,
        chain_nonce: int,
        sign: Callable[[int], SignedIntent],
    ) -> tuple[LedgerRow, bool]:
        """Allocate a nonce, sign, and commit the row before anything is sent.

        Returns the row and whether this call created it.

        The intent is looked up **twice**. The caller checks before doing expensive work, and
        this checks again while holding the write lock. Without the second check two processes
        can both miss the first one, and the loser gets a uniqueness violation instead of the
        idempotent answer it was promised.

        `sign` is invoked inside the lock and receives the allocated nonce. That is where the
        final chain-time observation and revalidation belong: everything expensive, including
        keystore decryption and gas estimation, has already happened outside.
        """

        wallet = canonical_address(wallet)
        contract_address = canonical_address(contract_address)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._find(connection, chain_id, wallet, contract_address, intent_id)
                if existing is not None:
                    connection.execute("ROLLBACK")
                    return existing, False

                blocking = self._blocking_row(connection, chain_id, wallet)
                if blocking is not None:
                    connection.execute("ROLLBACK")
                    raise WalletBusy(
                        f"{wallet} still has {blocking.intent_id} in state {blocking.status}; "
                        "resolve it before signing another transaction"
                    )

                cursor = connection.execute(
                    "SELECT MAX(nonce) FROM transactions WHERE chain_id = ? AND wallet = ?",
                    (chain_id, wallet),
                )
                highest = cursor.fetchone()[0]
                nonce = chain_nonce if highest is None else max(chain_nonce, int(highest) + 1)

                intent = sign(nonce)
                if intent.nonce != nonce:
                    raise LedgerError("the signer used a different nonce than it was allocated")

                stamp = _now()
                connection.execute(
                    """INSERT INTO transactions (
                           chain_id, wallet, contract_address, intent_id, nonce, calldata,
                           value_wei, max_fee_wei, max_priority_wei, gas_limit, accept_by,
                           preimage, tx_hash, raw, status, attempts, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                    (
                        chain_id,
                        wallet,
                        contract_address,
                        intent_id,
                        intent.nonce,
                        intent.calldata,
                        str(intent.value_wei),
                        str(intent.max_fee_wei),
                        str(intent.max_priority_wei),
                        intent.gas_limit,
                        intent.accept_by,
                        json.dumps(intent.preimage, sort_keys=True, separators=(",", ":")),
                        intent.tx_hash,
                        intent.raw,
                        SIGNED,
                        stamp,
                        stamp,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

            created = self._find(connection, chain_id, wallet, contract_address, intent_id)
        assert created is not None
        return created, True

    def set_status(
        self,
        row: LedgerRow,
        status: str,
        *,
        block_number: int | None = None,
        block_hash: str | None = None,
        last_error: str | None = None,
        bump_attempts: bool = False,
    ) -> LedgerRow:
        if status not in ALLOWED_TRANSITIONS.get(row.status, frozenset()):
            raise IllegalTransition(f"{row.status} -> {status} is not an allowed transition")

        with self._connect() as connection:
            connection.execute(
                """UPDATE transactions
                      SET status = ?, block_number = ?, block_hash = ?, last_error = ?,
                          attempts = attempts + ?, updated_at = ?
                    WHERE chain_id = ? AND wallet = ? AND contract_address = ? AND intent_id = ?""",
                (
                    status,
                    block_number,
                    block_hash,
                    last_error,
                    1 if bump_attempts else 0,
                    _now(),
                    row.chain_id,
                    row.wallet,
                    row.contract_address,
                    row.intent_id,
                ),
            )
        return replace(
            row,
            status=status,
            block_number=block_number,
            block_hash=block_hash,
            last_error=last_error,
            attempts=row.attempts + (1 if bump_attempts else 0),
        )


def _row_from(record: sqlite3.Row) -> LedgerRow:
    return LedgerRow(
        chain_id=int(record["chain_id"]),
        wallet=record["wallet"],
        contract_address=record["contract_address"],
        intent_id=record["intent_id"],
        nonce=int(record["nonce"]),
        calldata=record["calldata"],
        value_wei=int(record["value_wei"]),
        max_fee_wei=int(record["max_fee_wei"]),
        max_priority_wei=int(record["max_priority_wei"]),
        gas_limit=int(record["gas_limit"]),
        accept_by=int(record["accept_by"]),
        preimage=json.loads(record["preimage"]),
        tx_hash=record["tx_hash"],
        raw=record["raw"],
        status=record["status"],
        block_number=None if record["block_number"] is None else int(record["block_number"]),
        block_hash=record["block_hash"],
        attempts=int(record["attempts"]),
        last_error=record["last_error"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


# --------------------------------------------------------------------------------------
# Integrity
# --------------------------------------------------------------------------------------


def verify_row_integrity(row: LedgerRow) -> None:
    """Check the stored bytes actually are the transaction the row describes.

    Rehashing alone would only prove the hash column matches the raw column. Decoding proves
    the row is not describing a different transaction entirely, which is the failure that
    would send funds somewhere nobody recorded.
    """

    raw = bytes.fromhex(row.raw.removeprefix("0x"))

    computed = "0x" + bytes(Web3.keccak(raw)).hex()
    if computed != row.tx_hash:
        raise LedgerCorrupt(f"{row.intent_id}: stored hash {row.tx_hash} is not keccak(raw)")

    try:
        sender = Account.recover_transaction(raw)
        fields = _decode_transaction(raw)
    except Exception as error:  # noqa: BLE001 - undecodable bytes are corruption, whatever the cause
        raise LedgerCorrupt(f"{row.intent_id}: signed payload does not decode: {error}") from error

    expected = {
        "sender": row.wallet,
        "chain_id": row.chain_id,
        "nonce": row.nonce,
        "to": row.contract_address,
        "calldata": row.calldata,
        "value": row.value_wei,
        "max_fee": row.max_fee_wei,
        "max_priority": row.max_priority_wei,
        "gas": row.gas_limit,
    }
    actual = {
        "sender": canonical_address(sender),
        "chain_id": fields["chainId"],
        "nonce": fields["nonce"],
        "to": canonical_address(fields["to"]),
        "calldata": fields["data"],
        "value": fields["value"],
        "max_fee": fields["maxFeePerGas"],
        "max_priority": fields["maxPriorityFeePerGas"],
        "gas": fields["gas"],
    }
    for name, want in expected.items():
        if actual[name] != want:
            raise LedgerCorrupt(
                f"{row.intent_id}: signed payload says {name}={actual[name]!r}, row says {want!r}"
            )


def _decode_transaction(raw: bytes) -> dict[str, Any]:
    """Pull the typed-transaction fields back out of the signed payload."""

    from eth_account.typed_transactions import TypedTransaction
    from hexbytes import HexBytes

    payload = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
    return {
        "chainId": int(payload["chainId"]),
        "nonce": int(payload["nonce"]),
        "to": payload["to"] if isinstance(payload["to"], str) else Web3.to_hex(payload["to"]),
        "data": payload["data"] if isinstance(payload["data"], str) else Web3.to_hex(payload["data"]),
        "value": int(payload["value"]),
        "maxFeePerGas": int(payload["maxFeePerGas"]),
        "maxPriorityFeePerGas": int(payload["maxPriorityFeePerGas"]),
        "gas": int(payload["gas"]),
    }


# --------------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """What the chain currently says about a recorded transaction."""

    status: str
    detail: str
    block_number: int | None = None
    block_hash: str | None = None
    #: Only ever true for `unknown`, and even then only the identical bytes may be sent.
    may_rebroadcast: bool = False


#: Reported when the transaction is nowhere to be found and no nonce has moved past it. It
#: does NOT mean the transaction never reached a node: a different RPC, a dropped mempool
#: entry or a replacement all look the same from here.
UNKNOWN = "unknown"


def _receipt_or_none(web3: Any, tx_hash: str) -> Any:
    try:
        return web3.eth.get_transaction_receipt(tx_hash)
    except TransactionNotFound:
        return None
    except Exception as error:  # noqa: BLE001 - a failed query is not an answer
        raise RpcUnavailable(f"receipt lookup failed for {tx_hash}: {error}") from error


def _transaction_or_none(web3: Any, tx_hash: str) -> Any:
    try:
        return web3.eth.get_transaction(tx_hash)
    except TransactionNotFound:
        return None
    except Exception as error:  # noqa: BLE001
        raise RpcUnavailable(f"transaction lookup failed for {tx_hash}: {error}") from error


def _nonce(web3: Any, wallet: str, tag: str) -> int:
    try:
        return int(web3.eth.get_transaction_count(Web3.to_checksum_address(wallet), tag))
    except Exception as error:  # noqa: BLE001
        raise RpcUnavailable(f"{tag} nonce lookup failed for {wallet}: {error}") from error


def _field(value: Any, name: str) -> Any:
    return value[name] if isinstance(value, dict) else getattr(value, name)


def _as_hex(value: Any) -> str:
    if isinstance(value, str):
        return value if value.startswith("0x") else "0x" + value
    return "0x" + bytes(value).hex()


def resolve(
    web3: Any,
    row: LedgerRow,
    *,
    fallback_web3: Any | None = None,
    chain_now: int | None = None,
    stuck_after_seconds: int = 1_800,
) -> Verdict:
    """Say what the chain currently knows about this row, without changing anything.

    Read-only on purpose. `tx-status` calls this; only `tx-resolve` persists the answer.
    """

    verify_row_integrity(row)
    receipt = _receipt_or_none(web3, row.tx_hash)

    if receipt is not None:
        block_number = int(_field(receipt, "blockNumber"))
        block_hash = _as_hex(_field(receipt, "blockHash"))
        succeeded = int(_field(receipt, "status")) == 1
        return Verdict(
            status=INCLUDED_SUCCESS if succeeded else INCLUDED_REVERTED,
            detail="included" if succeeded else "included but reverted; reason is best-effort",
            block_number=block_number,
            block_hash=block_hash,
        )

    if _transaction_or_none(web3, row.tx_hash) is not None:
        return Verdict(PENDING, "the node holds it but it is not mined")

    latest = _nonce(web3, row.wallet, "latest")
    pending = _nonce(web3, row.wallet, "pending")

    if latest > row.nonce:
        # One node's view is not the chain. Ask a second before calling a nonce spent, because
        # this verdict is terminal and it abandons a transaction that may yet be mined.
        if fallback_web3 is not None:
            confirmation = _receipt_or_none(fallback_web3, row.tx_hash)
            if confirmation is not None:
                block_number = int(_field(confirmation, "blockNumber"))
                succeeded = int(_field(confirmation, "status")) == 1
                return Verdict(
                    status=INCLUDED_SUCCESS if succeeded else INCLUDED_REVERTED,
                    detail="found on the fallback RPC",
                    block_number=block_number,
                    block_hash=_as_hex(_field(confirmation, "blockHash")),
                )
        return Verdict(
            NONCE_CONSUMED_OR_REPLACED,
            f"latest nonce {latest} is past {row.nonce}; something else occupied this slot",
        )

    if pending > row.nonce:
        # A pending count above ours proves only that this node knows of *a* transaction in
        # that slot. It does not prove ours was consumed, so this stays non-terminal.
        return Verdict(
            NONCE_CONFLICT_PENDING,
            f"pending nonce {pending} is past {row.nonce}, but nothing is confirmed",
        )

    if chain_now is not None and row.accept_by <= chain_now:
        return Verdict(STUCK, "the acceptance deadline has passed; resending cannot succeed")

    age = (datetime.now(UTC) - datetime.fromisoformat(row.updated_at)).total_seconds()
    if age > stuck_after_seconds:
        return Verdict(STUCK, f"unresolved for {int(age)}s, past the {stuck_after_seconds}s bound")

    return Verdict(UNKNOWN, "no receipt and no transaction; the identical bytes may be resent",
                   may_rebroadcast=True)


@dataclass(frozen=True)
class ConfirmationPolicy:
    """How a run decides an included transaction has actually settled.

    Two policies exist, and which one is in force is always reported, because "confirmed"
    means different things under each.
    """

    name: str
    read_head: Callable[[Any], int]


def safe_head_policy() -> ConfirmationPolicy:
    """The only policy used against a real network.

    Reads the chain's own `safe` tag and fails closed when the node has none. Substituting
    the latest block would turn the rule into decoration.
    """

    def head(web3: Any) -> int:
        try:
            return int(_field(web3.eth.get_block("safe"), "number"))
        except Exception as error:  # noqa: BLE001 - includes a node with no safe tag at all
            raise SafeHeadUnavailable(
                "this node cannot report a safe head, so nothing can be called confirmed; "
                "substituting the latest block would make the rule meaningless"
            ) from error

    return ConfirmationPolicy("safe-head", head)


def local_depth_policy(blocks: int) -> ConfirmationPolicy:
    """A rehearsal-only stand-in for chains that have no meaningful safe head.

    Anvil pins `safe` at block zero forever, so a local run can never satisfy the real rule.
    Counting blocks instead is not a finality claim and must never be used against a real
    network, which is why it has to be asked for explicitly on the command line and is named
    in every result it produces.
    """

    if blocks < 0:
        raise ValueError("confirmation depth cannot be negative")

    def head(web3: Any) -> int:
        return max(0, int(_field(web3.eth.get_block("latest"), "number")) - blocks)

    return ConfirmationPolicy(f"local-depth-{blocks}", head)


def confirm(web3: Any, row: LedgerRow, *, policy: ConfirmationPolicy | None = None) -> Verdict:
    """Decide whether an included transaction has actually settled.

    A receipt is inclusion. Base Sepolia can still reorg it. Confirmation needs the recorded
    block hash to still be canonical at that height and the block to be at or below the safe
    head. Success and revert stay distinguishable at every step, which is why there is no
    single `confirmed`.
    """

    if row.block_number is None or row.block_hash is None:
        raise LedgerError(f"{row.intent_id} has no recorded block to confirm")

    try:
        block = web3.eth.get_block(row.block_number)
    except BlockNotFound:
        return Verdict(REORGED, f"block {row.block_number} no longer exists")
    except Exception as error:  # noqa: BLE001
        raise RpcUnavailable(f"block lookup failed: {error}") from error

    if _as_hex(_field(block, "hash")) != row.block_hash:
        return Verdict(REORGED, f"block {row.block_number} now has a different hash")

    policy = policy or safe_head_policy()
    head = policy.read_head(web3)

    if row.block_number > head:
        return Verdict(
            row.status,
            f"included at {row.block_number}, {policy.name} is at {head}; not yet settled",
            block_number=row.block_number,
            block_hash=row.block_hash,
        )

    settled = CONFIRMED_SUCCESS if row.status == INCLUDED_SUCCESS else CONFIRMED_REVERTED
    return Verdict(
        settled,
        f"canonical at or below {policy.name} ({head})",
        block_number=row.block_number,
        block_hash=row.block_hash,
    )


# --------------------------------------------------------------------------------------
# Broadcasting
# --------------------------------------------------------------------------------------

BROADCAST_ENV = "WRASSE_ALLOW_BROADCAST"

_ALREADY_KNOWN = ("already known", "already imported", "known transaction", "alreadyknown")
_NONCE_TOO_LOW = ("nonce too low", "invalid nonce", "oldnonce", "nonce is too low")
#: Reasons a resend cannot fix. Anything not listed here is treated as uncertain, because
#: guessing that a failure is permanent abandons a transaction that might still be live.
_DETERMINISTIC = (
    "insufficient funds",
    "intrinsic gas too low",
    "exceeds block gas limit",
    "invalid sender",
    "oversized data",
    "negative value",
)


@dataclass(frozen=True)
class BroadcastOutcome:
    status: str
    detail: str


def require_broadcast_opt_in(environ: dict[str, str] | None = None) -> None:
    """Refuse to send unless somebody deliberately asked for it, this invocation."""

    source = os.environ if environ is None else environ
    if source.get(BROADCAST_ENV) != "1":
        raise BroadcastNotAuthorised(
            f"refusing to broadcast: set {BROADCAST_ENV}=1 on the command that should send. "
            "A signed transaction moves funds without needing the key again, so the barrier "
            "is here rather than at the signature."
        )


def broadcast(web3: Any, row: LedgerRow, *, environ: dict[str, str] | None = None) -> BroadcastOutcome:
    """Send the recorded bytes, exactly as recorded, once.

    Never called for a transaction that was rebuilt. The caller must have durably recorded
    `send_attempted` first, so a crash between here and the status update lands in the window
    the resolver exists for.
    """

    require_broadcast_opt_in(environ)
    verify_row_integrity(row)

    try:
        web3.eth.send_raw_transaction(bytes.fromhex(row.raw.removeprefix("0x")))
    except Exception as error:  # noqa: BLE001 - the classification below is the point
        message = str(error).lower()
        if any(token in message for token in _ALREADY_KNOWN):
            return BroadcastOutcome(PENDING, "the node already had it")
        if any(token in message for token in _NONCE_TOO_LOW):
            return BroadcastOutcome(
                NONCE_CONFLICT_PENDING, "the node reports this nonce as used; investigating"
            )
        if any(token in message for token in _DETERMINISTIC):
            raise DeterministicRejection(str(error)) from error
        # Everything else is uncertain. The transaction may well be in flight, so this must
        # never be retried here; it goes back to the resolver, which can only resend the
        # identical bytes.
        return BroadcastOutcome(SEND_ATTEMPTED, f"uncertain: {error}")

    _trip_failpoint(CRASH_AFTER_SEND)
    return BroadcastOutcome(PENDING, "broadcast accepted")


# --------------------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------------------

#: Bounded, and shown in the preview. An unbounded multiplier on an estimate is a way to
#: overpay by an amount nobody looked at.
GAS_HEADROOM_PERCENT = 25
MAX_GAS_LIMIT = 3_000_000


class RoleMismatch(RuntimeError):
    """A keystore does not hold the wallet the role it was asked to play requires."""


def load_signer(keystore: Path | str, password_file: Path | str, *, expected_address: str) -> Any:
    """Decrypt a keystore and prove it is the wallet this role is allowed to use.

    Foundry keystores are standard v3 and carry no address field, so the address has to be
    derived. That is what makes this check real rather than decorative: nothing in the file
    asserts an identity, so the identity comes from the key itself.

    The private key never leaves the returned account object, is never logged, never written
    and never placed in the environment.
    """

    keystore_json = json.loads(Path(keystore).read_text(encoding="utf-8"))
    password = Path(password_file).read_text(encoding="utf-8").strip()
    account = Account.from_key(Account.decrypt(keystore_json, password))

    if canonical_address(account.address) != canonical_address(expected_address):
        raise RoleMismatch(
            f"{keystore} holds {account.address}, but this role requires {expected_address}"
        )
    return account


def bounded_gas_limit(estimate: int) -> int:
    """Add visible, bounded headroom to an estimate."""

    padded = estimate * (100 + GAS_HEADROOM_PERCENT) // 100
    if padded > MAX_GAS_LIMIT:
        raise LedgerError(f"gas limit {padded} exceeds the {MAX_GAS_LIMIT} bound")
    return padded


def require_affordable(balance_wei: int, *, value_wei: int, gas_limit: int, max_fee_wei: int) -> int:
    """Refuse to sign something the wallet cannot pay for. Returns the worst-case cost."""

    worst_case = value_wei + gas_limit * max_fee_wei
    if balance_wei < worst_case:
        raise LedgerError(
            f"wallet holds {balance_wei} wei but the worst case costs {worst_case} wei "
            f"({value_wei} value plus {gas_limit} gas at {max_fee_wei})"
        )
    return worst_case


def sign_transaction(account: Any, transaction: dict[str, Any]) -> SignedIntent:
    """Sign, and compute the hash locally from the bytes rather than trusting a node."""

    signed = account.sign_transaction(transaction)
    raw = "0x" + bytes(signed.raw_transaction).hex()
    return SignedIntent(
        nonce=int(transaction["nonce"]),
        calldata=transaction["data"],
        value_wei=int(transaction["value"]),
        max_fee_wei=int(transaction["maxFeePerGas"]),
        max_priority_wei=int(transaction["maxPriorityFeePerGas"]),
        gas_limit=int(transaction["gas"]),
        accept_by=0,
        preimage={},
        tx_hash="0x" + bytes(Web3.keccak(hexstr=raw)).hex(),
        raw=raw,
    )


def with_intent_context(
    intent: SignedIntent, *, accept_by: int, preimage: dict[str, Any]
) -> SignedIntent:
    return replace(intent, accept_by=accept_by, preimage=preimage)


__all__: Sequence[str] = (
    "ALLOWED_TRANSITIONS",
    "ALL_STATUSES",
    "BROADCAST_ENV",
    "BroadcastNotAuthorised",
    "BroadcastOutcome",
    "CRASH_AFTER_SEND",
    "DeterministicRejection",
    "FAILPOINT_ENV",
    "IllegalTransition",
    "LedgerCorrupt",
    "LedgerError",
    "LedgerRow",
    "RoleMismatch",
    "RpcUnavailable",
    "SafeHeadUnavailable",
    "SignedIntent",
    "TERMINAL_STATUSES",
    "TransactionLedger",
    "UNKNOWN",
    "Verdict",
    "WalletBusy",
    "arm_failpoint",
    "bounded_gas_limit",
    "broadcast",
    "canonical_address",
    "ConfirmationPolicy",
    "confirm",
    "load_signer",
    "local_depth_policy",
    "safe_head_policy",
    "require_affordable",
    "require_broadcast_opt_in",
    "resolve",
    "sign_transaction",
    "verify_row_integrity",
    "with_intent_context",
)

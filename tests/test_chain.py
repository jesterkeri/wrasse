"""The transaction orchestrator, tested against a node that fails the way real ones do.

A fake beats a local chain here. Anvil will not time out on demand, will not return a 429,
and will not hand back a receipt that a second RPC disagrees with. Those are exactly the
situations that decide whether a retry creates a second funded offer.
"""

from __future__ import annotations

import json

import pytest
from eth_account import Account
from web3 import Web3
from web3.exceptions import BlockNotFound, TransactionNotFound

from wrasse import chain

BUYER_KEY = "0x" + "11" * 32
OTHER_KEY = "0x" + "44" * 32
ESCROW = Web3.to_checksum_address("0x" + "22" * 20)
CHAIN_ID = 84532
INTENT = "9f2c1a:urgent"


# --------------------------------------------------------------------------------------
# A node that can be wrong in specific ways
# --------------------------------------------------------------------------------------


class Boom(Exception):
    """Stands in for a transport failure, a 429 or a 5xx."""


class FakeEth:
    def __init__(self, chain_id: int = CHAIN_ID) -> None:
        self.chain_id = chain_id
        self.receipts: dict[str, dict] = {}
        self.transactions: dict[str, dict] = {}
        self.latest: dict[str, int] = {}
        self.pending: dict[str, int] = {}
        self.blocks: dict[object, dict] = {}
        self.sent: list[bytes] = []
        self.send_error: Exception | None = None
        self.fail_receipts = False
        self.safe_supported = True
        self.echo_hash: bytes | None = None
        self.safe_nonce: dict[str, int] = {}

    def get_transaction_receipt(self, tx_hash):
        if self.fail_receipts:
            raise Boom("429 Too Many Requests")
        if tx_hash not in self.receipts:
            raise TransactionNotFound(tx_hash)
        return self.receipts[tx_hash]

    def get_transaction(self, tx_hash):
        if tx_hash not in self.transactions:
            raise TransactionNotFound(tx_hash)
        return self.transactions[tx_hash]

    def get_transaction_count(self, address, tag):
        if tag == "safe" and not self.safe_supported:
            raise Boom("unknown block tag safe")
        table = {"latest": self.latest, "pending": self.pending, "safe": self.safe_nonce}[tag]
        return table.get(chain.canonical_address(address), 0)

    def get_block(self, identifier):
        if identifier == "safe" and not self.safe_supported:
            raise Boom("unknown block tag safe")
        if identifier not in self.blocks:
            raise BlockNotFound(str(identifier))
        return self.blocks[identifier]

    def send_raw_transaction(self, raw: bytes):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(raw)
        # A real node echoes the transaction hash, and the orchestrator checks that it names
        # the transaction we actually sent.
        return self.echo_hash if self.echo_hash is not None else Web3.keccak(raw)


class FakeWeb3:
    def __init__(self, chain_id: int = CHAIN_ID) -> None:
        self.eth = FakeEth(chain_id)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _signed(nonce: int = 0, *, key: str = BUYER_KEY, calldata: str = "0xdeadbeef",
            accept_by: int = 1_800_000_000, policy_hash: str = "0xaa") -> tuple[object, chain.SignedIntent]:
    account = Account.from_key(key)
    transaction = {
        "chainId": CHAIN_ID,
        "nonce": nonce,
        "to": ESCROW,
        "data": calldata,
        "value": 10**14,
        "maxFeePerGas": 10**9,
        "maxPriorityFeePerGas": 10**6,
        "gas": 250_000,
        "type": 2,
    }
    intent = chain.with_intent_context(
        chain.sign_transaction(account, transaction),
        accept_by=accept_by,
        preimage={"policy_hash": policy_hash},
    )
    return account, intent


def _ledger(tmp_path) -> chain.TransactionLedger:
    return chain.TransactionLedger(tmp_path / "state" / "transactions.db")


def _record(ledger, *, intent_id: str = INTENT, nonce: int = 0, chain_nonce: int = 0,
            wallet: str | None = None, **kwargs):
    account, intent = _signed(nonce, **kwargs)
    calls: list[int] = []

    def sign(allocated: int) -> chain.SignedIntent:
        calls.append(allocated)
        return intent

    row, created = ledger.record_signed(
        chain_id=CHAIN_ID,
        wallet=wallet or account.address,
        contract_address=ESCROW,
        intent_id=intent_id,
        read_chain_nonce=lambda: chain_nonce,
        sign=sign,
    )
    return row, created, calls


# --------------------------------------------------------------------------------------
# The critical property
# --------------------------------------------------------------------------------------


def test_the_same_intent_never_signs_twice_even_when_the_terms_move(tmp_path):
    """The reason action identity cannot come from the policy hash.

    `acceptBy` is re-derived from chain time on every attempt, so the committed terms differ
    between the first call and the retry. If identity followed the terms, this second call
    would look like a brand new deal and would fund a second offer.
    """
    ledger = _ledger(tmp_path)
    first, created, _ = _record(ledger, accept_by=1_800_000_000, policy_hash="0xaaa")
    assert created is True

    second, created_again, calls = _record(ledger, accept_by=1_800_009_999, policy_hash="0xbbb")
    assert created_again is False
    assert calls == [], "the signer must not even be invoked for an intent that already exists"
    assert second.tx_hash == first.tx_hash
    assert second.preimage["policy_hash"] == "0xaaa"
    assert len(ledger.rows()) == 1


def test_the_locked_recheck_returns_the_record_rather_than_a_constraint_error(tmp_path):
    """Two processes can both miss the opening lookup before either holds the write lock.

    Uniqueness alone would give the loser a database error, when what it was promised was the
    idempotent answer. The recheck inside the lock is what turns that into a result.
    """
    ledger = _ledger(tmp_path)
    _record(ledger)

    # This call skips any pre-check, exactly like a process that lost the race.
    row, created, calls = _record(ledger, nonce=7, chain_nonce=7)
    assert created is False
    assert calls == []
    assert row.nonce == 0


def test_checksum_casing_cannot_open_a_second_row(tmp_path):
    ledger = _ledger(tmp_path)
    account, _ = _signed()
    _record(ledger)

    row, created, _ = _record(ledger, wallet=account.address.lower())
    assert created is False
    assert len(ledger.rows()) == 1


# --------------------------------------------------------------------------------------
# Serialisation and nonces
# --------------------------------------------------------------------------------------


def test_a_wallet_with_something_in_flight_refuses_a_second_transaction(tmp_path):
    ledger = _ledger(tmp_path)
    _record(ledger)
    with pytest.raises(chain.WalletBusy, match="in state signed"):
        _record(ledger, intent_id="other:urgent", nonce=1, chain_nonce=1)


@pytest.mark.parametrize("blocking", [chain.STUCK, chain.NONCE_CONFLICT_PENDING])
def test_an_unresolved_nonce_holds_the_wallet_rather_than_opening_a_gap(tmp_path, blocking):
    """Both states mean "we do not know whether this nonce is spent".

    Letting the wallet move on would skip a nonce, and every later transaction would sit
    behind the gap forever.
    """
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    row = ledger.set_status(row, chain.SEND_ATTEMPTED)
    row = ledger.set_status(row, blocking)
    assert row.is_terminal is False

    with pytest.raises(chain.WalletBusy):
        _record(ledger, intent_id="other:urgent", nonce=1, chain_nonce=1)


def test_a_terminal_row_releases_the_wallet(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    row = ledger.set_status(row, chain.SEND_ATTEMPTED)
    row = ledger.set_status(row, chain.INCLUDED_SUCCESS, block_number=5, block_hash="0x" + "0e" * 32)
    ledger.set_status(row, chain.CONFIRMED_SUCCESS, block_number=5, block_hash="0x" + "0e" * 32)

    _, created, calls = _record(ledger, intent_id="other:urgent", nonce=1, chain_nonce=1)
    assert created is True
    assert calls == [1]


def test_the_nonce_comes_from_whichever_source_is_further_ahead(tmp_path):
    """The ledger knows what it allocated; the node knows what it has seen pending."""
    ledger = _ledger(tmp_path)
    _, _, calls = _record(ledger, nonce=9, chain_nonce=9)
    assert calls == [9]


def test_illegal_transitions_are_refused(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    with pytest.raises(chain.IllegalTransition, match="signed -> confirmed_success"):
        ledger.set_status(row, chain.CONFIRMED_SUCCESS)


def test_inclusion_can_move_backwards_to_reorged(tmp_path):
    """Transitions are a graph, not a ladder. A reorg unmines a transaction."""
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    row = ledger.set_status(row, chain.SEND_ATTEMPTED)
    row = ledger.set_status(row, chain.INCLUDED_SUCCESS, block_number=5, block_hash="0x" + "0e" * 32)
    row = ledger.set_status(row, chain.REORGED)
    assert row.status == chain.REORGED


# --------------------------------------------------------------------------------------
# Integrity
# --------------------------------------------------------------------------------------


def test_a_row_that_disagrees_with_its_own_bytes_stops_everything(tmp_path):
    """Rehashing only proves two columns agree. Decoding proves the row is about this
    transaction and not some other one."""
    import dataclasses

    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    chain.verify_row_integrity(row)

    for field, value in [
        ("nonce", 4),
        ("value_wei", 1),
        ("calldata", "0xfeedface"),
        ("gas_limit", 21_000),
        ("max_fee_wei", 2),
        ("chain_id", 1),
        ("contract_address", chain.canonical_address("0x" + "99" * 20)),
    ]:
        with pytest.raises(chain.LedgerCorrupt):
            chain.verify_row_integrity(dataclasses.replace(row, **{field: value}))


# --------------------------------------------------------------------------------------
# Broadcasting
# --------------------------------------------------------------------------------------


def test_broadcasting_refuses_without_the_deliberate_opt_in(tmp_path):
    """The barrier is at the send, not the signature.

    A signed payload moves funds without the key being decrypted again, so gating signing
    would leave replay wide open.
    """
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()

    with pytest.raises(chain.BroadcastNotAuthorised, match="WRASSE_ALLOW_BROADCAST"):
        chain.broadcast(web3, row, environ={})
    assert web3.eth.sent == []

    with pytest.raises(chain.BroadcastNotAuthorised):
        chain.broadcast(web3, row, environ={"WRASSE_ALLOW_BROADCAST": "true"})
    assert web3.eth.sent == []


def test_an_authorised_broadcast_sends_the_recorded_bytes_verbatim(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()

    outcome = chain.broadcast(web3, row, environ={"WRASSE_ALLOW_BROADCAST": "1"})
    assert outcome.status == chain.PENDING
    assert web3.eth.sent == [bytes.fromhex(row.raw.removeprefix("0x"))]


@pytest.mark.parametrize(
    "message,expected",
    [
        ("already known", chain.PENDING),
        ("known transaction: 0xabc", chain.PENDING),
        ("nonce too low", chain.NONCE_CONFLICT_PENDING),
        ("Read timed out", chain.SEND_ATTEMPTED),
        ("429 Too Many Requests", chain.SEND_ATTEMPTED),
        ("502 Bad Gateway", chain.SEND_ATTEMPTED),
    ],
)
def test_broadcast_errors_are_classified_rather_than_retried(tmp_path, message, expected):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()
    web3.eth.send_error = Boom(message)

    outcome = chain.broadcast(web3, row, environ={"WRASSE_ALLOW_BROADCAST": "1"})
    assert outcome.status == expected


def test_a_deterministic_rejection_stops_instead_of_resending(tmp_path):
    """Resending something the node will always refuse just burns the wallet's turn."""
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()
    web3.eth.send_error = Boom("insufficient funds for gas * price + value")

    with pytest.raises(chain.DeterministicRejection, match="insufficient funds"):
        chain.broadcast(web3, row, environ={"WRASSE_ALLOW_BROADCAST": "1"})


# --------------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------------


def _receipt(status: int = 1, block: int = 12, block_hash: str | None = None) -> dict:
    return {
        "status": status,
        "blockNumber": block,
        "blockHash": block_hash or "0x" + "0e" * 32,
    }


def test_a_receipt_distinguishes_success_from_revert(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()

    web3.eth.receipts[row.tx_hash] = _receipt(status=1)
    assert chain.resolve(web3, row).status == chain.INCLUDED_SUCCESS

    web3.eth.receipts[row.tx_hash] = _receipt(status=0)
    verdict = chain.resolve(web3, row)
    assert verdict.status == chain.INCLUDED_REVERTED
    assert "best-effort" in verdict.detail


def test_a_transaction_the_node_holds_is_pending(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()
    web3.eth.transactions[row.tx_hash] = {"hash": row.tx_hash}

    verdict = chain.resolve(web3, row)
    assert verdict.status == chain.PENDING
    assert verdict.may_rebroadcast is False


def test_nothing_found_is_unknown_and_only_then_may_bytes_be_resent(tmp_path):
    """`unknown` is not "never reached the node".

    A different RPC, a dropped mempool entry or a replacement all look identical from here,
    which is why the only safe response is the identical payload.
    """
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    verdict = chain.resolve(FakeWeb3(), row)

    assert verdict.status == chain.UNKNOWN
    assert verdict.may_rebroadcast is True


def test_a_pending_nonce_alone_is_never_a_terminal_answer(tmp_path):
    """A pending count above ours proves some node knows of a transaction in that slot.

    It does not prove ours was consumed, so calling it terminal would abandon a transaction
    that may still be mined.
    """
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()
    web3.eth.pending[row.wallet] = row.nonce + 1

    verdict = chain.resolve(web3, row)
    assert verdict.status == chain.NONCE_CONFLICT_PENDING
    assert verdict.status not in chain.TERMINAL_STATUSES
    assert verdict.may_rebroadcast is False


def test_one_node_alone_cannot_declare_the_slot_spent(tmp_path):
    """Abandoning a payload is terminal, so it needs corroboration.

    Without a second chain agreeing, the honest answer is that we do not know, which holds the
    wallet rather than skipping a nonce.
    """
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()
    web3.eth.latest[row.wallet] = row.nonce + 1
    web3.eth.pending[row.wallet] = row.nonce + 1

    verdict = chain.resolve(web3, row)
    assert verdict.status == chain.NONCE_CONFLICT_PENDING
    assert verdict.status not in chain.TERMINAL_STATUSES
    assert "no fallback RPC is configured" in verdict.detail


def test_a_nonce_used_only_at_the_tip_is_not_declared_spent(tmp_path):
    """Two views of the tip are corroboration, not confirmation.

    A short reorg can undo whatever advanced the nonce. This verdict releases the wallet, so
    acting on an unsafe tip would simply reopen the gap a few blocks later.
    """
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    primary, fallback = FakeWeb3(), FakeWeb3()
    for node in (primary, fallback):
        node.eth.latest[row.wallet] = row.nonce + 1

    verdict = chain.resolve(primary, row, fallback_web3=fallback)
    assert verdict.status == chain.NONCE_CONFLICT_PENDING
    assert "not yet at the safe head" in verdict.detail


def test_a_nonce_spent_at_the_safe_head_is_declared_spent(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    primary, fallback = FakeWeb3(), FakeWeb3()
    for node in (primary, fallback):
        node.eth.latest[row.wallet] = row.nonce + 1
        node.eth.safe_nonce[row.wallet] = row.nonce + 1

    verdict = chain.resolve(primary, row, fallback_web3=fallback)
    assert verdict.status == chain.NONCE_CONSUMED_OR_REPLACED
    assert verdict.status in chain.TERMINAL_STATUSES


def test_a_node_with_no_safe_head_cannot_abandon_the_payload(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    primary, fallback = FakeWeb3(), FakeWeb3()
    for node in (primary, fallback):
        node.eth.latest[row.wallet] = row.nonce + 1
        node.eth.safe_supported = False

    verdict = chain.resolve(primary, row, fallback_web3=fallback)
    assert verdict.status == chain.NONCE_CONFLICT_PENDING
    assert "safe head" in verdict.detail


def test_two_nodes_that_disagree_do_not_abandon_the_payload(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    primary, fallback = FakeWeb3(), FakeWeb3()
    primary.eth.latest[row.wallet] = row.nonce + 1  # the fallback still sees the slot free

    verdict = chain.resolve(primary, row, fallback_web3=fallback)
    assert verdict.status == chain.NONCE_CONFLICT_PENDING
    assert "disagree" in verdict.detail


def test_a_fallback_on_the_wrong_chain_is_not_a_second_opinion(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    primary, fallback = FakeWeb3(), FakeWeb3(chain_id=1)
    primary.eth.latest[row.wallet] = row.nonce + 1

    with pytest.raises(chain.RpcUnavailable, match="reports chain 1"):
        chain.resolve(primary, row, fallback_web3=fallback)


def test_a_node_naming_a_different_hash_is_an_integrity_failure(tmp_path):
    """The local hash is authoritative. A node accepting something else is not agreeing."""
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()
    web3.eth.echo_hash = bytes.fromhex("ee" * 32)

    with pytest.raises(chain.LedgerCorrupt, match="but we sent"):
        chain.broadcast(web3, row, environ={"WRASSE_ALLOW_BROADCAST": "1"})


def test_the_fallback_rpc_is_asked_before_a_transaction_is_abandoned(tmp_path):
    """One node's view is not the chain, and this verdict is terminal."""
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    primary = FakeWeb3()
    primary.eth.latest[row.wallet] = row.nonce + 1

    fallback = FakeWeb3()
    fallback.eth.receipts[row.tx_hash] = _receipt(status=1, block=31)

    verdict = chain.resolve(primary, row, fallback_web3=fallback)
    assert verdict.status == chain.INCLUDED_SUCCESS
    assert verdict.block_number == 31
    assert "fallback" in verdict.detail


def test_a_failed_query_is_not_an_absent_transaction(tmp_path):
    """Treating a 429 as "not found" is how a live transaction gets resent for no reason."""
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()
    web3.eth.fail_receipts = True

    with pytest.raises(chain.RpcUnavailable, match="receipt lookup failed"):
        chain.resolve(web3, row)


def test_an_expired_deadline_is_never_rebroadcast(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger, accept_by=1_700_000_000)

    verdict = chain.resolve(FakeWeb3(), row, chain_now=1_700_000_001)
    assert verdict.status == chain.STUCK
    assert verdict.may_rebroadcast is False


# --------------------------------------------------------------------------------------
# Confirmation
# --------------------------------------------------------------------------------------


def _included(ledger, row, *, block: int = 12, block_hash: str | None = None, status: int = 1):
    row = ledger.set_status(row, chain.SEND_ATTEMPTED)
    return ledger.set_status(
        row,
        chain.INCLUDED_SUCCESS if status == 1 else chain.INCLUDED_REVERTED,
        block_number=block,
        block_hash=block_hash or "0x" + "0e" * 32,
    )


def test_a_receipt_alone_is_not_confirmation(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    row = _included(ledger, row, block=12)

    web3 = FakeWeb3()
    web3.eth.blocks[12] = {"hash": row.block_hash, "number": 12}
    web3.eth.blocks["safe"] = {"number": 9}

    verdict = chain.confirm(web3, row)
    assert verdict.status == chain.INCLUDED_SUCCESS
    assert "not yet settled" in verdict.detail


def test_confirmation_keeps_success_and_revert_distinguishable(tmp_path):
    ledger = _ledger(tmp_path)
    web3 = FakeWeb3()
    web3.eth.blocks[12] = {"hash": "0x" + "0e" * 32, "number": 12}
    web3.eth.blocks["safe"] = {"number": 20}

    row, _, _ = _record(ledger)
    assert chain.confirm(web3, _included(ledger, row, status=1)).status == chain.CONFIRMED_SUCCESS

    second = chain.TransactionLedger(tmp_path / "second" / "transactions.db")
    row2, _, _ = _record(second, intent_id="other:budget")
    assert chain.confirm(web3, _included(second, row2, status=0)).status == chain.CONFIRMED_REVERTED


def test_a_block_that_changed_hash_is_reorged_not_confirmed(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    row = _included(ledger, row, block=12, block_hash="0x" + "0e" * 32)

    web3 = FakeWeb3()
    web3.eth.blocks[12] = {"hash": "0x" + "ff" * 32, "number": 12}
    web3.eth.blocks["safe"] = {"number": 20}

    assert chain.confirm(web3, row).status == chain.REORGED


def test_a_vanished_block_is_reorged(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    row = _included(ledger, row, block=12)
    assert chain.confirm(FakeWeb3(), row).status == chain.REORGED


def test_a_node_without_a_safe_head_fails_closed(tmp_path):
    """Substituting the latest block would turn the confirmation rule into decoration."""
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    row = _included(ledger, row, block=12)

    web3 = FakeWeb3()
    web3.eth.blocks[12] = {"hash": row.block_hash, "number": 12}
    web3.eth.safe_supported = False

    with pytest.raises(chain.SafeHeadUnavailable, match="cannot report a safe head"):
        chain.confirm(web3, row)


# --------------------------------------------------------------------------------------
# The failpoint
# --------------------------------------------------------------------------------------


def test_the_failpoint_only_arms_from_a_value_the_caller_passed(tmp_path, monkeypatch):
    """A crash simulation that a stale dotfile could arm would eventually fire for real."""
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()

    monkeypatch.setenv(chain.FAILPOINT_ENV, chain.CRASH_AFTER_SEND)
    chain.arm_failpoint(None)
    try:
        chain.broadcast(web3, row, environ={"WRASSE_ALLOW_BROADCAST": "1"})
    finally:
        chain.arm_failpoint(None)
    assert len(web3.eth.sent) == 1, "the environment alone must not arm the failpoint"


def test_the_armed_failpoint_crashes_after_the_send_not_before(tmp_path):
    """The crash has to land in the window the resolver exists for: the bytes are gone, and
    nothing recorded that they went."""
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()

    chain.arm_failpoint(chain.CRASH_AFTER_SEND)
    try:
        with pytest.raises(SystemExit):
            chain.broadcast(web3, row, environ={"WRASSE_ALLOW_BROADCAST": "1"})
    finally:
        chain.arm_failpoint(None)

    assert len(web3.eth.sent) == 1, "the transaction must already be out before the crash"


def test_a_fresh_process_resolves_the_crashed_intent_without_rebuilding(tmp_path):
    """The whole point. After the crash a new run finds the same intent, does not sign, and
    learns what happened from the hash it recorded."""
    path = tmp_path / "state" / "transactions.db"
    first = chain.TransactionLedger(path)
    row, _, _ = _record(first)
    web3 = FakeWeb3()

    chain.arm_failpoint(chain.CRASH_AFTER_SEND)
    try:
        with pytest.raises(SystemExit):
            chain.broadcast(web3, row, environ={"WRASSE_ALLOW_BROADCAST": "1"})
    finally:
        chain.arm_failpoint(None)

    reopened = chain.TransactionLedger(path)
    recovered, created, calls = _record(reopened, accept_by=1_800_055_555, policy_hash="0xnew")
    assert created is False
    assert calls == []
    assert recovered.tx_hash == row.tx_hash

    web3.eth.receipts[recovered.tx_hash] = _receipt(status=1, block=44)
    assert chain.resolve(web3, recovered).status == chain.INCLUDED_SUCCESS
    assert len(reopened.rows()) == 1


# --------------------------------------------------------------------------------------
# Signing preconditions
# --------------------------------------------------------------------------------------


def test_a_keystore_holding_the_wrong_wallet_is_refused(tmp_path):
    """Foundry keystores carry no address field, so the identity comes from the key itself.
    That is what makes this check real rather than a label comparison."""
    account = Account.from_key(OTHER_KEY)
    keystore = tmp_path / "keystore"
    password = tmp_path / "keystore.password"
    keystore.write_text(json.dumps(Account.encrypt(OTHER_KEY, "hunter2", kdf="pbkdf2")))
    password.write_text("hunter2\n")

    loaded = chain.load_signer(keystore, password, expected_address=account.address)
    assert chain.canonical_address(loaded.address) == chain.canonical_address(account.address)

    with pytest.raises(chain.RoleMismatch, match="requires"):
        chain.load_signer(keystore, password, expected_address=Account.from_key(BUYER_KEY).address)


def test_gas_headroom_is_bounded():
    assert chain.bounded_gas_limit(200_000) == 250_000
    with pytest.raises(chain.LedgerError, match="exceeds the"):
        chain.bounded_gas_limit(chain.MAX_GAS_LIMIT)


def test_a_wallet_that_cannot_cover_the_worst_case_does_not_sign():
    worst = chain.require_affordable(10**18, value_wei=10**14, gas_limit=250_000, max_fee_wei=10**9)
    assert worst == 10**14 + 250_000 * 10**9

    with pytest.raises(chain.LedgerError, match="worst case"):
        chain.require_affordable(10**12, value_wei=10**14, gas_limit=250_000, max_fee_wei=10**9)


# --------------------------------------------------------------------------------------
# Nonce lifecycle
# --------------------------------------------------------------------------------------


def test_a_nonce_that_never_reached_a_mempool_is_given_back(tmp_path):
    """A pre-validation refusal means nothing can ever mine at that nonce.

    Keeping it allocated would leave a permanent gap, and every later transaction from this
    wallet would sit behind it forever. One bounced send would end the demo.
    """
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger, nonce=0, chain_nonce=0)
    row = ledger.set_status(row, chain.UNBROADCAST, last_error="insufficient funds")
    assert row.is_terminal is True

    # A genuinely new intent, so different bytes and a different hash.
    reused, created, calls = _record(
        ledger, intent_id="next:urgent", nonce=0, chain_nonce=0, calldata="0xcafebabe"
    )
    assert created is True
    assert calls == [0], "the released nonce must be offered again, not skipped"
    assert reused.nonce == 0


def test_a_held_nonce_is_not_given_back(tmp_path):
    """`stuck` means we do not know, and not knowing is not permission to skip."""
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    row = ledger.set_status(row, chain.SEND_ATTEMPTED)
    ledger.set_status(row, chain.STUCK)

    with pytest.raises(chain.WalletBusy):
        _record(ledger, intent_id="next:urgent", nonce=1, chain_nonce=0)


def test_the_nonce_is_read_inside_the_lock(tmp_path):
    """A count taken before gas estimation is already old, and this lock does not stop other
    users of the same wallet."""
    ledger = _ledger(tmp_path)
    reads: list[str] = []

    account, intent = _signed(4)

    def read_nonce() -> int:
        reads.append("read")
        return 4

    row, created = ledger.record_signed(
        chain_id=CHAIN_ID,
        wallet=account.address,
        contract_address=ESCROW,
        intent_id="late:urgent",
        read_chain_nonce=read_nonce,
        sign=lambda allocated: intent,
    )
    assert created is True and row.nonce == 4 and reads == ["read"]


# --------------------------------------------------------------------------------------
# Concurrent resolvers
# --------------------------------------------------------------------------------------


def test_a_stale_resolver_cannot_overwrite_a_settled_row(tmp_path):
    """Two tx-resolve runs can read the same included row.

    If the second wrote its older opinion unconditionally, a confirmed transaction would be
    downgraded to reorged by a process that simply looked earlier.
    """
    path = tmp_path / "state" / "transactions.db"
    first = chain.TransactionLedger(path)
    row, _, _ = _record(first)
    row = first.set_status(row, chain.SEND_ATTEMPTED)
    included = first.set_status(row, chain.INCLUDED_SUCCESS, block_number=9, block_hash="0x" + "0e" * 32)

    second = chain.TransactionLedger(path)
    stale = second.rows()[0]
    assert stale.status == chain.INCLUDED_SUCCESS

    first.set_status(included, chain.CONFIRMED_SUCCESS, block_number=9, block_hash="0x" + "0e" * 32)

    with pytest.raises(chain.StaleStatus, match="but is now confirmed_success"):
        second.set_status(stale, chain.REORGED)

    assert second.rows()[0].status == chain.CONFIRMED_SUCCESS


# --------------------------------------------------------------------------------------
# Integrity of the recovery-critical columns
# --------------------------------------------------------------------------------------


def _create_deal_row(ledger, *, accept_by=1_800_000_000, price=10**14):
    """A row whose calldata really is a createDeal, so the terms can be bound to it."""
    from wrasse import escrow
    from wrasse.policy_hash import EMPTY_EVIDENCE_HASH, ENGINE_VERSION, PolicyPreimage

    account = Account.from_key(BUYER_KEY)
    preimage = PolicyPreimage(
        buyer=account.address,
        provider=Web3.to_checksum_address("0x" + "33" * 20),
        price=price,
        bond_bps=2_000,
        accept_by=accept_by,
        service_window=7_200,
        payout_delay=1_800,
        engine_version=ENGINE_VERSION,
        buyer_evidence_hash=EMPTY_EVIDENCE_HASH,
        provider_evidence_hash=EMPTY_EVIDENCE_HASH,
    )
    calldata = escrow.create_deal_calldata(
        Web3(), ESCROW,
        provider=preimage.provider, bond_bps=preimage.bond_bps, accept_by=accept_by,
        service_window=preimage.service_window, payout_delay=preimage.payout_delay,
        engine_version_hash="0x" + bytes(Web3.keccak(text=ENGINE_VERSION)).hex(),
        buyer_evidence_hash=preimage.buyer_evidence_hash,
        provider_evidence_hash=preimage.provider_evidence_hash,
    )
    transaction = {
        "chainId": CHAIN_ID, "nonce": 0, "to": ESCROW, "data": calldata, "value": price,
        "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**6, "gas": 250_000, "type": 2,
    }
    intent = chain.with_intent_context(
        chain.sign_transaction(account, transaction), accept_by=accept_by, preimage=preimage.as_dict()
    )
    row, _ = ledger.record_signed(
        chain_id=CHAIN_ID, wallet=account.address, contract_address=ESCROW,
        intent_id="terms:urgent", read_chain_nonce=lambda: 0, sign=lambda n: intent,
    )
    return row


def test_the_deadline_the_resolver_reads_is_the_one_the_transaction_commits(tmp_path):
    """`accept_by` decides whether a rebroadcast can still succeed.

    Moving it forward in the row while the signed bytes still carry the expired deadline would
    let a dead transaction be resent.
    """
    import dataclasses

    row = _create_deal_row(_ledger(tmp_path))
    chain.verify_row_integrity(row)

    with pytest.raises(chain.LedgerCorrupt, match="calldata commits to acceptBy"):
        chain.verify_row_integrity(dataclasses.replace(row, accept_by=row.accept_by + 10_000))


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", 1),
        ("bond_bps", 1),
        ("service_window", 60),
        ("payout_delay", 60),
        ("engine_version", "wrasse/9.9.9"),
        ("provider", "0x" + "aa" * 20),
        ("buyer_evidence_hash", "0x" + "bb" * 32),
        ("provider_evidence_hash", "0x" + "cc" * 32),
    ],
)
def test_the_stored_preimage_is_bound_to_the_transaction(tmp_path, field, value):
    """The preimage is what an audit trail says the deal committed to.

    Unbound, it could describe terms other than the ones actually funded.
    """
    import dataclasses

    row = _create_deal_row(_ledger(tmp_path))
    tampered = dict(row.preimage)
    tampered[field] = value

    with pytest.raises(chain.LedgerCorrupt, match="commits|preimage"):
        chain.verify_row_integrity(dataclasses.replace(row, preimage=tampered))


def test_calldata_with_the_right_selector_but_wrong_arguments_is_caught(tmp_path):
    import dataclasses
    from wrasse import escrow

    row = _create_deal_row(_ledger(tmp_path))
    forged = escrow.create_deal_calldata(
        Web3(), ESCROW, provider="0x" + "ee" * 20, bond_bps=1, accept_by=row.accept_by,
        service_window=1, payout_delay=1,
        engine_version_hash="0x" + "00" * 32,
        buyer_evidence_hash="0x" + "00" * 32, provider_evidence_hash="0x" + "00" * 32,
    )
    with pytest.raises(chain.LedgerCorrupt):
        chain.verify_row_integrity(dataclasses.replace(row, calldata=forged))


# --------------------------------------------------------------------------------------
# Reads retry, sends never do
# --------------------------------------------------------------------------------------


class _Flaky:
    def __init__(self, failures: int, error: Exception) -> None:
        self.calls = 0
        self.failures = failures
        self.error = error

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "answer"


def test_a_transient_read_failure_is_retried_within_a_bound(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(chain, "_sleep", slept.append)

    call = _Flaky(2, Boom("502 Bad Gateway"))
    assert chain._read(call, describe="probe") == "answer"
    assert call.calls == 3
    assert len(slept) == 2 and all(delay > 0 for delay in slept)


def test_a_persistent_read_failure_gives_up_and_says_so(monkeypatch):
    monkeypatch.setattr(chain, "_sleep", lambda _: None)
    call = _Flaky(99, Boom("429 Too Many Requests"))

    with pytest.raises(chain.RpcUnavailable, match="probe"):
        chain._read(call, describe="probe")
    assert call.calls == chain.READ_ATTEMPTS


def test_a_server_that_names_its_own_delay_is_obeyed(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(chain, "_sleep", slept.append)

    class Throttled(Exception):
        class response:  # noqa: N801 - mimics a requests response
            headers = {"Retry-After": "2.5"}

    call = _Flaky(1, Throttled())
    assert chain._read(call, describe="probe") == "answer"
    assert slept == [2.5]


# --------------------------------------------------------------------------------------
# The rehearsal fence
# --------------------------------------------------------------------------------------


class _Node:
    def __init__(self, client: str) -> None:
        self.client_version = client
        self.eth = FakeEth()


def test_the_rehearsal_confirmation_policy_refuses_a_real_endpoint():
    """A warning is not a boundary, and the chain id cannot fence this because the rehearsal
    deliberately runs on the production one."""
    with pytest.raises(chain.NotALocalChain, match="not a loopback"):
        chain.require_local_chain(_Node("anvil/v1.0"), "https://sepolia.base.org")


def test_the_rehearsal_confirmation_policy_refuses_a_real_node_on_loopback():
    """A tunnel or a proxy can put a real network on a local address."""
    with pytest.raises(chain.NotALocalChain, match="not local development software"):
        chain.require_local_chain(_Node("Geth/v1.14.0"), "http://127.0.0.1:8545")


def test_the_rehearsal_confirmation_policy_accepts_a_local_dev_node():
    assert "anvil" in chain.require_local_chain(_Node("anvil/v1.3.0"), "http://127.0.0.1:8545")


def test_the_buyer_is_bound_to_the_key_that_signed(tmp_path):
    """The buyer never appears in the calldata; the contract reads it as msg.sender.

    Left unbound, the stored preimage could name someone else entirely and the audit trail
    would describe a deal that did not happen.
    """
    import dataclasses

    row = _create_deal_row(_ledger(tmp_path))
    tampered = dict(row.preimage)
    tampered["buyer"] = Web3.to_checksum_address("0x" + "aa" * 20)

    with pytest.raises(chain.LedgerCorrupt, match="buyer"):
        chain.verify_row_integrity(dataclasses.replace(row, preimage=tampered))


def test_confirmation_checks_the_bytes_before_promoting_a_row(tmp_path):
    """A confirmed row is what reconciliation turns into memory."""
    import dataclasses

    ledger = _ledger(tmp_path)
    row = _create_deal_row(ledger)
    row = ledger.set_status(row, chain.SEND_ATTEMPTED)
    row = ledger.set_status(row, chain.INCLUDED_SUCCESS, block_number=3, block_hash="0x" + "0e" * 32)

    web3 = FakeWeb3()
    web3.eth.blocks[3] = {"hash": row.block_hash, "number": 3}
    web3.eth.blocks["safe"] = {"number": 9}
    assert chain.confirm(web3, row).status == chain.CONFIRMED_SUCCESS

    corrupted = dataclasses.replace(row, calldata="0xdeadbeef")
    with pytest.raises(chain.LedgerCorrupt):
        chain.confirm(web3, corrupted)


def test_only_a_first_send_may_release_its_nonce(tmp_path):
    """The precondition lives in the ledger, not in whichever caller happens to be right."""
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)

    with pytest.raises(chain.IllegalTransition, match="only a first send"):
        ledger.mark_unbroadcast(row, "not attempted yet")

    attempted = ledger.set_status(row, chain.SEND_ATTEMPTED, bump_attempts=True)
    released = ledger.mark_unbroadcast(attempted, "insufficient funds")
    assert released.status == chain.UNBROADCAST


def test_a_resent_transaction_can_never_release_its_nonce(tmp_path):
    """Once bytes have been accepted somewhere, a later refusal proves nothing."""
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    row = ledger.set_status(row, chain.SEND_ATTEMPTED, bump_attempts=True)
    row = ledger.set_status(row, chain.PENDING)
    row = ledger.set_status(row, chain.SEND_ATTEMPTED, bump_attempts=True)

    with pytest.raises(chain.IllegalTransition, match="after 2 attempts"):
        ledger.mark_unbroadcast(row, "refused on resend")

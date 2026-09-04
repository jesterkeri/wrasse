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
        table = self.latest if tag == "latest" else self.pending
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
        return raw


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
        chain_nonce=chain_nonce,
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


def test_only_a_confirmed_nonce_declares_the_slot_spent(tmp_path):
    ledger = _ledger(tmp_path)
    row, _, _ = _record(ledger)
    web3 = FakeWeb3()
    web3.eth.latest[row.wallet] = row.nonce + 1
    web3.eth.pending[row.wallet] = row.nonce + 1

    verdict = chain.resolve(web3, row)
    assert verdict.status == chain.NONCE_CONSUMED_OR_REPLACED
    assert verdict.status in chain.TERMINAL_STATUSES


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

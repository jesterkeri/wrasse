"""Only a receipt that survives all five checks may become memory.

The store is the entry's whole claim, so what gets into it matters more than what stays out.
Every refusal here is a case where something looked like evidence and was not.
"""

from __future__ import annotations

import pytest
from web3 import Web3

from wrasse import chain
from wrasse.evidence import MemoryWriter  # noqa: F401  (documents the protocol under test)
from wrasse.reconciler import (
    RELEASED_SIGNATURE,
    TIMEOUT_SIGNATURE,
    ChainVerificationError,
    verify_outcome,
)

CHAIN_ID = 84532
CONTRACT = Web3.to_checksum_address("0x" + "11" * 20)
BUYER = Web3.to_checksum_address("0x" + "44" * 20)
PROVIDER = Web3.to_checksum_address("0x" + "33" * 20)
STRANGER = Web3.to_checksum_address("0x" + "99" * 20)
TX = "0x" + "22" * 32
BLOCK = 123_456
BLOCK_HASH = "0x" + "ab" * 32

_STATES = ("Offered", "Accepted", "Delivered", "Released", "TimedOut", "Cancelled")


def _log(signature, deal_id, *, data=b"", address=CONTRACT, index=3):
    topics = [bytes(signature), deal_id.to_bytes(32, "big")]
    return {"address": address, "topics": topics, "data": data, "logIndex": index}


def _timeout_log(deal_id=7):
    return _log(TIMEOUT_SIGNATURE, deal_id)


def _release_log(deal_id=7, *, by_buyer=True, index=3):
    return _log(RELEASED_SIGNATURE, deal_id, data=(1 if by_buyer else 0).to_bytes(32, "big"), index=index)


class FakeEth:
    def __init__(self, *, logs, state="TimedOut", sender=BUYER, status=1, deal_readable=True,
                 block=BLOCK, block_hash=BLOCK_HASH, canonical_hash=None):
        self.chain_id = CHAIN_ID
        self.receipt = {
            "status": status,
            "blockNumber": block,
            "blockHash": block_hash,
            "transactionHash": bytes.fromhex(TX[2:]),
            "logs": logs,
        }
        self.transaction = {"to": CONTRACT, "from": sender}
        self.state = state
        self.deal_readable = deal_readable
        self.block_asked_for = None
        self.canonical_hash = block_hash if canonical_hash is None else canonical_hash

    def get_transaction_receipt(self, tx_hash):
        return self.receipt

    def get_transaction(self, tx_hash):
        return self.transaction

    def get_block(self, number):
        return {"timestamp": 1_788_000_000, "number": number, "hash": self.canonical_hash}

    def contract(self, address, abi):
        return _FakeContract(self)


class _FakeContract:
    def __init__(self, eth):
        self.functions = _FakeFunctions(eth)


class _FakeFunctions:
    def __init__(self, eth):
        self.eth = eth

    def deals(self, deal_id):
        return _FakeCall(self.eth)


class _FakeCall:
    def __init__(self, eth):
        self.eth = eth

    def call(self, block_identifier="latest"):
        if not self.eth.deal_readable:
            raise RuntimeError("archive node unavailable")
        self.eth.block_asked_for = block_identifier
        return [
            BUYER, PROVIDER, 10**14, 2 * 10**13, 0, 0, 0, 0, 0, 0,
            b"\x00" * 32, _STATES.index(self.eth.state),
        ]


class FakeWeb3:
    def __init__(self, **kwargs):
        self.eth = FakeEth(**kwargs)


class FakeLedger:
    """Stands in for the transaction ledger, which is check number one."""

    def __init__(self, status=chain.CONFIRMED_SUCCESS, present=True,
                 block_number=BLOCK, block_hash=BLOCK_HASH):
        self.status = status
        self.present = present
        self.block_number = block_number
        self.block_hash = block_hash

    def find_by_tx_hash(self, *, chain_id, tx_hash):
        if not self.present:
            return None
        return _Row(self.status, self.block_number, self.block_hash)


class _Row:
    def __init__(self, status, block_number=BLOCK, block_hash=BLOCK_HASH):
        self.status = status
        self.intent_id = "deal:0:claimTimeout"
        self.block_number = block_number
        self.block_hash = block_hash


@pytest.fixture(autouse=True)
def _skip_integrity(request, monkeypatch):
    """Row integrity has its own suite; here the ledger row is a stand-in.

    Opt out with `@pytest.mark.real_integrity` where the point is that reconciliation calls
    it. Proving the helper works is a different claim from proving this path invokes it, and
    a blanket patch meant deleting the production call changed no test at all.
    """

    if "real_integrity" in request.keywords:
        return
    monkeypatch.setattr(chain, "verify_row_integrity", lambda row: None)


def _verify(web3, ledger=None):
    return verify_outcome(
        web3, ledger or FakeLedger(), tx_hash=TX,
        expected_chain_id=CHAIN_ID, expected_contract=CONTRACT,
    )


@pytest.mark.parametrize(
    "logs,state,sender,expected",
    [
        ([_timeout_log()], "TimedOut", BUYER, "timeout_claimed_without_delivery"),
        ([_release_log(by_buyer=True)], "Released", BUYER, "delivered_and_released_by_buyer"),
        ([_release_log(by_buyer=False)], "Released", PROVIDER, "delivered_and_claimed_after_delay"),
    ],
)
def test_every_outcome_derives_from_its_own_log(logs, state, sender, expected):
    """The event type is read out of the receipt, never supplied by the caller."""
    event = _verify(FakeWeb3(logs=logs, state=state, sender=sender))
    assert event.event_type == expected
    assert event.deal_id == 7


def test_the_buyer_comes_from_contract_state_at_the_receipt_block():
    """Not from a log, and not from the tip. The deal as it stood when this happened."""
    web3 = FakeWeb3(logs=[_timeout_log()])
    event = _verify(web3)
    assert event.buyer == BUYER
    assert event.provider == PROVIDER
    assert web3.eth.block_asked_for == BLOCK


@pytest.mark.parametrize(
    "status",
    [chain.INCLUDED_SUCCESS, chain.PENDING, chain.SIGNED, chain.REORGED, chain.CONFIRMED_REVERTED],
)
def test_only_a_confirmed_success_may_become_memory(status):
    """A reverted transaction proves no outcome; an unconfirmed one may yet be undone."""
    with pytest.raises(ChainVerificationError, match="only a confirmed success"):
        _verify(FakeWeb3(logs=[_timeout_log()]), FakeLedger(status=status))


def test_a_transaction_this_build_never_sent_is_not_its_evidence():
    with pytest.raises(ChainVerificationError, match="not in this ledger"):
        _verify(FakeWeb3(logs=[_timeout_log()]), FakeLedger(present=False))


def test_a_receipt_carrying_two_outcomes_describes_more_than_one_thing():
    logs = [_timeout_log(), _release_log(index=4)]
    with pytest.raises(ChainVerificationError, match="carries 2 recognised outcomes"):
        _verify(FakeWeb3(logs=logs))


def test_a_receipt_carrying_no_recognised_outcome_is_not_evidence():
    with pytest.raises(ChainVerificationError, match="carries 0 recognised outcomes"):
        _verify(FakeWeb3(logs=[]))


def test_a_log_from_another_address_is_ignored():
    elsewhere = _log(TIMEOUT_SIGNATURE, 7, address=Web3.to_checksum_address("0x" + "ab" * 20))
    with pytest.raises(ChainVerificationError, match="carries 0 recognised"):
        _verify(FakeWeb3(logs=[elsewhere]))


def test_the_wrong_sender_is_refused():
    """A timeout is claimed by the buyer. Anyone else claiming it is not this story."""
    with pytest.raises(ChainVerificationError, match="must come from the buyer"):
        _verify(FakeWeb3(logs=[_timeout_log()], sender=STRANGER))


def test_a_deal_in_the_wrong_state_at_that_block_is_refused():
    with pytest.raises(ChainVerificationError, match="needs the deal TimedOut"):
        _verify(FakeWeb3(logs=[_timeout_log()], state="Accepted"))


def test_a_failed_historical_read_stops_rather_than_using_the_tip():
    """Falling back to current state would answer a different question in the same words."""
    with pytest.raises(ChainVerificationError, match="could not read deal 7 at block"):
        _verify(FakeWeb3(logs=[_timeout_log()], deal_readable=False))


def test_an_unsuccessful_receipt_is_refused():
    with pytest.raises(ChainVerificationError, match="not successful"):
        _verify(FakeWeb3(logs=[_timeout_log()], status=0))


# --------------------------------------------------------------------------------------
# A confirmation belongs to a fork, not to a transaction
# --------------------------------------------------------------------------------------


def test_a_receipt_reincluded_in_another_block_is_not_still_confirmed():
    """`confirmed_success` is terminal, so it outlives the fork it was earned on.

    A reorg puts the transaction back in the mempool and it is re-included in a newer block
    that nothing has waited on. The ledger row still says confirmed. Taking the receipt at
    that moment records a fact the chain has agreed to for seconds, under a rule that claims
    to accept only what it has agreed to for a hundred blocks.
    """

    web3 = FakeWeb3(logs=[_timeout_log()], block=BLOCK + 4, block_hash="0x" + "cd" * 32)
    with pytest.raises(ChainVerificationError, match="reorged and re-included"):
        _verify(web3)


def test_a_confirmation_on_an_abandoned_fork_is_refused():
    """Same block number, different chain. The receipt is honest; the fork is gone."""

    web3 = FakeWeb3(logs=[_timeout_log()], canonical_hash="0x" + "ef" * 32)
    with pytest.raises(ChainVerificationError, match="abandoned"):
        _verify(web3)


def test_a_confirmed_row_with_no_block_has_nothing_to_check_against():
    web3 = FakeWeb3(logs=[_timeout_log()])
    ledger = FakeLedger(block_number=None, block_hash=None)
    with pytest.raises(ChainVerificationError, match="no block"):
        _verify(web3, ledger)


def test_an_unreadable_block_stops_rather_than_assuming_it_is_canonical():
    web3 = FakeWeb3(logs=[_timeout_log()])

    def unavailable(number):
        raise RuntimeError("archive node unavailable")

    web3.eth.get_block = unavailable
    with pytest.raises(ChainVerificationError, match="still canonical"):
        _verify(web3)


@pytest.mark.real_integrity
def test_a_row_whose_bytes_do_not_match_its_claims_cannot_become_memory():
    """Reconciliation has to invoke the integrity check, not merely be near it.

    The ledger row is what says this build sent the transaction. A row whose recorded signed
    bytes do not hash to the transaction it names is not evidence that anything was sent, and
    memory built on it would be memory of somebody else's transaction.
    """

    corrupt = chain.LedgerRow(
        chain_id=CHAIN_ID,
        wallet=BUYER,
        contract_address=CONTRACT,
        intent_id="deal:0:claimTimeout",
        nonce=0,
        calldata="0x",
        value_wei=0,
        max_fee_wei=1,
        max_priority_wei=1,
        gas_limit=21_000,
        accept_by=0,
        preimage={},
        tx_hash=TX,
        raw="0x02f8",  # not a transaction, and certainly not the one named above
        status=chain.CONFIRMED_SUCCESS,
        block_number=BLOCK,
        block_hash=BLOCK_HASH,
        attempts=1,
        last_error=None,
        created_at="2026-09-05T00:00:00+00:00",
        updated_at="2026-09-05T00:00:00+00:00",
    )

    class Corrupted(FakeLedger):
        def find_by_tx_hash(self, *, chain_id, tx_hash):
            return corrupt

    with pytest.raises(Exception) as raised:
        _verify(FakeWeb3(logs=[_timeout_log()]), Corrupted())
    assert not isinstance(raised.value, ChainVerificationError) or "integrity" in str(raised.value), (
        f"the row was accepted or refused for the wrong reason: {raised.value}"
    )

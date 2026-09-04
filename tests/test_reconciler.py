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

_STATES = ("Offered", "Accepted", "Delivered", "Released", "TimedOut", "Cancelled")


def _log(signature, deal_id, *, data=b"", address=CONTRACT, index=3):
    topics = [bytes(signature), deal_id.to_bytes(32, "big")]
    return {"address": address, "topics": topics, "data": data, "logIndex": index}


def _timeout_log(deal_id=7):
    return _log(TIMEOUT_SIGNATURE, deal_id)


def _release_log(deal_id=7, *, by_buyer=True, index=3):
    return _log(RELEASED_SIGNATURE, deal_id, data=(1 if by_buyer else 0).to_bytes(32, "big"), index=index)


class FakeEth:
    def __init__(self, *, logs, state="TimedOut", sender=BUYER, status=1, deal_readable=True):
        self.chain_id = CHAIN_ID
        self.receipt = {
            "status": status,
            "blockNumber": BLOCK,
            "transactionHash": bytes.fromhex(TX[2:]),
            "logs": logs,
        }
        self.transaction = {"to": CONTRACT, "from": sender}
        self.state = state
        self.deal_readable = deal_readable
        self.block_asked_for = None

    def get_transaction_receipt(self, tx_hash):
        return self.receipt

    def get_transaction(self, tx_hash):
        return self.transaction

    def get_block(self, number):
        return {"timestamp": 1_788_000_000, "number": number}

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

    def __init__(self, status=chain.CONFIRMED_SUCCESS, present=True):
        self.status = status
        self.present = present

    def find_by_tx_hash(self, *, chain_id, tx_hash):
        if not self.present:
            return None
        return _Row(self.status)


class _Row:
    def __init__(self, status):
        self.status = status
        self.intent_id = "deal:0:claimTimeout"


@pytest.fixture(autouse=True)
def _skip_integrity(monkeypatch):
    """Row integrity has its own suite; here the ledger row is a stand-in."""
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

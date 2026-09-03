from __future__ import annotations

import pytest
from web3 import Web3

from rapport.reconciler import ChainVerificationError, TIMEOUT_SIGNATURE, verify_timeout_claim


CONTRACT = "0x1111111111111111111111111111111111111111"
PROVIDER = "0x3333333333333333333333333333333333333333"
TX_HASH = "0x" + "22" * 32


class FakeDealCall:
    def __init__(self, deal):
        self.deal = deal

    def call(self, **kwargs):
        return self.deal


class FakeFunctions:
    def __init__(self, deal):
        self.deal = deal

    def deals(self, deal_id):
        assert deal_id == 7
        return FakeDealCall(self.deal)


class FakeContract:
    def __init__(self, deal):
        self.functions = FakeFunctions(deal)


class FakeEth:
    def __init__(self):
        self.chain_id = 84532
        self.receipt = {
            "status": 1,
            "transactionHash": bytes.fromhex("22" * 32),
            "blockNumber": 900,
            "logs": [{
                "address": CONTRACT,
                "topics": [TIMEOUT_SIGNATURE, (7).to_bytes(32, "big")],
                "logIndex": 2,
            }],
        }
        self.transaction = {"to": CONTRACT}
        self.deal = (
            "0x4444444444444444444444444444444444444444",
            PROVIDER,
            1_000,
            200,
            1,
            2,
            3,
            4,
            5,
            0,
            bytes(32),
            4,
        )
        self.block = {"timestamp": 1_788_436_800}

    def get_transaction_receipt(self, tx_hash):
        assert tx_hash == TX_HASH
        return self.receipt

    def get_transaction(self, tx_hash):
        assert tx_hash == TX_HASH
        return self.transaction

    def contract(self, **kwargs):
        assert Web3.to_checksum_address(kwargs["address"]) == Web3.to_checksum_address(CONTRACT)
        return FakeContract(self.deal)

    def get_block(self, number):
        assert number == 900
        return self.block


class FakeWeb3:
    def __init__(self):
        self.eth = FakeEth()


def verify(web3):
    return verify_timeout_claim(
        web3,
        tx_hash=TX_HASH,
        expected_chain_id=84532,
        expected_contract=CONTRACT,
        expected_deal_id=7,
        expected_provider=PROVIDER,
    )


def test_valid_receipt_becomes_neutral_evidence():
    event = verify(FakeWeb3())
    assert event.event_type == "timeout_claimed_without_delivery"
    assert event.deal_id == 7
    assert event.tx_hash == TX_HASH
    assert event.log_index == 2


def test_wrong_chain_is_rejected():
    web3 = FakeWeb3()
    web3.eth.chain_id = 8453
    with pytest.raises(ChainVerificationError, match="chain id"):
        verify(web3)


def test_failed_receipt_is_rejected():
    web3 = FakeWeb3()
    web3.eth.receipt["status"] = 0
    with pytest.raises(ChainVerificationError, match="not successful"):
        verify(web3)


def test_wrong_transaction_target_is_rejected():
    web3 = FakeWeb3()
    web3.eth.transaction["to"] = "0x5555555555555555555555555555555555555555"
    with pytest.raises(ChainVerificationError, match="target"):
        verify(web3)


def test_wrong_contract_log_is_rejected():
    web3 = FakeWeb3()
    web3.eth.receipt["logs"][0]["address"] = "0x5555555555555555555555555555555555555555"
    with pytest.raises(ChainVerificationError, match="no TimeoutClaimed"):
        verify(web3)


def test_wrong_provider_is_rejected():
    web3 = FakeWeb3()
    deal = list(web3.eth.deal)
    deal[1] = "0x5555555555555555555555555555555555555555"
    web3.eth.deal = tuple(deal)
    with pytest.raises(ChainVerificationError, match="provider"):
        verify(web3)

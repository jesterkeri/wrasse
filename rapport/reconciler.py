"""Verify Base receipts before turning them into durable Sibyl evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from web3 import Web3

from .evidence import ChainEvent, MemoryWriter, PersistResult, persist_verified_event


TIMEOUT_SIGNATURE = Web3.keccak(text="TimeoutClaimed(uint256)")

DEALS_ABI = [{
    "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
    "name": "deals",
    "outputs": [
        {"internalType": "address", "name": "buyer", "type": "address"},
        {"internalType": "address", "name": "provider", "type": "address"},
        {"internalType": "uint256", "name": "price", "type": "uint256"},
        {"internalType": "uint256", "name": "providerBond", "type": "uint256"},
        {"internalType": "uint64", "name": "acceptBy", "type": "uint64"},
        {"internalType": "uint64", "name": "acceptedAt", "type": "uint64"},
        {"internalType": "uint64", "name": "serviceWindow", "type": "uint64"},
        {"internalType": "uint64", "name": "deadline", "type": "uint64"},
        {"internalType": "uint64", "name": "payoutDelay", "type": "uint64"},
        {"internalType": "uint64", "name": "payoutAvailableAt", "type": "uint64"},
        {"internalType": "bytes32", "name": "policyHash", "type": "bytes32"},
        {"internalType": "enum RapportEscrow.State", "name": "state", "type": "uint8"},
    ],
    "stateMutability": "view",
    "type": "function",
}]


class ChainVerificationError(RuntimeError):
    """A receipt does not prove the neutral event Rapport was asked to ingest."""


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def _hex(value: Any) -> str:
    raw = bytes(value)
    return "0x" + raw.hex()


def verify_timeout_claim(
    web3: Web3,
    *,
    tx_hash: str,
    expected_chain_id: int,
    expected_contract: str,
    expected_deal_id: int,
    expected_provider: str,
) -> ChainEvent:
    """Return canonical evidence only when every receipt invariant matches."""

    if web3.eth.chain_id != expected_chain_id:
        raise ChainVerificationError("connected chain id does not match configuration")
    contract_address = Web3.to_checksum_address(expected_contract)
    provider = Web3.to_checksum_address(expected_provider)
    receipt = web3.eth.get_transaction_receipt(tx_hash)
    if _field(receipt, "status") != 1:
        raise ChainVerificationError("transaction receipt is not successful")
    transaction = web3.eth.get_transaction(tx_hash)
    if Web3.to_checksum_address(_field(transaction, "to")) != contract_address:
        raise ChainVerificationError("transaction target is not the configured contract")

    matching_log = None
    for log in _field(receipt, "logs"):
        topics = _field(log, "topics")
        if (
            Web3.to_checksum_address(_field(log, "address")) == contract_address
            and topics
            and bytes(topics[0]) == bytes(TIMEOUT_SIGNATURE)
        ):
            if matching_log is not None:
                raise ChainVerificationError("receipt contains multiple timeout events")
            matching_log = log
    if matching_log is None:
        raise ChainVerificationError("receipt has no TimeoutClaimed event from the contract")

    topics = _field(matching_log, "topics")
    if len(topics) != 2:
        raise ChainVerificationError("timeout event has an invalid topic count")
    deal_id = int.from_bytes(bytes(topics[1]), "big")
    if deal_id != expected_deal_id:
        raise ChainVerificationError("event deal id does not match")

    contract = web3.eth.contract(address=contract_address, abi=DEALS_ABI)
    deal = contract.functions.deals(deal_id).call(block_identifier=_field(receipt, "blockNumber"))
    if Web3.to_checksum_address(deal[1]) != provider:
        raise ChainVerificationError("stored deal provider does not match")
    if int(deal[11]) != 4:  # RapportEscrow.State.TimedOut
        raise ChainVerificationError("deal is not in TimedOut state at the receipt block")

    block = web3.eth.get_block(_field(receipt, "blockNumber"))
    observed_at = datetime.fromtimestamp(_field(block, "timestamp"), UTC).isoformat()
    return ChainEvent(
        chain_id=expected_chain_id,
        contract_address=contract_address,
        tx_hash=_hex(_field(receipt, "transactionHash")),
        log_index=int(_field(matching_log, "logIndex")),
        block_number=int(_field(receipt, "blockNumber")),
        event_type="timeout_claimed_without_delivery",
        deal_id=deal_id,
        provider=provider,
        observed_at=observed_at,
    )


def reconcile_timeout_claim(
    memory: MemoryWriter,
    web3: Web3,
    **expected: Any,
) -> PersistResult:
    event = verify_timeout_claim(web3, **expected)
    return persist_verified_event(memory, event)


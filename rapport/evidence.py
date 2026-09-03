"""Canonical, idempotent persistence for verified Base event evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from eth_abi import encode
from web3 import Web3
from sibyl_memory_client import NotFoundError


CHAIN_EVENT_CATEGORY = "chain_event"


class EventConflict(RuntimeError):
    """The same onchain event id was previously stored with different data."""


class InvalidEvidence(ValueError):
    """An evidence field is not a valid canonical chain value."""


class MemoryWriter(Protocol):
    def get_entity(self, category: str, name: str) -> dict[str, Any]: ...

    def set_entity(
        self,
        category: str,
        name: str,
        body: dict[str, Any] | list[Any],
        *,
        status: str | None = None,
    ) -> dict[str, Any]: ...

    def write_event(
        self,
        *,
        evaluated: Any = None,
        acted: Any = None,
        forward: Any = None,
        extra: Any = None,
        ts: str | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class ChainEvent:
    chain_id: int
    contract_address: str
    tx_hash: str
    log_index: int
    block_number: int
    event_type: str
    deal_id: int
    provider: str
    observed_at: str

    def canonical_body(self) -> dict[str, Any]:
        validate_chain_event(self)
        body = asdict(self)
        body["contract_address"] = Web3.to_checksum_address(self.contract_address)
        body["provider"] = Web3.to_checksum_address(self.provider)
        body["tx_hash"] = _hex32(self.tx_hash)
        body["event_id"] = event_id(self)
        return body


@dataclass(frozen=True)
class PersistResult:
    entity: dict[str, Any]
    created: bool


def _bytes32(value: str) -> bytes:
    try:
        raw = bytes.fromhex(value.removeprefix("0x"))
    except ValueError as exc:
        raise InvalidEvidence("expected a hexadecimal bytes32 value") from exc
    if len(raw) != 32:
        raise InvalidEvidence("expected a 32-byte value")
    return raw


def _hex32(value: str) -> str:
    return "0x" + _bytes32(value).hex()


def validate_chain_event(event: ChainEvent) -> None:
    if event.chain_id <= 0:
        raise InvalidEvidence("chain_id must be positive")
    if not Web3.is_address(event.contract_address):
        raise InvalidEvidence("contract_address is not an EVM address")
    if not Web3.is_address(event.provider):
        raise InvalidEvidence("provider is not an EVM address")
    _bytes32(event.tx_hash)
    for name in ("log_index", "block_number", "deal_id"):
        if getattr(event, name) < 0:
            raise InvalidEvidence(f"{name} cannot be negative")
    if not event.event_type or not event.observed_at:
        raise InvalidEvidence("event_type and observed_at are required")


def event_id(event: ChainEvent) -> str:
    """keccak256(abi.encode(chainId, contract, txHash, logIndex))."""

    validate_chain_event(event)
    payload = encode(
        ["uint256", "address", "bytes32", "uint256"],
        [
            event.chain_id,
            Web3.to_checksum_address(event.contract_address),
            _bytes32(event.tx_hash),
            event.log_index,
        ],
    )
    return "0x" + bytes(Web3.keccak(payload)).hex()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def persist_verified_event(memory: MemoryWriter, event: ChainEvent) -> PersistResult:
    """Write a verified event once and reject a divergent replay."""

    body = event.canonical_body()
    identifier = body["event_id"]
    try:
        existing = memory.get_entity(CHAIN_EVENT_CATEGORY, identifier)
    except NotFoundError:
        existing = None

    if existing is not None:
        if canonical_json(existing["body"]) != canonical_json(body):
            raise EventConflict(identifier)
        return PersistResult(existing, created=False)

    entity = memory.set_entity(
        CHAIN_EVENT_CATEGORY,
        identifier,
        body,
        status="verified",
    )
    memory.write_event(
        evaluated={"source": "base_receipt", "verification": "passed"},
        acted=[f"ingested chain event {identifier}"],
        extra={
            "event_id": identifier,
            "deal_id": event.deal_id,
            "event_type": event.event_type,
        },
        ts=event.observed_at,
    )
    return PersistResult(entity, created=True)

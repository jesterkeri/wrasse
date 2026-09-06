"""Canonical, idempotent persistence for verified Base event evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from eth_abi import encode
from web3 import Web3
from sibyl_memory_client import NotFoundError

from .constants import SUBJECTS_OF


CHAIN_EVENT_CATEGORY = "chain_event"

#: The journal is a projection over the canonical entities, and this marks which entities it
#: already carries. See `persist_verified_event` for why the marker is a third write.
JOURNAL_MARKER_CATEGORY = "chain_event_journalled"

#: Closed and ABI-derived. An event type outside this set never came from a log this build
#: recognises, so it can never become evidence.
#:
#: `SUBJECTS_OF` says whose conduct a receipt is evidence about. It is re-exported from
#: `constants` rather than defined here, because it is consumed on the pricing path and a table
#: that decides a term belongs to the digest claiming to cover the term. The reasoning behind
#: every entry is written out there.
#:
#: `VALENCE_OF` lives beside it in `constants` and is **not** re-exported through this module.
#: `dimensions` imports it from the source directly. This comment used to claim both were
#: re-exported here, which would have sent anyone following it into an `ImportError`.
EVENT_TYPES = frozenset(SUBJECTS_OF)


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

    def list_entities(self, category: str | None = None, *, status: str | None = None,
                      limit: int = 100) -> list[dict[str, Any]]: ...

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
    buyer: str
    provider: str
    observed_at: str

    def canonical_body(self) -> dict[str, Any]:
        validate_chain_event(self)
        body = asdict(self)
        body["contract_address"] = Web3.to_checksum_address(self.contract_address)
        body["buyer"] = Web3.to_checksum_address(self.buyer)
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
    for label in ("buyer", "provider"):
        if not Web3.is_address(getattr(event, label)):
            raise InvalidEvidence(f"{label} is not an EVM address")
    if event.buyer.lower() == event.provider.lower():
        raise InvalidEvidence("buyer and provider must differ")
    _bytes32(event.tx_hash)
    for name in ("log_index", "block_number", "deal_id"):
        if getattr(event, name) < 0:
            raise InvalidEvidence(f"{name} cannot be negative")
    if event.event_type not in EVENT_TYPES:
        raise InvalidEvidence(f"{event.event_type!r} is not a recognised event type")
    if not event.observed_at:
        raise InvalidEvidence("observed_at is required")


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
    """Write a verified event, and repair a journal left short by an earlier crash.

    Three writes, in this order:

    1. the canonical WARM entity, which is the fact itself
    2. the journal projection, which is the timeline
    3. a WARM marker saying the timeline already carries this one

    A crash between 1 and 2 is repaired on the next replay, because the marker is missing and
    the append is attempted again. A crash between 2 and 3 means replay appends a second time,
    and that is accepted rather than prevented: Sibyl offers no atomic or idempotent journal
    key, so "exactly once" is not a promise this build can keep. Consumers deduplicate the
    journal by `event_id` instead. **Exactly once applies to the canonical entity, not to
    journal rows.**
    """

    body = event.canonical_body()
    identifier = body["event_id"]
    try:
        existing = memory.get_entity(CHAIN_EVENT_CATEGORY, identifier)
    except NotFoundError:
        existing = None

    if existing is not None:
        if canonical_json(existing["body"]) != canonical_json(body):
            raise EventConflict(identifier)
        entity = existing
        created = False
    else:
        entity = memory.set_entity(CHAIN_EVENT_CATEGORY, identifier, body, status="verified")
        created = True

    # Reached whether or not the entity is new, so a journal left short by a crash between the
    # two writes is filled in on replay rather than staying missing forever.
    try:
        memory.get_entity(JOURNAL_MARKER_CATEGORY, identifier)
    except NotFoundError:
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
        memory.set_entity(
            JOURNAL_MARKER_CATEGORY, identifier, {"event_id": identifier}, status="verified"
        )

    return PersistResult(entity, created=created)

"""A memory that knows whose it is.

Two things live here that the rest of the project depends on and could not get from a plain
`MemoryClient`.

**Identity.** A store carries a record of the role, owner and deployment it was created for,
checked every time it opens. Two different file paths are not isolation: the paths can simply
be exchanged, and because both sides receive the same receipts the swap would be invisible
from the outside. A role passed as an argument is an instruction; the record is a fact about
the file.

**An exact index.** `search_entities` is fuzzy and capped, and it can miss a matching row
without ever reaching its limit, so validating what it returns proves nothing about what it
left out. Terms built on it could price a counterparty on half its history while looking
complete. The pricing path therefore reads a manifest of canonical event ids and resolves each
one by exact lookup. Fuzzy search remains, for discovery and display, and never sets a price.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sibyl_memory_client import MemoryClient, NotFoundError, SibylMemoryError
from web3 import Web3

from .evidence import CHAIN_EVENT_CATEGORY, EVENT_TYPES, ChainEvent, event_id, persist_verified_event
from .memory_gate import MemoryRequired

STORE_SCHEMA_VERSION = 1

IDENTITY_CATEGORY = "store_identity"
INDEX_CATEGORY = "counterparty_index"
PERSONA_CATEGORY = "persona_commitment"

#: Which field of a neutral receipt names the counterparty, from each owner's point of view.
#: Both stores hold both receipts; what differs is who each store is reading them about.
_COUNTERPARTY_FIELD = {"buyer": "provider", "provider": "buyer"}


class StoreError(RuntimeError):
    """The store is not the one this configuration describes, or holds something unexplainable."""


class EvidenceRejected(StoreError):
    """A stored row cannot be explained, so nothing may be priced against this store.

    Deliberately fatal rather than skipped. Dropping the row quietly would price the deal on a
    history nobody can reconstruct, which is the failure the exact index exists to prevent.
    """


@dataclass(frozen=True)
class StoreIdentity:
    schema_version: int
    role: str
    owner_address: str
    chain_id: int
    escrow_address: str

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role": self.role,
            "owner_address": self.owner_address,
            "chain_id": self.chain_id,
            "escrow_address": self.escrow_address,
        }


@dataclass(frozen=True)
class Recall:
    """What one side remembers about one counterparty, and how sure it is of that."""

    counterparty: str
    owner_role: str
    evidence: tuple[dict[str, Any], ...]
    verdict: str

    @property
    def is_cold_start(self) -> bool:
        return not self.evidence


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _address(value: str) -> str:
    return Web3.to_checksum_address(value)


class WrasseStore:
    """One side's memory, with its own identity and its own index."""

    def __init__(self, memory: MemoryClient, identity: StoreIdentity) -> None:
        self._memory = memory
        self.identity = identity

    # -- opening --------------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        role: str,
        owner_address: str,
        chain_id: int,
        escrow_address: str,
    ) -> "WrasseStore":
        """Open a store, or refuse if it was created for something else."""

        if role not in _COUNTERPARTY_FIELD:
            raise StoreError(f"{role!r} is not a role this build knows")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        wanted = StoreIdentity(
            schema_version=STORE_SCHEMA_VERSION,
            role=role,
            owner_address=_address(owner_address),
            chain_id=chain_id,
            escrow_address=_address(escrow_address),
        )

        try:
            memory = MemoryClient.local(path)
            try:
                found = memory.get_entity(IDENTITY_CATEGORY, "self")["body"]
            except NotFoundError:
                memory.set_entity(
                    IDENTITY_CATEGORY, "self", {**wanted.body(), "created_at": _now()},
                    status="verified",
                )
                return cls(memory, wanted)
        except SibylMemoryError as error:
            raise MemoryRequired(f"cannot open the {role} memory at {path}") from error

        for field, expected in wanted.body().items():
            if found.get(field) != expected:
                raise StoreError(
                    f"{path} was created as {found.get('role')} {found.get('owner_address')} "
                    f"on chain {found.get('chain_id')}; this run wants {role} "
                    f"{wanted.owner_address} on chain {chain_id}. Its {field} disagrees. "
                    "A store cannot change whose memory it is."
                )
        return cls(memory, wanted)

    @property
    def memory(self) -> MemoryClient:
        """The underlying client, for display paths and for the fuzzy search that is not pricing."""

        return self._memory

    # -- the persona, committed before there is anything to tune it against ------------------

    def commit_persona(self, *, name: str, digest: str) -> None:
        """Record which persona this store belongs to, before it holds any evidence.

        Printing a value at startup proves it at run time. Recording it here, and refusing to
        do so once the store holds a receipt, proves it predates the evidence, which is the
        claim actually being made.
        """

        try:
            existing = self._memory.get_entity(PERSONA_CATEGORY, "self")["body"]
        except NotFoundError:
            if self._memory.list_entities(CHAIN_EVENT_CATEGORY, limit=1):
                raise StoreError(
                    "this store already holds evidence, so a persona committed now could have "
                    "been chosen with that evidence in view"
                ) from None
            self._memory.set_entity(
                PERSONA_CATEGORY, "self",
                {"name": name, "sha256": digest, "created_at": _now()},
                status="verified",
            )
            return

        if existing.get("sha256") != digest:
            raise StoreError(
                f"the persona changed after it was committed: {existing.get('sha256')} became "
                f"{digest}"
            )

    def persona_commitment(self) -> dict[str, Any] | None:
        try:
            return self._memory.get_entity(PERSONA_CATEGORY, "self")["body"]
        except NotFoundError:
            return None

    # -- writing --------------------------------------------------------------------------

    def _index_name(self, counterparty: str) -> str:
        return (
            f"{self.identity.chain_id}:{self.identity.escrow_address.lower()}:"
            f"{self.identity.role}:{_address(counterparty).lower()}"
        )

    def counterparty_of(self, body: dict[str, Any]) -> str | None:
        """Who this receipt is about, from this store's point of view."""

        other = body.get(_COUNTERPARTY_FIELD[self.identity.role])
        owner = body.get(self.identity.role)
        if other is None or owner is None:
            return None
        if _address(owner) != self.identity.owner_address:
            return None  # a real receipt, but not one about this owner's dealings
        return _address(other)

    def ingest(self, event: ChainEvent) -> dict[str, Any]:
        """Persist the canonical fact, then index it under this store's own relationship."""

        result = persist_verified_event(self._memory, event)
        body = result.entity["body"]
        counterparty = self.counterparty_of(body)
        if counterparty is not None:
            self._add_to_index(counterparty, body["event_id"])
        return {"created": result.created, "counterparty": counterparty, "body": body}

    def _add_to_index(self, counterparty: str, identifier: str) -> None:
        """The index is a projection, so adding twice is harmless and adding late is a repair."""

        name = self._index_name(counterparty)
        try:
            body = self._memory.get_entity(INDEX_CATEGORY, name)["body"]
            ids = set(body.get("event_ids", []))
        except NotFoundError:
            ids = set()
        ids.add(identifier)
        self._memory.set_entity(
            INDEX_CATEGORY,
            name,
            {
                "chain_id": self.identity.chain_id,
                "contract_address": self.identity.escrow_address,
                "owner_role": self.identity.role,
                "counterparty": _address(counterparty),
                "event_ids": sorted(ids),
            },
            status="verified",
        )

    def repair_index(self) -> int:
        """Put back any entity the index has lost, and report how many.

        A missing index entry is repairable; a missing entity is not. That asymmetry is the
        whole reason the entities stay canonical and the index stays a projection.
        """

        repaired = 0
        for row in self._memory.list_entities(CHAIN_EVENT_CATEGORY, limit=1000):
            body = row.get("body")
            if not isinstance(body, dict):
                continue
            counterparty = self.counterparty_of(body)
            if counterparty is None:
                continue
            name = self._index_name(counterparty)
            try:
                indexed = set(self._memory.get_entity(INDEX_CATEGORY, name)["body"]["event_ids"])
            except NotFoundError:
                indexed = set()
            if body["event_id"] not in indexed:
                self._add_to_index(counterparty, body["event_id"])
                repaired += 1
        return repaired

    # -- reading, on the path that sets prices -----------------------------------------------

    def recall(self, counterparty: str) -> Recall:
        """Every receipt this side holds about this counterparty, exactly and completely.

        Resolved from the index by exact lookup, never by search. Any row that cannot be fully
        explained stops the recall rather than being dropped, because terms computed on a
        quietly shorter history are worse than no terms at all.
        """

        counterparty = _address(counterparty)
        try:
            body = self._memory.get_entity(INDEX_CATEGORY, self._index_name(counterparty))["body"]
            identifiers = list(body.get("event_ids", []))
        except NotFoundError:
            identifiers = []
        except SibylMemoryError as error:
            raise MemoryRequired("Sibyl Memory is required to generate deal terms") from error

        evidence = []
        for identifier in sorted(identifiers):
            try:
                row = self._memory.get_entity(CHAIN_EVENT_CATEGORY, identifier)
            except NotFoundError:
                raise EvidenceRejected(
                    f"{identifier} is indexed but its record is gone. The index is a projection "
                    "and can be repaired; a missing record cannot."
                ) from None
            except SibylMemoryError as error:
                raise MemoryRequired("Sibyl Memory is required to generate deal terms") from error
            evidence.append(self._validated(row, identifier, counterparty))

        verdict = "empty_store" if not identifiers else ("no_match" if not evidence else "match")
        return Recall(counterparty, self.identity.role, tuple(evidence), verdict)

    def _validated(self, row: dict[str, Any], identifier: str, counterparty: str) -> dict[str, Any]:
        body = row.get("body")
        if not isinstance(body, dict):
            raise EvidenceRejected(f"{identifier} has no readable body")

        if row.get("status") != "verified":
            raise EvidenceRejected(f"{identifier} is {row.get('status')!r}, not verified")

        for field, expected in (
            ("chain_id", self.identity.chain_id),
            ("contract_address", self.identity.escrow_address),
        ):
            actual = body.get(field)
            if field == "contract_address":
                actual = _address(actual) if actual else actual
            if actual != expected:
                raise EvidenceRejected(
                    f"{identifier} names {field}={actual!r}, this run is configured for {expected!r}"
                )

        if body.get("event_type") not in EVENT_TYPES:
            raise EvidenceRejected(
                f"{identifier} carries event type {body.get('event_type')!r}, which is not one "
                "this build derives from a log"
            )

        for role in ("buyer", "provider"):
            if not body.get(role):
                raise EvidenceRejected(f"{identifier} does not name a {role}")
        if _address(body[self.identity.role]) != self.identity.owner_address:
            raise EvidenceRejected(
                f"{identifier} names {body[self.identity.role]} as the {self.identity.role}, "
                f"but this store belongs to {self.identity.owner_address}"
            )
        if _address(body[_COUNTERPARTY_FIELD[self.identity.role]]) != counterparty:
            raise EvidenceRejected(f"{identifier} is not about {counterparty}")

        for field in ("deal_id", "block_number", "log_index"):
            value = body.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise EvidenceRejected(f"{identifier} has a malformed {field}")
        try:
            datetime.fromisoformat(str(body.get("observed_at")))
        except (TypeError, ValueError) as error:
            raise EvidenceRejected(f"{identifier} has an unreadable observed_at") from error

        # The name is the fact's own fingerprint, so a body that no longer produces it has been
        # edited since it was verified.
        #
        # Note exactly what this proves and what it does not. `event_id` covers the transaction
        # coordinates: chain, contract, transaction hash and log index. It binds the record to
        # the receipt it came from. It does NOT cover `deal_id`, `block_number` or
        # `observed_at`, which cannot be checked offline without re-reading the chain, and the
        # trust model deliberately refuses to rescan before every quote. Those fields are
        # checked for shape above and are trusted because the reconciler put them there.
        try:
            recomputed = event_id(ChainEvent(**{
                key: body[key] for key in (
                    "chain_id", "contract_address", "tx_hash", "log_index", "block_number",
                    "event_type", "deal_id", "buyer", "provider", "observed_at",
                )
            }))
        except (KeyError, TypeError, ValueError) as error:
            raise EvidenceRejected(f"{identifier} is not a canonical record: {error}") from error
        if recomputed != identifier:
            raise EvidenceRejected(
                f"{identifier} recomputes to {recomputed}; its contents changed after it was stored"
            )
        return body


def persona_digest(path: Path | str) -> tuple[str, dict[str, Any]]:
    """The committed persona, and the hash that proves it is the one on disk."""

    raw = Path(path).read_bytes()
    document = json.loads(raw.decode("utf-8"))
    return hashlib.sha256(raw).hexdigest(), document

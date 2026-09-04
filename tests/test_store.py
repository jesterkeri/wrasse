"""A memory that knows whose it is, and an index that cannot quietly forget.

Two claims are tested here, and both are load-bearing for the whole entry.

The stores cannot be swapped. Two different file paths are not isolation, because paths can be
exchanged, and since both stores receive the same receipts the swap would look like nothing at
all from the outside.

And the pricing path reads an exact index rather than a search. Fuzzy search can miss a row
without reaching its limit, so terms built on it could price a counterparty on half its history
while appearing complete.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sibyl_memory_client import MemoryClient

from wrasse.evidence import CHAIN_EVENT_CATEGORY, ChainEvent
from wrasse.store import (
    INDEX_CATEGORY,
    EvidenceRejected,
    StoreError,
    WrasseStore,
)

BUYER = "0x4444444444444444444444444444444444444444"
PROVIDER = "0x3333333333333333333333333333333333333333"
STRANGER = "0x9999999999999999999999999999999999999999"
ESCROW = "0x1111111111111111111111111111111111111111"
CHAIN_ID = 84532


def _event(**overrides) -> ChainEvent:
    base = dict(
        chain_id=CHAIN_ID,
        contract_address=ESCROW,
        tx_hash="0x" + "22" * 32,
        log_index=3,
        block_number=99,
        event_type="timeout_claimed_without_delivery",
        deal_id=7,
        buyer=BUYER,
        provider=PROVIDER,
        observed_at=datetime(2026, 9, 4, tzinfo=UTC).isoformat(),
    )
    base.update(overrides)
    return ChainEvent(**base)


def _open(tmp_path, role=None, owner=None, name="store.db", **overrides):
    arguments = {
        "role": role or "buyer",
        "owner_address": owner or BUYER,
        "chain_id": CHAIN_ID,
        "escrow_address": ESCROW,
    }
    arguments.update(overrides)
    return WrasseStore.open(tmp_path / name, **arguments)


@pytest.fixture
def buyer_store(tmp_path):
    return _open(tmp_path, "buyer", BUYER, "buyer.db")


@pytest.fixture
def provider_store(tmp_path):
    return _open(tmp_path, "provider", PROVIDER, "provider.db")


# --------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------


def test_a_store_records_whose_it_is_on_first_open(tmp_path):
    store = _open(tmp_path, "buyer", BUYER)
    assert store.identity.role == "buyer"

    reopened = _open(tmp_path, "buyer", BUYER)
    assert reopened.identity == store.identity


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"role": "provider", "owner_address": PROVIDER}, "role"),
        ({"owner_address": STRANGER}, "owner_address"),
        ({"chain_id": 1}, "chain_id"),
        ({"escrow_address": STRANGER}, "escrow_address"),
    ],
)
def test_a_store_cannot_change_whose_memory_it_is(tmp_path, overrides, reason):
    """The anti-swap. Two paths are not isolation; the record in the file is."""
    _open(tmp_path, "buyer", BUYER)
    with pytest.raises(StoreError, match=reason):
        _open(tmp_path, **overrides)


def test_an_unknown_role_is_refused(tmp_path):
    with pytest.raises(StoreError, match="not a role"):
        _open(tmp_path, "arbiter", BUYER)


# --------------------------------------------------------------------------------------
# Both sides hold the same fact, and read it about different people
# --------------------------------------------------------------------------------------


def test_one_receipt_lands_in_both_stores_under_opposite_counterparties(buyer_store, provider_store):
    """The receipt is neutral. What differs is who each side is reading it about."""
    event = _event()
    assert buyer_store.ingest(event)["counterparty"] == PROVIDER
    assert provider_store.ingest(event)["counterparty"] == BUYER

    assert len(buyer_store.recall(PROVIDER).evidence) == 1
    assert len(provider_store.recall(BUYER).evidence) == 1


def test_a_store_never_recalls_its_own_side_as_a_counterparty(buyer_store):
    buyer_store.ingest(_event())
    recall = buyer_store.recall(BUYER)
    assert recall.evidence == ()
    assert recall.is_cold_start is True


def test_a_receipt_about_other_people_is_stored_but_not_indexed(buyer_store):
    """A fact can be real without being about this owner's dealings."""
    elsewhere = _event(buyer=STRANGER, tx_hash="0x" + "77" * 32)
    result = buyer_store.ingest(elsewhere)

    assert result["counterparty"] is None
    assert buyer_store.recall(PROVIDER).evidence == ()
    assert len(buyer_store.memory.list_entities(CHAIN_EVENT_CATEGORY)) == 1


def test_an_empty_relationship_is_an_honest_cold_start(buyer_store):
    recall = buyer_store.recall(PROVIDER)
    assert recall.is_cold_start is True
    assert recall.verdict == "empty_store"


def test_ingesting_the_same_receipt_twice_indexes_it_once(buyer_store):
    buyer_store.ingest(_event())
    buyer_store.ingest(_event())
    assert len(buyer_store.recall(PROVIDER).evidence) == 1


# --------------------------------------------------------------------------------------
# The index is a projection; the entities are the truth
# --------------------------------------------------------------------------------------


def test_an_entity_the_index_lost_is_repaired(buyer_store):
    """A missing index entry is recoverable, which is why it may be a projection at all."""
    buyer_store.ingest(_event())
    name = buyer_store._index_name(PROVIDER)
    buyer_store.memory.set_entity(INDEX_CATEGORY, name, {"event_ids": []}, status="verified")
    assert buyer_store.recall(PROVIDER).evidence == ()

    assert buyer_store.repair_index() == 1
    assert len(buyer_store.recall(PROVIDER).evidence) == 1


def test_an_indexed_record_that_has_gone_missing_is_fatal(buyer_store):
    """The asymmetry that makes the index safe to trust: entities are canonical."""
    buyer_store.ingest(_event())
    name = buyer_store._index_name(PROVIDER)
    body = buyer_store.memory.get_entity(INDEX_CATEGORY, name)["body"]
    body["event_ids"] = [*body["event_ids"], "0x" + "ab" * 32]
    buyer_store.memory.set_entity(INDEX_CATEGORY, name, body, status="verified")

    with pytest.raises(EvidenceRejected, match="its record is gone"):
        buyer_store.recall(PROVIDER)


# --------------------------------------------------------------------------------------
# A row that cannot be explained stops the quote
# --------------------------------------------------------------------------------------


def _plant(store, body, *, status="verified", identifier=None):
    """Write a row directly, the way a tampered store would look."""
    identifier = identifier or body["event_id"]
    store.memory.set_entity(CHAIN_EVENT_CATEGORY, identifier, body, status=status)
    store._add_to_index(PROVIDER, identifier)
    return identifier


def _stored_body(store):
    store.ingest(_event())
    identifier = store.recall(PROVIDER).evidence[0]["event_id"]
    return dict(store.memory.get_entity(CHAIN_EVENT_CATEGORY, identifier)["body"])


@pytest.mark.parametrize("field,value", [
    ("tx_hash", "0x" + "ee" * 32),
    ("log_index", 41),
])
def test_a_row_that_no_longer_produces_its_own_name_is_refused(buyer_store, field, value):
    """The entity's key is the fact's fingerprint. A body that stopped matching it was edited."""
    body = _stored_body(buyer_store)
    identifier = body["event_id"]
    body[field] = value
    buyer_store.memory.set_entity(CHAIN_EVENT_CATEGORY, identifier, body, status="verified")

    with pytest.raises(EvidenceRejected, match="its contents changed"):
        buyer_store.recall(PROVIDER)


def test_what_the_fingerprint_does_not_cover_is_checked_for_shape_and_otherwise_trusted(buyer_store):
    """An honest limit, stated rather than papered over.

    `event_id` is keccak over the transaction coordinates, so it binds a record to the receipt
    it came from. It cannot bind `deal_id`, `block_number` or `observed_at`, because those
    would need the chain re-read, and the trust model refuses to rescan before every quote.
    They are checked for shape and otherwise trusted, because the reconciler put them there.
    """
    body = _stored_body(buyer_store)
    identifier = body["event_id"]

    plausible = {**body, "deal_id": 999}
    buyer_store.memory.set_entity(CHAIN_EVENT_CATEGORY, identifier, plausible, status="verified")
    assert buyer_store.recall(PROVIDER).evidence[0]["deal_id"] == 999, (
        "a changed deal id is not detectable offline, and pretending otherwise would be worse "
        "than saying so"
    )

    malformed = {**body, "deal_id": -1}
    buyer_store.memory.set_entity(CHAIN_EVENT_CATEGORY, identifier, malformed, status="verified")
    with pytest.raises(EvidenceRejected, match="malformed deal_id"):
        buyer_store.recall(PROVIDER)


def test_an_unreadable_timestamp_is_refused(buyer_store):
    body = _stored_body(buyer_store)
    identifier = body["event_id"]
    body["observed_at"] = "whenever"
    buyer_store.memory.set_entity(CHAIN_EVENT_CATEGORY, identifier, body, status="verified")

    with pytest.raises(EvidenceRejected, match="unreadable observed_at"):
        buyer_store.recall(PROVIDER)


def test_an_unverified_row_is_refused(buyer_store):
    body = _stored_body(buyer_store)
    buyer_store.memory.set_entity(CHAIN_EVENT_CATEGORY, body["event_id"], body, status="draft")

    with pytest.raises(EvidenceRejected, match="not verified"):
        buyer_store.recall(PROVIDER)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("chain_id", 1, "chain_id"),
        ("contract_address", STRANGER, "contract_address"),
        ("event_type", "provider_seemed_unreliable", "not one this build derives"),
        ("provider", STRANGER, "not about"),
        ("buyer", STRANGER, "belongs to"),
    ],
)
def test_a_row_that_does_not_belong_here_is_refused(buyer_store, field, value, reason):
    """Each of these is a row that would look plausible and price a deal it has no business in."""
    body = _stored_body(buyer_store)
    identifier = body["event_id"]
    body[field] = value
    buyer_store.memory.set_entity(CHAIN_EVENT_CATEGORY, identifier, body, status="verified")

    with pytest.raises(EvidenceRejected, match=reason):
        buyer_store.recall(PROVIDER)


def test_a_row_missing_a_participant_is_refused(buyer_store):
    body = _stored_body(buyer_store)
    identifier = body["event_id"]
    body["provider"] = ""
    buyer_store.memory.set_entity(CHAIN_EVENT_CATEGORY, identifier, body, status="verified")

    with pytest.raises(EvidenceRejected, match="does not name a provider"):
        buyer_store.recall(PROVIDER)


# --------------------------------------------------------------------------------------
# The persona, committed before there is anything to tune it against
# --------------------------------------------------------------------------------------


def test_a_persona_can_be_committed_to_an_empty_store(provider_store):
    provider_store.commit_persona(name="atlas", digest="abc123")
    assert provider_store.persona_commitment()["sha256"] == "abc123"

    provider_store.commit_persona(name="atlas", digest="abc123")  # idempotent


def test_a_persona_cannot_be_committed_once_the_store_holds_evidence(provider_store):
    """Otherwise the persona could have been chosen with the counterparty's record in view."""
    provider_store.ingest(_event())
    with pytest.raises(StoreError, match="already holds evidence"):
        provider_store.commit_persona(name="atlas", digest="abc123")


def test_a_persona_that_changed_after_commitment_is_refused(provider_store):
    provider_store.commit_persona(name="atlas", digest="abc123")
    with pytest.raises(StoreError, match="changed after it was committed"):
        provider_store.commit_persona(name="atlas", digest="def456")


# --------------------------------------------------------------------------------------
# Unavailable memory is not an empty memory
# --------------------------------------------------------------------------------------


def test_an_unreadable_store_stops_the_quote_rather_than_reporting_no_history(buyer_store):
    """A store that cannot be read has not told us there is nothing there."""
    from sibyl_memory_client import SibylMemoryError

    from wrasse.memory_gate import MemoryRequired

    class Broken:
        def get_entity(self, *args, **kwargs):
            raise SibylMemoryError("database is locked")

        def list_entities(self, *args, **kwargs):
            raise SibylMemoryError("database is locked")

    buyer_store._memory = Broken()
    with pytest.raises(MemoryRequired):
        buyer_store.recall(PROVIDER)


# --------------------------------------------------------------------------------------
# A half-finished ingest is not an empty history
# --------------------------------------------------------------------------------------


def test_a_crash_between_the_record_and_the_index_stops_the_quote(buyer_store):
    """The failure this marker exists for.

    Without it, a crash after writing the record and before indexing it left real evidence
    outside the index, and the quote path could not tell that from a store that genuinely held
    nothing. It priced confidently on a shorter history than the truth.
    """
    from wrasse.evidence import persist_verified_event
    from wrasse.store import PENDING_CATEGORY, IngestionIncomplete

    event = _event()
    identifier = event.canonical_body()["event_id"]
    buyer_store.memory.set_entity(
        PENDING_CATEGORY, identifier, {"event_id": identifier, "started_at": "now"},
        status="verified",
    )
    persist_verified_event(buyer_store.memory, event)  # the crash landed here

    assert buyer_store.pending_ingestions() == [identifier]
    with pytest.raises(IngestionIncomplete, match="never finished"):
        buyer_store.recall(PROVIDER)


def test_finishing_the_pending_ingest_restores_the_quote(buyer_store):
    from wrasse.evidence import persist_verified_event
    from wrasse.store import PENDING_CATEGORY

    event = _event()
    identifier = event.canonical_body()["event_id"]
    buyer_store.memory.set_entity(
        PENDING_CATEGORY, identifier, {"event_id": identifier, "started_at": "now"},
        status="verified",
    )
    persist_verified_event(buyer_store.memory, event)

    assert buyer_store.finish_pending({identifier: event}) == [identifier]
    assert buyer_store.pending_ingestions() == []
    assert len(buyer_store.recall(PROVIDER).evidence) == 1


def test_a_completed_ingest_leaves_no_marker(buyer_store):
    buyer_store.ingest(_event())
    assert buyer_store.pending_ingestions() == []


# --------------------------------------------------------------------------------------
# An unowned store is not an available store
# --------------------------------------------------------------------------------------


def test_a_database_with_history_but_no_owner_is_not_adopted(tmp_path):
    """No compromise needed for this, only a careless configuration line.

    A legacy or half-migrated file holding records but naming no owner would otherwise become
    whichever side the configuration said, which is the swap the identity record exists to stop.
    """
    from sibyl_memory_client import MemoryClient

    path = tmp_path / "legacy.db"
    memory = MemoryClient.local(path)
    memory.set_entity(CHAIN_EVENT_CATEGORY, "0x" + "ab" * 32, {"event_id": "x"}, status="verified")

    with pytest.raises(StoreError, match="names no owner"):
        _open(tmp_path, "buyer", BUYER, name="legacy.db")


def test_an_empty_database_is_adopted(tmp_path):
    store = _open(tmp_path, "buyer", BUYER, name="fresh.db")
    assert store.identity.role == "buyer"


# --------------------------------------------------------------------------------------
# Honest verdicts
# --------------------------------------------------------------------------------------


def test_a_store_with_other_history_is_not_reported_as_empty(buyer_store):
    """Both use baseline terms; only one of them knows nothing."""
    buyer_store.ingest(_event(buyer=STRANGER, tx_hash="0x" + "77" * 32))

    recall = buyer_store.recall(PROVIDER)
    assert recall.is_cold_start is True
    assert recall.verdict == "no_match"


def test_the_two_sides_agree_on_which_receipts_exist(buyer_store, provider_store):
    event = _event()
    buyer_store.ingest(event)
    provider_store.ingest(event)

    assert buyer_store.indexed_event_ids(PROVIDER) == provider_store.indexed_event_ids(BUYER)


def test_a_full_page_refuses_rather_than_repairing_part_of_the_store(buyer_store, monkeypatch):
    """`list_entities` has no cursor, so a full page means there may be more nobody can see."""
    from wrasse import store as store_module

    monkeypatch.setattr(store_module, "ENUMERATION_LIMIT", 1)
    buyer_store.ingest(_event())
    buyer_store.ingest(_event(tx_hash="0x" + "88" * 32))

    with pytest.raises(StoreError, match="cannot be proved"):
        buyer_store.repair_index()


def test_the_marker_is_written_before_the_record(buyer_store, monkeypatch):
    """Order is the whole mechanism.

    A marker written after the record would leave exactly the window it exists to close: the
    record on disk, the index not yet updated, and nothing saying so.
    """
    from wrasse import store as store_module
    from wrasse.store import PENDING_CATEGORY

    seen = {}
    original = store_module.persist_verified_event

    def observe(memory, event):
        seen["marker_present"] = bool(memory.list_entities(PENDING_CATEGORY, limit=10))
        return original(memory, event)

    monkeypatch.setattr(store_module, "persist_verified_event", observe)
    buyer_store.ingest(_event())

    assert seen["marker_present"] is True, "the record was written with nothing marking it"
    assert buyer_store.pending_ingestions() == []

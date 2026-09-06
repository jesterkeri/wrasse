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
    IDENTITY_CATEGORY,
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


# --------------------------------------------------------------------------------------
# Repair is a writer, and adoption is a decision about whose memory this is
# --------------------------------------------------------------------------------------


def test_repair_holds_the_same_lock_a_writer_takes(tmp_path):
    """Repair is a read-modify-write over the entry an ingest is writing.

    Unlocked, a repair that read the index before a concurrent ingest and wrote it after put
    back its own stale copy. The id the ingest had just added was gone, the ingest had already
    cleared its marker on the way out, and nothing was left to say the store was short: the
    quote path then read a shorter history and could not tell. The property that stops it is
    that no other writer can be inside the index while a repair is running.

    `flock` is held per open file description, so a second `open` of the same path contends
    exactly as another process would.
    """

    import fcntl

    store = _open(tmp_path, "buyer", BUYER, "buyer.db")
    store.ingest(_event(tx_hash="0x" + "a1" * 32))
    name = store._index_name(PROVIDER)
    store.memory.set_entity(INDEX_CATEGORY, name, {"event_ids": []}, status="verified")

    lock_path = tmp_path / "buyer.db.lock"
    observed = {}

    def try_lock() -> bool:
        with open(lock_path, "w") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(other, fcntl.LOCK_UN)
            return True

    original = store._add_to_index

    def watch(counterparty, identifier):
        observed["during"] = try_lock()
        return original(counterparty, identifier)

    store._add_to_index = watch
    assert store.repair_index() == 1
    store._add_to_index = original

    assert observed["during"] is False, "a repair ran with the index open to other writers"
    assert try_lock() is True, "the lock outlived the repair that took it"


def test_a_store_holding_only_a_learned_dimension_is_not_empty(tmp_path):
    """Emptiness is a property of the file, not of the categories this build happens to name.

    A dimension is economically active: it is what turns a receipt into a number. Adopting a
    store that already holds one would let a configuration line decide whose ontology sets a
    price.
    """

    MemoryClient.local(tmp_path / "legacy.db").set_entity(
        "behavior_dimension", "inherited",
        {"source_event_type": "timeout_claimed_without_delivery", "severity": 0.9},
        status="active",
    )

    with pytest.raises(StoreError, match="already holds records but names no owner"):
        _open(tmp_path, "buyer", BUYER, "legacy.db")


def test_an_identity_nothing_attested_is_not_an_identity(tmp_path):
    """A `draft` row is one nothing has vouched for, and everything else rests on this one."""

    store = _open(tmp_path, "buyer", BUYER, "buyer.db")
    row = store.memory.get_entity(IDENTITY_CATEGORY, "self")
    store.memory.set_entity(IDENTITY_CATEGORY, "self", row["body"], status="draft")

    with pytest.raises(StoreError, match="not verified"):
        _open(tmp_path, "buyer", BUYER, "buyer.db")


def test_an_identity_carrying_fields_this_build_never_wrote_is_refused(tmp_path):
    """Matching the fields we look at proves nothing about the ones we do not."""

    store = _open(tmp_path, "buyer", BUYER, "buyer.db")
    body = dict(store.memory.get_entity(IDENTITY_CATEGORY, "self")["body"])
    body["also_provider"] = True
    store.memory.set_entity(IDENTITY_CATEGORY, "self", body, status="verified")

    with pytest.raises(StoreError, match="not the shape this build writes"):
        _open(tmp_path, "buyer", BUYER, "buyer.db")


def test_an_identity_missing_the_field_that_dates_it_is_refused(tmp_path):
    """Exactly this build's shape means exactly, not "at least the parts we look at"."""

    store = _open(tmp_path, "buyer", BUYER, "buyer.db")
    body = dict(store.memory.get_entity(IDENTITY_CATEGORY, "self")["body"])
    body.pop("created_at")
    store.memory.set_entity(IDENTITY_CATEGORY, "self", body, status="verified")

    with pytest.raises(StoreError, match="Missing: \\['created_at'\\]"):
        _open(tmp_path, "buyer", BUYER, "buyer.db")


def test_a_second_thread_cannot_walk_into_a_held_store(tmp_path):
    """Recursion is a property of the caller, never of the object.

    A counter on the store answers "is somebody inside", and a second thread reads that as
    "I am inside" and enters the section the lock exists to protect. That is how the repair
    and ingest interleaving came back after the file lock was added: both were locked, and
    neither lock excluded the other thread.
    """

    import threading

    store = _open(tmp_path, "buyer", BUYER, "buyer.db")
    inside = threading.Event()
    release = threading.Event()
    overlapped = []

    def hold():
        with store._exclusive():
            inside.set()
            release.wait(5)

    def intrude():
        inside.wait(5)
        with store._exclusive():
            overlapped.append(release.is_set())

    holder, intruder = threading.Thread(target=hold), threading.Thread(target=intrude)
    holder.start()
    intruder.start()
    inside.wait(5)
    release.set()
    holder.join(5)
    intruder.join(5)

    assert overlapped == [True], "a second thread entered while the first still held the store"


def test_a_conflicting_replay_does_not_strand_the_marker(buyer_store):
    """The failure that turns a recoverable store into a dead one.

    `ingest` marks, writes, indexes, clears. A crash anywhere in the middle leaves the mark
    standing and `recall` refuses until a replay finishes it, which is the design and it
    works. `EventConflict` is different: it is raised by `persist_verified_event` before any
    mutation, propagates out of `ingest`, and the mark is never cleared. Every later call
    refuses, and replaying the conflicting receipt raises the same conflict and re-strands it.

    Reachable without a bug or an attacker. `event_id` covers chain, contract, transaction and
    log index; the canonical body also carries `block_number`, which a reorg changes. Ingest at
    block N, reorg, re-include at N+1, and every command afterwards produces the N+1 body. The
    only body that clears the mark is the one the chain no longer returns.

    The conflict must still be raised. Refusing is right: two bodies for one id is exactly what
    must never be resolved by arrival order. What must not happen is the store dying with it.
    """

    from wrasse.evidence import EventConflict

    store = buyer_store
    event = _event(tx_hash="0x" + "e1" * 32, block_number=100)
    store.ingest(event)
    assert store.pending_ingestions() == []

    reorged = _event(tx_hash="0x" + "e1" * 32, block_number=101)
    with pytest.raises(EventConflict):
        store.ingest(reorged)

    assert store.pending_ingestions() == [], (
        "the conflict was refused before anything was written, so the mark must not survive it"
    )
    assert len(store.recall(PROVIDER).evidence) == 1, "and the store still prices"

    # the same replay again, because a store that only survives one is not fixed
    with pytest.raises(EventConflict):
        store.ingest(reorged)
    assert store.pending_ingestions() == []
    assert len(store.recall(PROVIDER).evidence) == 1


def test_a_group_of_memories_is_held_together_not_one_after_the_other(buyer_store, provider_store):
    """`reconcile` delivers one receipt to both stores, and a reader must not see it half done.

    Taking each lock in turn leaves a window where one side holds a receipt the other does not.
    A quote acquiring both inside that window sees a genuinely one-sided history and aborts,
    accusing the two memories of disagreeing when the second delivery was milliseconds away.
    It fails closed, which is the right direction, and it is still a false accusation.

    Unreachable while one person ran one command at a time. Reachable the moment a hosted quote
    service reads while an operator reconciles, which is the architecture that shipped today.

    Checked by holding the group and then trying to take one member from another thread. The
    per-store guard is held across the yield, so a second thread blocks rather than walking in,
    and a thread that is still blocked after a generous wait is the property.
    """

    import threading

    from wrasse.store import both_locked

    stores = {"buyer": buyer_store, "provider": provider_store}
    took_it = threading.Event()

    def grab():
        with provider_store.lock():
            took_it.set()

    with both_locked(stores):
        other = threading.Thread(target=grab, daemon=True)
        other.start()
        assert not took_it.wait(timeout=1.0), (
            "a second thread took one of the memories while the group was held, so the pair "
            "can be observed mid-delivery"
        )

    other.join(timeout=5.0)
    assert took_it.is_set(), "and it must be released afterwards, or the next quote hangs"


def test_reconcile_holds_both_memories_across_both_deliveries(buyer_store, provider_store, monkeypatch):
    """The property the helper above exists for, asserted where it is actually used.

    The previous test proves `both_locked` locks. It does not prove `reconcile` calls it, and a
    mutation removing the call passed the entire suite. That is the shape this project keeps
    hitting: the assertion one layer above the defect.

    Checked on the real objects. `_depth` is non-zero only inside a held lock, so reading it
    from inside the first delivery says whether the second store was already held when the
    first one was written.
    """

    from wrasse import reconciler
    from wrasse.store import ChainEvent as _  # noqa: F401  (import shape check only)

    event = _event(tx_hash="0x" + "d1" * 32)
    monkeypatch.setattr(reconciler, "verify_outcome", lambda *a, **k: event)

    stores = {"buyer": buyer_store, "provider": provider_store}
    depth_during_first_delivery = {}
    real_ingest = buyer_store.ingest

    def watched(evt):
        depth_during_first_delivery["provider"] = provider_store._depth
        return real_ingest(evt)

    monkeypatch.setattr(buyer_store, "ingest", watched)
    reconciler.reconcile(stores, object(), object())

    assert depth_during_first_delivery["provider"] > 0, (
        "the provider memory was not held while the buyer's was being written, so a reader "
        "between the two deliveries sees a one-sided history"
    )


def test_recall_reads_the_stores_that_set_the_price(tmp_path, monkeypatch, capsys):
    """The command named after the thing this project demonstrates read the wrong file.

    It called a fuzzy helper against `WRASSE_MEMORY_PATH`, a third database neither side ever
    writes to. `MemoryClient` creates a missing file, so it answered `cold_start: true,
    verdict: empty_store` about a store that had never existed, while both real memories held
    full history. A judge running it to inspect what an agent remembers was told: nothing.

    `WRASSE_MEMORY_PATH` is pointed at a path that cannot exist, so a regression to the old
    helper fails here rather than passing quietly against an empty file it invents.
    """

    import json as _json

    from wrasse.cli import main

    buyer = _open(tmp_path, "buyer", BUYER, "buyer.db")
    provider = _open(tmp_path, "provider", PROVIDER, "provider.db")
    event = _event(tx_hash="0x" + "f1" * 32)
    buyer.ingest(event)
    provider.ingest(event)

    monkeypatch.setenv("WRASSE_BUYER_MEMORY_PATH", str(tmp_path / "buyer.db"))
    monkeypatch.setenv("WRASSE_PROVIDER_MEMORY_PATH", str(tmp_path / "provider.db"))
    monkeypatch.setenv("WRASSE_BUYER_ADDRESS", BUYER)
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", PROVIDER)
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", str(CHAIN_ID))
    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "does" / "not" / "exist.db"))

    assert main(["recall", PROVIDER]) == 0
    printed = _json.loads(capsys.readouterr().out)

    assert set(printed) == {"buyer", "provider"}, "both sides, because both remember"
    for side in ("buyer", "provider"):
        assert printed[side]["cold_start"] is False
        assert len(printed[side]["evidence"]) == 1


def test_store_repair_puts_back_a_lost_index_entry(tmp_path, monkeypatch, capsys):
    """The repair existed and could not be run, which made "repairable" and "fatal" the same.

    A lost index entry prices as a cold start, and a cold start is exactly what a genuinely
    unknown counterparty looks like, so the failure is silent and reads as a correct answer.
    `repair_index` detects and fixes it and had no caller outside the tests.
    """

    import json as _json

    from wrasse.cli import main
    from wrasse.store import INDEX_CATEGORY

    buyer = _open(tmp_path, "buyer", BUYER, "buyer.db")
    provider = _open(tmp_path, "provider", PROVIDER, "provider.db")
    event = _event(tx_hash="0x" + "a7" * 32)
    buyer.ingest(event)
    provider.ingest(event)
    assert len(buyer.recall(PROVIDER).evidence) == 1

    # lose the projection, keeping the canonical entity, which is the repairable direction
    for row in buyer.memory.list_entities(INDEX_CATEGORY, limit=10):
        buyer.memory.delete_entity(INDEX_CATEGORY, row["name"])
    assert buyer.recall(PROVIDER).is_cold_start, "this is the silent failure being repaired"

    monkeypatch.setenv("WRASSE_BUYER_MEMORY_PATH", str(tmp_path / "buyer.db"))
    monkeypatch.setenv("WRASSE_PROVIDER_MEMORY_PATH", str(tmp_path / "provider.db"))
    monkeypatch.setenv("WRASSE_BUYER_ADDRESS", BUYER)
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", PROVIDER)
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", str(CHAIN_ID))

    assert main(["store-repair"]) == 0
    printed = _json.loads(capsys.readouterr().out)
    assert printed["buyer"]["index_entries_repaired"] == 1
    assert printed["buyer"]["unfinished_ingests"] == []

    assert len(_open(tmp_path, "buyer", BUYER, "buyer.db").recall(PROVIDER).evidence) == 1, (
        "and the history is priceable again"
    )


def test_a_conflict_does_not_erase_a_marker_left_by_an_earlier_crash(buyer_store, monkeypatch):
    """The hole in my own fix for the bricking bug, found by an adversarial pass.

    Clearing the marker on a conflict is right when this call created it, because nothing
    started. It is wrong when a marker was already standing: that one says an earlier ingest
    died somewhere in the middle and the store cannot say what it holds. Deleting it turns a
    refusal into a quote from a history that may be missing its index entry, which is the
    fail-closed guarantee inverted.

    The crash-then-conflict order is the realistic one. An ingest is interrupted between the
    record and the index, and the receipt is later replayed after a re-inclusion has changed
    its block number. Two independently ordinary events.
    """

    from wrasse.evidence import EventConflict

    event = _event(tx_hash="0x" + "c9" * 32, block_number=100)

    # An ingest that dies during the index write, which is the interruption that matters: the
    # record is stored and the projection is not, so the store holds evidence the pricing path
    # cannot find. That is exactly what the marker exists to announce.
    def interrupted(*_args, **_kwargs):
        raise RuntimeError("interrupted between the record and the index")

    monkeypatch.setattr(type(buyer_store), "_add_to_index", interrupted)
    with pytest.raises(RuntimeError):
        buyer_store.ingest(event)
    stranded = buyer_store.pending_ingestions()
    assert stranded == [event.canonical_body()["event_id"]], "the mark must survive a crash"

    monkeypatch.undo()

    # and then the same receipt arrives with a body a re-inclusion changed
    with pytest.raises(EventConflict):
        buyer_store.ingest(_event(tx_hash="0x" + "c9" * 32, block_number=101))

    assert buyer_store.pending_ingestions() == stranded, (
        "the conflict cleared a marker it did not create, so an unfinished ingest was "
        "forgotten and the store would price over the gap"
    )

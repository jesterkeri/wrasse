from __future__ import annotations

from dataclasses import replace

import pytest
from sibyl_memory_client import MemoryClient

from wrasse.evidence import EventConflict, event_id, persist_verified_event


def test_event_id_is_stable_and_abi_encoded(chain_event):
    assert event_id(chain_event) == event_id(chain_event)
    changed = replace(chain_event, log_index=chain_event.log_index + 1)
    assert event_id(changed) != event_id(chain_event)


def test_ingest_is_idempotent_and_journals_only_once(tmp_path, chain_event):
    memory = MemoryClient.local(tmp_path / "memory.db")
    first = persist_verified_event(memory, chain_event)
    second = persist_verified_event(memory, chain_event)
    assert first.created is True
    assert second.created is False
    assert len(memory.list_entities("chain_event")) == 1
    assert len(memory.read_events()) == 1


def test_divergent_replay_raises_conflict(tmp_path, chain_event):
    memory = MemoryClient.local(tmp_path / "memory.db")
    first = persist_verified_event(memory, chain_event)
    altered = dict(first.entity["body"])
    altered["event_type"] = "different_claim"
    memory.set_entity("chain_event", altered["event_id"], altered)

    with pytest.raises(EventConflict):
        persist_verified_event(memory, chain_event)



def test_a_journal_left_short_by_a_crash_is_filled_on_replay(tmp_path, chain_event):
    """The repair that has been owed since the first plan.

    A crash between writing the fact and writing the timeline used to be permanent, because
    replay saw the fact already there and returned without ever writing the timeline.
    """
    memory = MemoryClient.local(tmp_path / "memory.db")
    body = chain_event.canonical_body()
    memory.set_entity("chain_event", body["event_id"], body, status="verified")
    assert len(memory.read_events()) == 0, "the crash happened before the journal write"

    result = persist_verified_event(memory, chain_event)

    assert result.created is False, "the fact was already there"
    assert len(memory.read_events()) == 1, "the timeline caught up"


def test_a_crash_before_the_marker_may_append_twice_and_that_is_accepted(tmp_path, chain_event):
    """Exactly once applies to the canonical entity, not to journal rows.

    Sibyl offers no atomic or idempotent journal key, so a crash after appending and before
    recording that the append happened means replay appends again. Consumers deduplicate by
    event id, and a duplicate line in a timeline is harmless where a missing fact would not be.
    """
    from wrasse.evidence import JOURNAL_MARKER_CATEGORY

    memory = MemoryClient.local(tmp_path / "memory.db")
    body = chain_event.canonical_body()
    memory.set_entity("chain_event", body["event_id"], body, status="verified")
    memory.write_event(
        evaluated={"source": "base_receipt", "verification": "passed"},
        acted=[f"ingested chain event {body['event_id']}"],
        extra={"event_id": body["event_id"]},
        ts=chain_event.observed_at,
    )

    persist_verified_event(memory, chain_event)

    journal = memory.read_events()
    assert len(journal) == 2, "replay appended again, because the marker was missing"
    assert len({entry["extra"]["event_id"] for entry in journal}) == 1, (
        "one event id, so a consumer deduplicating by it sees one fact"
    )
    assert len(memory.list_entities("chain_event")) == 1, "the canonical entity is still one"
    assert len(memory.list_entities(JOURNAL_MARKER_CATEGORY)) == 1


def test_a_partial_dual_store_write_converges_on_replay(tmp_path, chain_event):
    """Two SQLite stores cannot be made atomic through this SDK.

    So the property is not atomicity, it is convergence: replaying the same receipt brings the
    store that missed it level without duplicating anything in the store that already had it.
    """
    buyer = MemoryClient.local(tmp_path / "buyer.db")
    provider = MemoryClient.local(tmp_path / "provider.db")

    persist_verified_event(buyer, chain_event)  # the crash landed here
    assert len(buyer.list_entities("chain_event")) == 1
    assert len(provider.list_entities("chain_event")) == 0

    for store in (buyer, provider):
        persist_verified_event(store, chain_event)

    for store in (buyer, provider):
        assert len(store.list_entities("chain_event")) == 1
        assert len(store.read_events()) == 1


def test_an_event_type_outside_the_closed_set_is_refused(chain_event):
    """Event names must match a log a judge can find, never free text."""
    from dataclasses import replace

    from wrasse.evidence import InvalidEvidence, validate_chain_event

    with pytest.raises(InvalidEvidence, match="not a recognised event type"):
        validate_chain_event(replace(chain_event, event_type="provider_seemed_unreliable"))


def test_the_two_participants_must_differ(chain_event):
    from dataclasses import replace

    from wrasse.evidence import InvalidEvidence, validate_chain_event

    with pytest.raises(InvalidEvidence, match="must differ"):
        validate_chain_event(replace(chain_event, buyer=chain_event.provider))

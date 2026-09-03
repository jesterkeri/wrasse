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


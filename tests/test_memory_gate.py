from __future__ import annotations

import pytest
from sibyl_memory_client import MemoryClient, StorageError

from wrasse.evidence import persist_verified_event
from wrasse.memory_gate import MemoryRequired, recall_counterparty_evidence


class BrokenMemory:
    def search_entities(self, *args, **kwargs):
        raise StorageError("database unavailable")


def test_empty_store_is_an_explicit_cold_start(tmp_path):
    memory = MemoryClient.local(tmp_path / "memory.db")
    recalled = recall_counterparty_evidence(
        memory, "0x3333333333333333333333333333333333333333"
    )
    assert recalled.evidence == ()
    assert recalled.verdict == "empty_store"
    assert recalled.is_cold_start


def test_non_matching_nonempty_store_is_an_explicit_cold_start(tmp_path):
    memory = MemoryClient.local(tmp_path / "memory.db")
    memory.set_entity("chain_event", "unrelated", {"provider": "0x" + "44" * 20})
    recalled = recall_counterparty_evidence(
        memory, "0x3333333333333333333333333333333333333333"
    )
    assert recalled.evidence == ()
    assert recalled.verdict == "no_match"
    assert recalled.is_cold_start


def test_memory_failure_stops_term_generation():
    with pytest.raises(MemoryRequired):
        recall_counterparty_evidence(
            BrokenMemory(), "0x3333333333333333333333333333333333333333"
        )


def test_fresh_client_recalls_prior_verified_event(tmp_path, chain_event):
    database = tmp_path / "memory.db"
    first_process = MemoryClient.local(database)
    persist_verified_event(first_process, chain_event)
    del first_process

    fresh_process = MemoryClient.local(database)
    recalled = recall_counterparty_evidence(fresh_process, chain_event.provider)
    assert len(recalled.evidence) == 1
    assert recalled.evidence[0]["event_id"]
    assert recalled.verdict == "ok"
    assert not recalled.is_cold_start


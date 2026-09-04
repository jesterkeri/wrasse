from __future__ import annotations

import json

import json

import pytest
from sibyl_memory_client import MemoryClient

from wrasse.dimensions import (
    DimensionDefinition,
    DimensionError,
    create_dimension,
    get_or_create_dimension,
    load_dimensions,
)


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


def test_dimension_rejects_unknown_fields():
    with pytest.raises(DimensionError):
        DimensionDefinition.from_model({
            "dimension_id": "delivery_pattern",
            "signal_direction": "negative",
            "severity": 0.5,
            "confidence": 0.5,
            "applies_when": ["deadline_sensitive"],
            "expression": "exec('no')",
        }, "some_event")


def test_creation_retries_once_after_malformed_json():
    outputs = iter([
        FakeResponse("not json"),
        FakeResponse(json.dumps({
            "dimension_id": "deadline_followthrough",
            "signal_direction": "negative",
            "severity": 0.8,
            "confidence": 0.4,
            "applies_when": ["deadline_sensitive"],
        })),
    ])
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return next(outputs)

    dimension = create_dimension(
        {"event_type": "timeout_claimed_without_delivery"},
        api_key="test-key",
        post=post,
    )
    assert len(calls) == 2
    assert dimension.dimension_id == "deadline_followthrough"
    assert dimension.source_event_type == "timeout_claimed_without_delivery"


def test_existing_source_event_dimension_is_reused_without_model_call(tmp_path):
    memory = MemoryClient.local(tmp_path / "memory.db")
    existing = DimensionDefinition(
        "deadline_followthrough",
        "timeout_claimed_without_delivery",
        "negative",
        0.8,
        0.4,
        ("deadline_sensitive",),
    )
    memory.set_entity("behavior_dimension", existing.dimension_id, existing.body(), status="active")

    def must_not_call(*args, **kwargs):
        raise AssertionError("model was called for a known event type")

    loaded, created = get_or_create_dimension(
        memory,
        {"event_type": "timeout_claimed_without_delivery"},
        api_key="unused",
        post=must_not_call,
    )
    assert loaded == existing
    assert created is False
    assert load_dimensions(memory) == (existing,)



def test_an_answer_no_retry_can_improve_is_not_retried():
    """A bad key or an empty balance refuses identically next time.

    Retrying it burns the one attempt a genuinely transient failure would have needed.
    """
    from wrasse.dimensions import DimensionError, create_dimension

    calls = []

    class Refused:
        status_code = 402

        @staticmethod
        def raise_for_status():
            raise AssertionError("should never be reached")

    def post(url, **kwargs):
        calls.append(url)
        return Refused()

    with pytest.raises(DimensionError, match="402"):
        create_dimension(
            {"event_type": "timeout_claimed_without_delivery"}, api_key="bad", post=post
        )
    assert len(calls) == 1


def test_a_transient_failure_gets_its_second_chance(monkeypatch):
    from wrasse import dimensions as module

    monkeypatch.setattr(module, "_sleep", lambda _: None)
    attempts = []

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": json.dumps({
                "dimension_id": "non_delivery_after_payment",
                "severity": 0.9,
                "confidence": 0.9,
                "applies_when": ["deadline_sensitive"],
            })}}]}

    def post(url, **kwargs):
        attempts.append(url)
        if len(attempts) == 1:
            raise module.requests.RequestException("connection reset")
        return Response()

    definition = module.create_dimension(
        {"event_type": "timeout_claimed_without_delivery"}, api_key="ok", post=post
    )
    assert len(attempts) == 2
    assert definition.signal_direction == "negative"

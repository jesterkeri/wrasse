from __future__ import annotations

import json

import pytest

from rapport.dimensions import DimensionDefinition, DimensionError, create_dimension


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


from __future__ import annotations

from wrasse.dimensions import DimensionDefinition
from wrasse.engine import PROFILES, produce_terms


def test_same_history_changes_terms_by_task_profile():
    evidence = [{
        "event_id": "0x" + "11" * 32,
        "event_type": "model_created_event_name",
    }]
    dimensions = [DimensionDefinition(
        dimension_id="model_created_dimension",
        source_event_type="model_created_event_name",
        signal_direction="negative",
        severity=0.8,
        confidence=0.5,
        applies_when=("deadline_sensitive",),
    )]
    common = dict(
        evidence=evidence,
        dimensions=dimensions,
        base_price_wei=10_000,
        base_bond_bps=500,
        base_service_window=3_600,
    )
    urgent = produce_terms(profile=PROFILES["urgent"], **common)
    budget = produce_terms(profile=PROFILES["budget"], **common)
    assert urgent.provider_bond_bps > budget.provider_bond_bps
    assert urgent.price_wei > budget.price_wei
    assert urgent.service_window < budget.service_window


def test_unknown_dimension_name_is_handled_without_code_changes():
    event_id = "0x" + "55" * 32
    terms = produce_terms(
        evidence=[{"event_id": event_id, "event_type": "novel_signal"}],
        dimensions=[DimensionDefinition(
            dimension_id="surprise_dimension_from_model",
            source_event_type="novel_signal",
            signal_direction="negative",
            severity=1,
            confidence=1,
            applies_when=("quality_sensitive",),
        )],
        profile=PROFILES["sensitive"],
        base_price_wei=10_000,
        base_bond_bps=0,
        base_service_window=100,
    )
    assert terms.risk == 1
    assert terms.provider_bond_bps == 2_500
    assert terms.evidence_event_ids == (event_id,)


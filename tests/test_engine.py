from __future__ import annotations

from wrasse.dimensions import DimensionDefinition
from wrasse.engine import PROFILES, produce_terms


def test_same_history_changes_terms_by_task_profile():
    evidence = [{
        "event_id": "0x" + "11" * 32,
        "event_type": "timeout_claimed_without_delivery",
    }]
    dimensions = [DimensionDefinition(
        dimension_id="model_created_dimension",
        source_event_type="timeout_claimed_without_delivery",
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
        base_payout_delay=1_800,
    )
    urgent = produce_terms(profile=PROFILES["urgent"], **common)
    budget = produce_terms(profile=PROFILES["budget"], **common)
    assert urgent.provider_bond_bps > budget.provider_bond_bps
    assert urgent.service_window < budget.service_window
    # The limits each publishes differ too, and those carry no risk term: an urgent buyer will
    # pay more for the same job and will wait less to be sure of it.
    assert urgent.max_price_bps > budget.max_price_bps
    assert urgent.min_payout_delay < budget.min_payout_delay


def test_unknown_dimension_name_is_handled_without_code_changes():
    """The dimension id comes from the model and is never hard-coded.

    The *event type* it derives from is a different matter: that set is closed and derived from
    the contract's own logs, because a name nobody can find in a receipt is not evidence.
    """
    event_id = "0x" + "55" * 32
    terms = produce_terms(
        evidence=[{"event_id": event_id, "event_type": "timeout_claimed_without_delivery"}],
        dimensions=[DimensionDefinition(
            dimension_id="surprise_dimension_from_model",
            source_event_type="timeout_claimed_without_delivery",
            signal_direction="negative",
            severity=1,
            confidence=1,
            applies_when=("quality_sensitive",),
        )],
        profile=PROFILES["sensitive"],
        base_price_wei=10_000,
        base_bond_bps=0,
        base_service_window=100,
        base_payout_delay=600,
    )
    assert terms.risk == 1
    assert terms.provider_bond_bps == 2_500
    assert terms.recalled_event_ids == (event_id,)



def test_a_repeated_recall_does_not_count_twice():
    """Evidence is a set.

    If the memory layer returned one receipt twice, doubling its weight would move terms
    against a counterparty on the strength of a single event counted twice.
    """
    event = {"event_id": "0x" + "99" * 32, "event_type": "timeout_claimed_without_delivery"}
    dimensions = [DimensionDefinition(
        dimension_id="model_created_dimension",
        source_event_type="timeout_claimed_without_delivery",
        signal_direction="negative",
        severity=0.8,
        confidence=0.5,
        applies_when=("deadline_sensitive",),
    )]
    common = dict(
        dimensions=dimensions,
        profile=PROFILES["urgent"],
        base_price_wei=10_000,
        base_bond_bps=500,
        base_service_window=3_600,
        base_payout_delay=1_800,
    )
    once = produce_terms(evidence=[event], **common)
    twice = produce_terms(evidence=[event, dict(event)], **common)
    assert once == twice


def _dimension():
    return DimensionDefinition(
        dimension_id="model_created_dimension",
        source_event_type="timeout_claimed_without_delivery",
        signal_direction="negative",
        severity=0.8,
        confidence=0.5,
        applies_when=("deadline_sensitive",),
    )


def test_the_same_receipt_spelled_differently_is_one_receipt():
    """The commitment and the engine must agree on what counts as one piece of evidence.

    The same 32 bytes can be written prefixed or bare, upper case or lower. If the two
    components disagreed, one receipt would move the terms while the commitment recorded a
    single member, and the hash would no longer describe the evidence that priced the deal.
    """
    from wrasse.policy_hash import evidence_hash

    prefixed = "0x" + "ab" * 32
    bare_upper = ("AB" * 32)
    common = dict(
        dimensions=[_dimension()],
        profile=PROFILES["urgent"],
        base_price_wei=10_000,
        base_bond_bps=500,
        base_service_window=3_600,
        base_payout_delay=1_800,
    )
    once = produce_terms(
        evidence=[{"event_id": prefixed, "event_type": "timeout_claimed_without_delivery"}], **common
    )
    spelled_twice = produce_terms(
        evidence=[
            {"event_id": prefixed, "event_type": "timeout_claimed_without_delivery"},
            {"event_id": bare_upper, "event_type": "timeout_claimed_without_delivery"},
        ],
        **common,
    )
    assert once == spelled_twice
    assert evidence_hash([prefixed, bare_upper]) == evidence_hash([prefixed])


def test_two_different_stories_about_one_receipt_fail_closed():
    """Silently keeping whichever arrived first makes terms depend on result ordering."""
    import pytest

    event_id = "0x" + "cd" * 32
    common = dict(
        dimensions=[_dimension()],
        profile=PROFILES["urgent"],
        base_price_wei=10_000,
        base_bond_bps=500,
        base_service_window=3_600,
        base_payout_delay=1_800,
    )
    with pytest.raises(ValueError, match="conflicting records"):
        produce_terms(
            evidence=[
                {"event_id": event_id, "event_type": "timeout_claimed_without_delivery"},
                {"event_id": event_id, "event_type": "something_else"},
            ],
            **common,
        )

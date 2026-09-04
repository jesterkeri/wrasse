"""The moment the whole entry is built on: both sides' terms move, for different reasons.

The two grievances are not symmetric and must not be. A buyer remembers a provider that took a
deal and never delivered. A provider remembers a buyer that made it wait out the payout delay
instead of releasing on delivery. Each side prices what it was actually hurt by, and neither
side's complaint may move the other side's numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from wrasse.dimensions import DIMENSION_CATEGORY, DimensionDefinition, load_dimensions
from wrasse.engine import (
    MIN_PAYOUT_DELAY_SECONDS,
    MIN_SERVICE_WINDOW_SECONDS,
    PROFILES,
    produce_provider_terms,
    produce_terms,
)
from wrasse.evidence import ChainEvent
from wrasse.providers import ProviderPersona
from wrasse.store import WrasseStore

BUYER = "0x4444444444444444444444444444444444444444"
PROVIDER = "0x3333333333333333333333333333333333333333"
ESCROW = "0x1111111111111111111111111111111111111111"
CHAIN_ID = 84532

BASE_PRICE = 10**14
BASE_BOND_BPS = 500
BASE_WINDOW = 3_600
BASE_DELAY = 1_800

PERSONA = ProviderPersona(
    name="atlas",
    address=PROVIDER,
    cashflow_sensitivity=Decimal("0.70"),
    price_sensitivity_bps=2_500,
    delay_sensitivity_seconds=1_800,
)

#: One dimension per outcome, of the shape the model is constrained to produce. Written by hand
#: here so the test never needs a model call, which is itself a property worth having.
DIMENSIONS = {
    "timeout": DimensionDefinition(
        dimension_id="abandoned_after_accepting",
        source_event_type="timeout_claimed_without_delivery",
        signal_direction="negative",
        severity=0.8,
        confidence=0.9,
        applies_when=("deadline_sensitive", "quality_sensitive"),
    ),
    "late_release": DimensionDefinition(
        dimension_id="withheld_release_until_forced",
        source_event_type="delivered_and_claimed_after_delay",
        signal_direction="negative",
        severity=0.6,
        confidence=0.9,
        applies_when=("cost_sensitive",),
    ),
}


def _event(event_type: str, *, tx: str) -> ChainEvent:
    return ChainEvent(
        chain_id=CHAIN_ID,
        contract_address=ESCROW,
        tx_hash=tx,
        log_index=0,
        block_number=100,
        event_type=event_type,
        deal_id=1,
        buyer=BUYER,
        provider=PROVIDER,
        observed_at=datetime(2026, 9, 4, tzinfo=UTC).isoformat(),
    )


TIMEOUT = _event("timeout_claimed_without_delivery", tx="0x" + "aa" * 32)
LATE_RELEASE = _event("delivered_and_claimed_after_delay", tx="0x" + "bb" * 32)


@pytest.fixture
def both(tmp_path):
    """Two identified stores, each holding both receipts, as the reconciler leaves them."""

    stores = {
        "buyer": WrasseStore.open(tmp_path / "buyer.db", role="buyer", owner_address=BUYER,
                                  chain_id=CHAIN_ID, escrow_address=ESCROW),
        "provider": WrasseStore.open(tmp_path / "provider.db", role="provider",
                                     owner_address=PROVIDER, chain_id=CHAIN_ID,
                                     escrow_address=ESCROW),
    }
    for store in stores.values():
        for event in (TIMEOUT, LATE_RELEASE):
            store.ingest(event)
        for dimension in DIMENSIONS.values():
            store.memory.set_entity(
                DIMENSION_CATEGORY, dimension.dimension_id, dimension.body(), status="active"
            )
    return stores


def _buyer_terms(store, evidence, profile="urgent"):
    return produce_terms(
        evidence=evidence,
        dimensions=load_dimensions(store.memory),
        profile=PROFILES[profile],
        base_price_wei=BASE_PRICE,
        base_bond_bps=BASE_BOND_BPS,
        base_service_window=BASE_WINDOW,
    )


def _provider_terms(store, evidence):
    return produce_provider_terms(
        evidence=evidence,
        dimensions=load_dimensions(store.memory),
        persona=PERSONA,
        base_price_wei=BASE_PRICE,
        base_payout_delay=BASE_DELAY,
    )


# --------------------------------------------------------------------------------------
# Both sides move
# --------------------------------------------------------------------------------------


def test_a_remembered_timeout_makes_the_buyer_ask_for_more_bond(both):
    cold = _buyer_terms(both["buyer"], [])
    warm = _buyer_terms(both["buyer"], both["buyer"].recall(PROVIDER).evidence)

    assert cold.provider_bond_bps == BASE_BOND_BPS
    assert warm.provider_bond_bps > cold.provider_bond_bps
    assert warm.risk > 0


def test_an_urgent_buyer_tightens_the_deadline_after_non_delivery(both):
    """The direction the profile calls for.

    Urgent used to carry a zero buffer, which meant a bad history could only decline to
    lengthen the window and never actually tighten it.
    """
    cold = _buyer_terms(both["buyer"], [], profile="urgent")
    warm = _buyer_terms(both["buyer"], both["buyer"].recall(PROVIDER).evidence, profile="urgent")

    assert warm.service_window < cold.service_window


def test_a_cost_sensitive_buyer_grants_a_longer_window_instead(both):
    """Prior non-delivery does not universally imply a tighter deadline.

    A cautious buyer may rationally give a longer realistic window rather than a deadline the
    provider already failed once.
    """
    cold = _buyer_terms(both["buyer"], [], profile="budget")
    warm = _buyer_terms(both["buyer"], both["buyer"].recall(PROVIDER).evidence, profile="budget")

    assert warm.service_window > cold.service_window


def test_a_remembered_late_release_makes_the_provider_charge_more_and_wait_less(both):
    """The provider's own grievance, priced by the provider's own memory."""
    cold = _provider_terms(both["provider"], [])
    warm = _provider_terms(both["provider"], both["provider"].recall(BUYER).evidence)

    assert warm.price_wei > cold.price_wei
    assert warm.payout_delay < cold.payout_delay


def test_a_tightened_window_never_goes_below_what_a_provider_can_meet(both):
    """A deadline nobody can hit is not a harder bargain, it is a broken deal."""
    terms = produce_terms(
        evidence=both["buyer"].recall(PROVIDER).evidence,
        dimensions=load_dimensions(both["buyer"].memory),
        profile=PROFILES["urgent"],
        base_price_wei=BASE_PRICE,
        base_bond_bps=BASE_BOND_BPS,
        base_service_window=MIN_SERVICE_WINDOW_SECONDS + 60,
    )
    assert terms.service_window >= MIN_SERVICE_WINDOW_SECONDS


def test_a_deliberately_short_base_window_is_not_raised_to_the_floor(both):
    """The floor bounds the adjustment, it does not override a number the operator chose.

    The timeout demo asks for sixty seconds precisely because it wants the deal to expire.
    """
    terms = produce_terms(
        evidence=both["buyer"].recall(PROVIDER).evidence,
        dimensions=load_dimensions(both["buyer"].memory),
        profile=PROFILES["urgent"],
        base_price_wei=BASE_PRICE,
        base_bond_bps=BASE_BOND_BPS,
        base_service_window=60,
    )
    assert terms.service_window == 60


def test_a_shortened_payout_delay_has_a_floor_too(both):
    terms = produce_provider_terms(
        evidence=both["provider"].recall(BUYER).evidence,
        dimensions=load_dimensions(both["provider"].memory),
        persona=PERSONA,
        base_price_wei=BASE_PRICE,
        base_payout_delay=MIN_PAYOUT_DELAY_SECONDS + 30,
    )
    assert terms.payout_delay >= MIN_PAYOUT_DELAY_SECONDS


# --------------------------------------------------------------------------------------
# Neither grievance moves the other side's numbers
# --------------------------------------------------------------------------------------


def test_the_buyers_timeout_does_not_move_the_providers_terms(both):
    """Both stores hold both receipts. Only one of them is about the provider being wronged."""
    only_timeout = [row for row in both["provider"].recall(BUYER).evidence
                    if row["event_type"] == "timeout_claimed_without_delivery"]
    cold = _provider_terms(both["provider"], [])
    warm = _provider_terms(both["provider"], only_timeout)

    assert warm.price_wei == cold.price_wei
    assert warm.payout_delay == cold.payout_delay
    assert warm.used_evidence_ids == ()
    assert len(warm.recalled_event_ids) == 1, "recalled, and visibly so, but not used"


def test_the_late_release_does_not_move_the_buyers_bond(both):
    only_late = [row for row in both["buyer"].recall(PROVIDER).evidence
                 if row["event_type"] == "delivered_and_claimed_after_delay"]
    cold = _buyer_terms(both["buyer"], [], profile="urgent")
    warm = _buyer_terms(both["buyer"], only_late, profile="urgent")

    assert warm.provider_bond_bps == cold.provider_bond_bps
    assert warm.service_window == cold.service_window
    assert warm.used_evidence_ids == ()


def test_evidence_that_changed_nothing_is_recalled_but_never_committed(both):
    """The distinction the commitment depends on.

    A hash over everything recalled would describe what a side read. A hash over what it used
    describes what it reasoned from, which is the thing a receipt is supposed to explain.
    """
    from wrasse.policy_hash import EMPTY_EVIDENCE_HASH, evidence_hash

    evidence = both["provider"].recall(BUYER).evidence
    terms = _provider_terms(both["provider"], evidence)

    assert len(terms.recalled_event_ids) == 2
    assert len(terms.used_evidence_ids) == 1
    assert evidence_hash(terms.used_evidence_ids) != evidence_hash(terms.recalled_event_ids)
    assert evidence_hash(terms.used_evidence_ids) != EMPTY_EVIDENCE_HASH


def test_the_two_sides_commit_to_different_evidence(both):
    """Each side's hash covers its own reasons, so the two are not interchangeable."""
    from wrasse.policy_hash import evidence_hash

    buyer = _buyer_terms(both["buyer"], both["buyer"].recall(PROVIDER).evidence)
    provider = _provider_terms(both["provider"], both["provider"].recall(BUYER).evidence)

    assert buyer.used_evidence_ids != provider.used_evidence_ids
    assert evidence_hash(buyer.used_evidence_ids) != evidence_hash(provider.used_evidence_ids)


def test_a_cold_side_commits_to_the_empty_set(both):
    from wrasse.policy_hash import EMPTY_EVIDENCE_HASH, evidence_hash

    terms = _buyer_terms(both["buyer"], [])
    assert evidence_hash(terms.used_evidence_ids) == EMPTY_EVIDENCE_HASH

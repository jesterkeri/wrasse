"""Deterministic policy generation from recalled evidence and stored dimensions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from .dimensions import DimensionDefinition
from .evidence import SUBJECTS_OF
from .policy_hash import canonical_event_id
from .providers import ProviderPersona

#: A window this side may not tighten past, because a deadline the provider cannot physically
#: meet is not a harder bargain, it is a broken deal. Base Sepolia's safe head trails the tip by
#: roughly 66 seconds, and a provider has to clear its acceptance before it can deliver.
#:
#: It is a floor on the *adjustment*, never an override of a base the operator chose. A demo
#: that deliberately asks for 60 seconds because it wants a timeout still gets 60 seconds.
MIN_SERVICE_WINDOW_SECONDS = 300

#: Same shape, for the provider's side of the bargain.
MIN_PAYOUT_DELAY_SECONDS = 60


@dataclass(frozen=True)
class PriorityProfile:
    name: str
    contexts: frozenset[str]
    risk_weight: Decimal
    bond_sensitivity_bps: int
    discount_sensitivity_bps: int
    window_buffer_seconds: int


#: `window_buffer_seconds` is signed, and the sign is the whole point. Prior non-delivery does
#: not universally imply a tighter deadline: an urgent buyer wants one, while a cost- or
#: quality-sensitive buyer may rationally grant a longer realistic window instead. Urgent was 0
#: before, which meant it could only decline to lengthen, never actually tighten.
PROFILES = {
    "urgent": PriorityProfile(
        "urgent", frozenset({"deadline_sensitive"}), Decimal("1.40"), 3_000, 250, -1_800
    ),
    "budget": PriorityProfile(
        "budget", frozenset({"cost_sensitive"}), Decimal("0.85"), 1_000, 1_500, 3_600
    ),
    "sensitive": PriorityProfile(
        "sensitive", frozenset({"quality_sensitive"}), Decimal("1.20"), 2_500, 750, 1_800
    ),
}


@dataclass(frozen=True)
class DealTerms:
    profile: str
    price_wei: int
    provider_bond_bps: int
    service_window: int
    risk: Decimal
    #: Everything this side holds about the counterparty.
    recalled_event_ids: tuple[str, ...]
    #: Only the receipts that actually moved a number. The commitment is computed from these,
    #: so it describes the reasoning rather than the reading, and evidence that changed nothing
    #: can be shown without silently entering a hash.
    used_evidence_ids: tuple[str, ...]


def produce_terms(
    *,
    evidence: Iterable[dict[str, Any]],
    dimensions: Iterable[DimensionDefinition],
    profile: PriorityProfile,
    base_price_wei: int,
    base_bond_bps: int,
    base_service_window: int,
) -> DealTerms:
    """Apply dimensions generically; no dimension identifier is hard-coded."""

    if base_price_wei <= 0 or base_service_window <= 0:
        raise ValueError("base price and service window must be positive")
    events = _unique_by_event_id(evidence)
    risk, used = _score(
        events,
        dimensions,
        # A buyer prices what its counterparty did. A provider's own failure to deliver is not
        # a reason for that buyer to be charged more.
        about="provider",
        weight=profile.risk_weight,
        relevance=lambda dimension: (
            Decimal("1.5") if profile.contexts.intersection(dimension.applies_when) else Decimal("0.5")
        ),
    )

    bond_delta = _round_decimal(risk * profile.bond_sensitivity_bps)
    discount_bps = _round_decimal(risk * profile.discount_sensitivity_bps)
    price = base_price_wei * (10_000 - discount_bps) // 10_000

    # The floor applies to the adjustment, not to a base the caller chose deliberately.
    window_floor = min(base_service_window, MIN_SERVICE_WINDOW_SECONDS)
    service_window = max(
        window_floor, base_service_window + _round_decimal(risk * profile.window_buffer_seconds)
    )

    return DealTerms(
        profile=profile.name,
        price_wei=price,
        provider_bond_bps=min(10_000, base_bond_bps + bond_delta),
        service_window=service_window,
        risk=risk.quantize(Decimal("0.0001")),
        recalled_event_ids=tuple(sorted(canonical_event_id(str(e["event_id"])) for e in events)),
        used_evidence_ids=tuple(sorted(used)),
    )


@dataclass(frozen=True)
class ProviderTerms:
    """The provider's half of the bargain, from the provider's own memory of this buyer."""

    persona: str
    price_wei: int
    payout_delay: int
    risk: Decimal
    recalled_event_ids: tuple[str, ...]
    used_evidence_ids: tuple[str, ...]


def produce_provider_terms(
    *,
    evidence: Iterable[dict[str, Any]],
    dimensions: Iterable[DimensionDefinition],
    persona: ProviderPersona,
    base_price_wei: int,
    base_payout_delay: int,
) -> ProviderTerms:
    """The same risk model, spent on the two terms the provider actually controls.

    The provider has no task profile, so every dimension is equally relevant to it; what
    differs is what it does with the score. A buyer who made it wait for its money last time
    gets a higher price and a shorter payout delay, and how much shorter is exactly what
    `cashflow_sensitivity` means.
    """

    if base_price_wei <= 0 or base_payout_delay <= 0:
        raise ValueError("base price and payout delay must be positive")

    events = _unique_by_event_id(evidence)
    risk, used = _score(
        events, dimensions, about="buyer", weight=Decimal("1"), relevance=lambda _: Decimal("1")
    )

    price = base_price_wei * (10_000 + _round_decimal(risk * persona.price_sensitivity_bps)) // 10_000
    tightening = _round_decimal(
        risk * persona.delay_sensitivity_seconds * persona.cashflow_sensitivity
    )
    delay_floor = min(base_payout_delay, MIN_PAYOUT_DELAY_SECONDS)
    payout_delay = max(delay_floor, base_payout_delay - tightening)

    return ProviderTerms(
        persona=persona.name,
        price_wei=price,
        payout_delay=payout_delay,
        risk=risk.quantize(Decimal("0.0001")),
        recalled_event_ids=tuple(sorted(canonical_event_id(str(e["event_id"])) for e in events)),
        used_evidence_ids=tuple(sorted(used)),
    )


def _score(events, dimensions, *, about: str, weight: Decimal, relevance) -> tuple[Decimal, set[str]]:
    """The bounded risk score, and which receipts actually contributed to it.

    `about` names whose conduct this side is pricing. Both stores hold both receipts, so
    without it a provider's own non-delivery would feed the price that provider charges. What a
    side is entitled to react to is what its counterparty did.

    A receipt that is about the wrong party, matched no dimension, or whose contribution came
    to zero is recalled but not used. Committing to it would make the hash describe what was
    read rather than what was reasoned from.
    """

    definitions = tuple(dimensions)
    risk = Decimal("0")
    used: set[str] = set()
    for event in events:
        if about not in SUBJECTS_OF.get(str(event.get("event_type")), frozenset()):
            continue
        for dimension in definitions:
            if event.get("event_type") != dimension.source_event_type:
                continue
            magnitude = Decimal(str(dimension.severity)) * Decimal(str(dimension.confidence))
            signed = magnitude if dimension.signal_direction == "negative" else -magnitude
            contribution = signed * relevance(dimension) * weight
            if contribution == 0:
                continue
            risk += contribution
            used.add(canonical_event_id(str(event["event_id"])))
    return max(Decimal("0"), min(Decimal("1"), risk)), used


def _unique_by_event_id(evidence: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Collapse repeated recalls of the same receipt.

    Evidence is a set, and the commitment treats it as one. A memory layer that returned the
    same receipt twice would otherwise double its weight in the risk sum, moving terms
    against a counterparty on the strength of one event counted twice.
    """

    seen: dict[str, dict[str, Any]] = {}
    unique: list[dict[str, Any]] = []
    for event in evidence:
        identifier = canonical_event_id(str(event["event_id"]))
        # Compare the normalised record, so a differently spelled id is the same record and
        # only a genuinely different body counts as a conflict.
        normalised = {**event, "event_id": identifier}
        previous = seen.get(identifier)
        if previous is not None:
            if previous != normalised:
                # Same receipt, two different stories. Keeping whichever arrived first would
                # make the terms depend on result ordering while the commitment stayed put.
                raise ValueError(f"conflicting records share event id {identifier}")
            continue
        seen[identifier] = normalised
        unique.append(normalised)
    return tuple(unique)


def _round_decimal(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


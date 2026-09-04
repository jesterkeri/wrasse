"""Deterministic policy generation from recalled evidence and stored dimensions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from .dimensions import DimensionDefinition


@dataclass(frozen=True)
class PriorityProfile:
    name: str
    contexts: frozenset[str]
    risk_weight: Decimal
    bond_sensitivity_bps: int
    discount_sensitivity_bps: int
    window_buffer_seconds: int


PROFILES = {
    "urgent": PriorityProfile(
        "urgent", frozenset({"deadline_sensitive"}), Decimal("1.40"), 3_000, 250, 0
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
    evidence_event_ids: tuple[str, ...]


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
    definitions = tuple(dimensions)
    risk = Decimal("0")
    for event in events:
        for dimension in definitions:
            if event.get("event_type") != dimension.source_event_type:
                continue
            relevance = Decimal("1.5") if profile.contexts.intersection(dimension.applies_when) else Decimal("0.5")
            magnitude = Decimal(str(dimension.severity)) * Decimal(str(dimension.confidence))
            signed = magnitude if dimension.signal_direction == "negative" else -magnitude
            risk += signed * relevance * profile.risk_weight
    risk = max(Decimal("0"), min(Decimal("1"), risk))

    bond_delta = _round_decimal(risk * profile.bond_sensitivity_bps)
    discount_bps = _round_decimal(risk * profile.discount_sensitivity_bps)
    price = base_price_wei * (10_000 - discount_bps) // 10_000
    service_window = base_service_window + _round_decimal(
        risk * profile.window_buffer_seconds
    )
    identifiers = tuple(sorted(str(event["event_id"]) for event in events))
    return DealTerms(
        profile=profile.name,
        price_wei=price,
        provider_bond_bps=min(10_000, base_bond_bps + bond_delta),
        service_window=service_window,
        risk=risk.quantize(Decimal("0.0001")),
        evidence_event_ids=identifiers,
    )


def _unique_by_event_id(evidence: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Collapse repeated recalls of the same receipt.

    Evidence is a set, and the commitment treats it as one. A memory layer that returned the
    same receipt twice would otherwise double its weight in the risk sum, moving terms
    against a counterparty on the strength of one event counted twice.
    """

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for event in evidence:
        identifier = str(event["event_id"])
        if identifier in seen:
            continue
        seen.add(identifier)
        unique.append(event)
    return tuple(unique)


def _round_decimal(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


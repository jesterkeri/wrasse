"""Deterministic policy generation from recalled evidence and stored dimensions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from .constants import (
    CONCESSION_DEN,
    CONCESSION_NUM,
    IRRELEVANT_MULTIPLIER,
    MAX_BOND_BPS,
    MIN_PAYOUT_DELAY_SECONDS,
    INTEGER_QUANTUM,
    MIN_SERVICE_WINDOW_SECONDS,
    PROFILE_FIELDS,
    PROVIDER_RISK_WEIGHT,
    RELEVANT_MULTIPLIER,
    RISK_CEILING,
    RISK_DISPLAY_QUANTUM,
    RISK_FLOOR,
    ROUNDING,
)
from .dimensions import DimensionDefinition
from .evidence import SUBJECTS_OF
from .negotiation import clamp_bond_bps, clamp_duration
from .policy_hash import BPS_DENOMINATOR, canonical_event_id
from .providers import ProviderPersona


@dataclass(frozen=True)
class PriorityProfile:
    """One task profile. Every field is in `PROFILE_FIELDS` and therefore in the digest.

    `bond_sensitivity_bps` and `window_buffer_seconds` move this profile's own proposals with
    its memory of the provider. `price_ceiling_premium_bps` and `payout_delay_floor_bps` are
    the limits it publishes to the provider, and neither has a risk term: a limit that moved
    with the *counterparty's* misconduct made this side better off as it was wronged more and
    then refused outright, punishing the party that suffered rather than the one that caused it.
    """

    name: str
    contexts: frozenset[str]
    risk_weight: Decimal
    bond_sensitivity_bps: int
    window_buffer_seconds: int
    price_ceiling_premium_bps: int
    payout_delay_floor_bps: int


PROFILES = {
    name: PriorityProfile(
        name=name,
        contexts=frozenset(fields["contexts"]),
        risk_weight=Decimal(fields["risk_weight"]),
        bond_sensitivity_bps=fields["bond_sensitivity_bps"],
        window_buffer_seconds=fields["window_buffer_seconds"],
        price_ceiling_premium_bps=fields["price_ceiling_premium_bps"],
        payout_delay_floor_bps=fields["payout_delay_floor_bps"],
    )
    for name, fields in PROFILE_FIELDS.items()
}


@dataclass(frozen=True)
class DealTerms:
    """The buyer's half: what it proposes, and the limits it publishes to the provider."""

    profile: str
    provider_bond_bps: int
    service_window: int
    #: The most this profile will pay, in basis points of the operator's baseline. Baseline
    #: relative rather than absolute wei, so it does not silently break when the baseline moves.
    max_price_bps: int
    #: The least payout delay this profile will accept, in seconds.
    min_payout_delay: int
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
    base_payout_delay: int,
) -> DealTerms:
    """Apply dimensions generically; no dimension identifier is hard-coded."""

    if base_price_wei <= 0 or base_service_window <= 0 or base_payout_delay <= 0:
        raise ValueError("base price, service window and payout delay must be positive")
    events = _unique_by_event_id(evidence)
    risk, used = _score(
        events,
        dimensions,
        # A buyer prices what its counterparty did. A provider's own failure to deliver is not
        # a reason for that buyer to be charged more.
        about="provider",
        weight=profile.risk_weight,
        relevance=lambda dimension: (
            Decimal(RELEVANT_MULTIPLIER) if profile.contexts.intersection(dimension.applies_when)
            else Decimal(IRRELEVANT_MULTIPLIER)
        ),
    )

    def committed(subset) -> tuple[int, int]:
        """Every number this side contributes that depends on risk.

        Only these two. The buyer's limits are constants, and a constant cannot discriminate
        between histories, so including them would add nothing to the minimality search but
        cost. What is *not* here is the settlement: running the search over a function of the
        counterparty's evidence would drop a receipt that genuinely moved this side's proposal
        whenever the other side's limit happened to bind, and the document would then say the
        buyer used nothing beside a risk of 1.0000.
        """

        partial, _ = _score(
            subset, dimensions, about="provider", weight=profile.risk_weight,
            relevance=lambda dimension: (
                Decimal(RELEVANT_MULTIPLIER) if profile.contexts.intersection(dimension.applies_when)
                else Decimal(IRRELEVANT_MULTIPLIER)
            ),
        )
        return (
            clamp_bond_bps(
                base_bond_bps + _round_decimal(partial * profile.bond_sensitivity_bps)
            ),
            clamp_duration(
                max(
                    min(base_service_window, MIN_SERVICE_WINDOW_SECONDS),
                    base_service_window + _round_decimal(partial * profile.window_buffer_seconds),
                )
            ),
        )

    bond, service_window = committed(events)

    return DealTerms(
        profile=profile.name,
        provider_bond_bps=bond,
        service_window=service_window,
        # No risk term in either limit. See `PriorityProfile`.
        max_price_bps=BPS_DENOMINATOR + profile.price_ceiling_premium_bps,
        min_payout_delay=clamp_duration(
            base_payout_delay * profile.payout_delay_floor_bps // BPS_DENOMINATOR
        ),
        risk=risk.quantize(Decimal(RISK_DISPLAY_QUANTUM)),
        recalled_event_ids=tuple(sorted(canonical_event_id(str(e["event_id"])) for e in events)),
        used_evidence_ids=_minimal_causal_set(events, used, committed),
    )


@dataclass(frozen=True)
class ProviderTerms:
    """The provider's half of the bargain, from the provider's own memory of this buyer."""

    persona: str
    #: The ask, in basis points of the operator's baseline. Basis points rather than wei
    #: because the comparison happens here and the conversion happens once, at the end: at a
    #: small baseline two distinct rates floor to the same wei and the settlement would be a
    #: coin toss.
    price_bps: int
    payout_delay: int
    #: The least this provider will take, above the baseline. It concedes most of what memory
    #: added and not all of it, because the reason it added it has not gone away. This is the
    #: one walk-away that is not simply the baseline, and it is what makes a refusal on price
    #: possible at all.
    price_floor_bps: int
    #: The most bond it will post. Falls as its memory of *this buyer* worsens, which is
    #: monotone against the party that caused it: a worse buyer gets less protection posted.
    max_bond_bps: int
    #: The least time it needs to deliver. A physical capability, so it carries no risk term:
    #: a buyer that paid late does not make delivery take longer. Never above the operator's
    #: own baseline, so a deliberately short window is not overridden.
    min_service_window: int
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
    base_service_window: int,
) -> ProviderTerms:
    """The same risk model, spent on the two terms the provider actually controls.

    The provider has no task profile, so every dimension is equally relevant to it; what
    differs is what it does with the score. A buyer who made it wait for its money last time
    gets a higher price and a shorter payout delay, and how much shorter is exactly what
    `cashflow_sensitivity` means.
    """

    if base_price_wei <= 0 or base_payout_delay <= 0 or base_service_window <= 0:
        raise ValueError("base price, payout delay and service window must be positive")

    events = _unique_by_event_id(evidence)
    risk, used = _score(
        events, dimensions, about="buyer", weight=Decimal(PROVIDER_RISK_WEIGHT),
        relevance=lambda _: Decimal(PROVIDER_RISK_WEIGHT)
    )

    def committed(subset) -> tuple[int, int, int, int]:
        """Every number this side contributes that depends on risk: four, not two.

        The two limits are here because they can settle a term on their own. A receipt that
        moved only the bond ceiling used to be recalled and never committed, so the hash
        described less than the reasoning did.

        `min_service_window` is absent on purpose: it is a constant and cannot discriminate.
        """

        partial, _ = _score(
            subset, dimensions, about="buyer", weight=Decimal(PROVIDER_RISK_WEIGHT),
            relevance=lambda _: Decimal(PROVIDER_RISK_WEIGHT),
        )
        premium = _round_decimal(partial * persona.price_sensitivity_bps)
        return (
            BPS_DENOMINATOR + premium,
            clamp_duration(
                max(
                    min(base_payout_delay, MIN_PAYOUT_DELAY_SECONDS),
                    base_payout_delay
                    - _round_decimal(
                        partial * persona.delay_sensitivity_seconds * persona.cashflow_sensitivity
                    ),
                )
            ),
            clamp_bond_bps(
                MAX_BOND_BPS
                - _round_decimal(partial * MAX_BOND_BPS * persona.cashflow_sensitivity)
            ),
            # Three quarters of the way back, never further. `_round_decimal` is monotone and
            # the share is below one, so the floor can never rise above the ask, which would
            # have the provider refusing its own proposal.
            BPS_DENOMINATOR
            + _round_decimal(
                partial
                * persona.price_sensitivity_bps
                * Decimal(CONCESSION_NUM)
                / Decimal(CONCESSION_DEN)
            ),
        )

    price_bps, payout_delay, max_bond_bps, price_floor_bps = committed(events)

    return ProviderTerms(
        persona=persona.name,
        price_bps=price_bps,
        payout_delay=payout_delay,
        price_floor_bps=price_floor_bps,
        max_bond_bps=max_bond_bps,
        min_service_window=clamp_duration(
            min(MIN_SERVICE_WINDOW_SECONDS, base_service_window)
        ),
        risk=risk.quantize(Decimal(RISK_DISPLAY_QUANTUM)),
        recalled_event_ids=tuple(sorted(canonical_event_id(str(e["event_id"])) for e in events)),
        used_evidence_ids=_minimal_causal_set(events, used, committed),
    )


def _minimal_causal_set(events, candidates: set[str], committed) -> tuple[str, ...]:
    """The receipts that actually moved a committed number.

    Contributing to the risk sum is not the same as changing an answer. A contribution can be
    clamped, rounded away, offset by another, or land against a cap or a floor, and the terms
    come out identical to a cold start. A hash over those would claim a receipt explained a
    number it did not touch.

    The rule is a minimal set: drop candidates one at a time, in a fixed order, while the
    committed terms stay the same. What remains has the property the name promises, because
    removing any one of them changes at least one committed output. Where several receipts only
    matter together, against a cap say, the set keeps as many as are needed and no more.

    **Dropping is repeated to a fixed point.** One pass is not enough. Removing a later
    receipt can make an earlier one that was already kept redundant, and a single pass never
    reconsiders it, so the set could still name a receipt whose removal changes nothing. Two
    offsetting receipts are the ordinary case: each looks necessary while the other is
    present, and once one goes the other stops mattering. The loop runs until a whole pass
    removes nothing, which is when every survivor is individually load-bearing.

    Minimal here means no single member can be removed. It does not mean smallest: where
    members are jointly necessary the result can depend on the order they are tried, so the
    order is fixed and sorted rather than arbitrary, and the same evidence always produces the
    same set and therefore the same commitment.
    """

    baseline = committed(events)
    keep = sorted(candidates)
    changed = True
    while changed:
        changed = False
        for identifier in list(keep):
            trial = [item for item in keep if item != identifier]
            retained = set(trial)
            without = [
                event for event in events
                if canonical_event_id(str(event["event_id"])) in retained
            ]
            if committed(without) == baseline:
                keep = trial
                changed = True
    return tuple(keep)


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
    return max(Decimal(RISK_FLOOR), min(Decimal(RISK_CEILING), risk)), used


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
    # The mode and the quantum both come from the manifest, so the numbers that are hashed
    # are the numbers that round. Python's decimal rounding modes are plain strings, which is
    # what makes the hashed value directly usable rather than merely descriptive.
    return int(value.quantize(Decimal(INTEGER_QUANTUM), rounding=ROUNDING))


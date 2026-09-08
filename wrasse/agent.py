"""One side of the negotiation, computed by a process that can only see one memory.

**What this changes about the claim.** Until now a single process opened both memories and
computed both halves. The memories were separate and the reasoning was not, so "two agents" was
a description of the model rather than a fact about the running system, and it was the weakest
sentence in the pitch. This module is the half that one side computes. Run it twice, in two
processes, each given only its own store, and the claim becomes literal.

**What a side is allowed to publish.** Exactly the numbers the settlement rule reads: what it
proposes, the limits it publishes against the other's proposals, its own walk-aways, the risk
it derived, which receipts it holds and which of them actually moved a number. Nothing else
crosses. In particular a side never publishes its ontology, because a dimension is how it reads
an outcome and the other side reading it would make one memory out of two.

**Why the held receipts are published even though they are not terms.** The single-process
version asserted internally that both memories held the same set, and refused to quote across a
gap. Split apart, that assertion has to become part of the exchange: each side says what it
holds, and a disagreement stops the quote. Moving it from an assertion to a published fact is
the honest version of the same check, because now neither side is trusted to speak for the
other.

**Nothing here reads the counterparty's store.** Not defensively, structurally: this function
is handed one store and has no way to find another. The command that wraps it removes the other
side's path from its own environment before it runs, so a bug that tried could not succeed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .dimensions import load_dimensions
from .engine import PROFILES, produce_provider_terms, produce_terms

#: Roles that can be published. Not an open string: a third role would have no terms to
#: propose and no limits to publish, and the settlement reads exactly two sides.
ROLES = ("buyer", "provider")


class AgentError(RuntimeError):
    """This side cannot publish, and the reason names what is missing."""


def _decimal(value: Decimal) -> str:
    return f"{value:.4f}"


def _readable(evidence, dimensions) -> None:
    """Refuse to publish over an outcome this side has no reading for.

    The same rule the joint path enforced, kept on the side that owns the memory. A side that
    priced an unreadable receipt as zero would be publishing a number that says the outcome was
    harmless, which is a claim it has no basis for.
    """

    missing = sorted({
        row["event_type"] for row in evidence
        if not any(d.source_event_type == row["event_type"] for d in dimensions)
    })
    if missing:
        raise AgentError(f"this memory holds verified events with no dimension yet: {missing}")


def publish_positions(
    *,
    role: str,
    store: Any,
    counterparty: str,
    base_price_wei: int,
    base_bond_bps: int,
    base_service_window: int,
    base_payout_delay: int,
    persona: Any = None,
    evidence: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """This side's published numbers, from this side's memory alone.

    `evidence` is supplied only by the simulator, which asks what this side would publish given
    a history it chose. Left unset, the side recalls its own, which is the live path.
    """

    if role not in ROLES:
        raise AgentError(f"{role!r} is not a side of this negotiation")

    recalled = None
    if evidence is None:
        recalled = store.recall(counterparty)
        evidence = tuple(recalled.evidence)
    dimensions = load_dimensions(store.memory)
    _readable(evidence, dimensions)

    published: dict[str, Any] = {
        "role": role,
        "counterparty": counterparty,
        "verdict": recalled.verdict if recalled is not None else "supplied",
        "cold_start": bool(recalled.is_cold_start) if recalled is not None else not evidence,
        # The cross-check that replaces the joint path's internal assertion. Sorted so two
        # sides that hold the same receipts publish the same string.
        "held_event_ids": sorted(str(row["event_id"]) for row in evidence),
        "recalled_evidence": [dict(row) for row in evidence],
    }

    if role == "provider":
        if persona is None:
            raise AgentError("the provider publishes under a committed persona and none was given")
        terms = produce_provider_terms(
            evidence=evidence, dimensions=dimensions, persona=persona,
            base_price_wei=base_price_wei, base_payout_delay=base_payout_delay,
            base_service_window=base_service_window,
        )
        published.update({
            "risk": _decimal(terms.risk),
            "persona": {"name": terms.persona},
            "proposes": {"price_bps": terms.price_bps, "payout_delay": terms.payout_delay},
            "limits": {
                "max_bond_bps": terms.max_bond_bps,
                "min_service_window": terms.min_service_window,
            },
            "walkaways": {"price_floor_bps": terms.price_floor_bps},
            "used_evidence_ids": list(terms.used_evidence_ids),
        })
        return published

    # The buyer publishes one set per profile, because a profile is a way of posting the same
    # job and each one proposes differently. The provider has no profiles: it is one seller
    # with one persona, and it answers whatever it is asked.
    profiles = {}
    risk = None
    used: dict[str, list[str]] = {}
    for name, profile in PROFILES.items():
        terms = produce_terms(
            evidence=evidence, dimensions=dimensions, profile=profile,
            base_price_wei=base_price_wei, base_bond_bps=base_bond_bps,
            base_service_window=base_service_window, base_payout_delay=base_payout_delay,
        )
        risk = terms.risk if risk is None else risk
        profiles[name] = {
            "proposes": {
                "provider_bond_bps": terms.provider_bond_bps,
                "service_window": terms.service_window,
            },
            "limits": {
                "max_price_bps": terms.max_price_bps,
                "min_payout_delay": terms.min_payout_delay,
            },
        }
        used[name] = list(terms.used_evidence_ids)
    published.update({
        "risk": _decimal(risk if risk is not None else Decimal(0)),
        "profiles": profiles,
        "used_evidence_ids": used,
    })
    return published

"""What the two agents would agree, given a history you choose rather than one that happened.

**Why this is not a second engine.** Everything here assembles inputs and then calls the same
`produce_terms` and `produce_provider_terms` the live quote calls, through the same document
writer. A simulator that reimplemented the arithmetic would drift from the thing it claims to
predict, and the first person to notice would be a judge comparing two numbers on one page.

**What is hypothetical and what is not.** The outcomes are supplied, so they are hypothetical
and are labelled that way at every level that leaves this module. Everything else is real: the
settlement rule, the limits, the persona and its commitment, and the ontology. That last one is
worth being precise about, because it is the part a reader is most likely to assume is faked.
A dimension is a reading of an *outcome type*, learned once and reused, not a reading of one
receipt. The readings used here are the ones the two live memories actually hold. So the claim
this module supports is exact: given outcomes of these kinds, and the readings these two agents
really have of such outcomes, this is what they would settle on.

**Nothing here writes to a store.** Not the simulated evidence, not a derived dimension,
nothing. A simulation that could deposit a receipt into a memory would make every later quote
unfalsifiable, and the whole argument of this project is that the receipts are checkable.

**An outcome the agents cannot read is refused by name.** Both memories hold dimensions for the
two outcomes that have actually settled on Base. Until a third settles and is read, a history
containing it cannot be priced, and this says so and names it rather than scoring it as zero.
Scoring it as zero would be indistinguishable from the outcome being harmless, which is the
opposite of unknown.
"""

from __future__ import annotations

from typing import Any, Sequence

from web3 import Web3

from .constants import SUBJECTS_OF
from .dimensions import load_dimensions
from .engine import produce_provider_terms, produce_terms
from .memory_gate import EvidenceRecall
from .policy_hash import canonical_event_id

#: The outcomes a history may contain. Exactly the closed set the contract can produce, so a
#: simulation cannot explore a world the escrow could not reach.
OUTCOMES: tuple[str, ...] = tuple(sorted(SUBJECTS_OF))

#: How many outcomes one simulated history may hold. The causal-set search behind `used`
#: evidence is quadratic, and a history nobody could have accumulated is not a better argument.
MAX_HISTORY = 12


class SimulationRefused(RuntimeError):
    """The history cannot be priced, and the reason names what is missing."""


def _event_id(index: int, event_type: str) -> str:
    """A stable, obviously synthetic identifier for one hypothetical outcome.

    Derived rather than random so the same history produces the same document twice, which is
    what makes a simulated result something a reader can check by repeating it. The prefix is
    not decoration: an identifier that could collide with a real receipt's would let a
    simulated outcome be mistaken for one that settled.
    """

    return canonical_event_id(
        Web3.keccak(text=f"wrasse-simulation:{index}:{event_type}").hex()
    )


def build_history(
    outcomes: Sequence[str], *, buyer: str, provider: str
) -> tuple[dict[str, Any], ...]:
    """Turn a list of outcome names into the evidence rows the engine reads.

    Both sides receive every row, which is how the real system works: the same public receipts
    reach both memories and `SUBJECTS_OF` decides which side each one is about. Handing each
    side a different list here would simulate a system nobody built.
    """

    if len(outcomes) > MAX_HISTORY:
        raise SimulationRefused(
            f"a simulated history holds at most {MAX_HISTORY} outcomes, and this one has "
            f"{len(outcomes)}"
        )
    rows = []
    for index, event_type in enumerate(outcomes):
        if event_type not in SUBJECTS_OF:
            raise SimulationRefused(
                f"{event_type!r} is not an outcome this escrow can produce. The closed set is "
                + ", ".join(OUTCOMES)
            )
        rows.append({
            "event_id": _event_id(index, event_type),
            "event_type": event_type,
            "buyer": buyer,
            "provider": provider,
            "simulated": True,
        })
    return tuple(rows)


def simulated_quote(
    *,
    stores: dict[str, Any],
    buyer: str,
    provider: str,
    outcomes: Sequence[str],
    base_price_wei: int,
    base_bond_bps: int,
    base_service_window: int,
    base_payout_delay: int,
    persona: Any,
    quote_class: Any,
) -> Any:
    """The same `BilateralQuote` the live path produces, over a history you chose.

    `stores` is read for one thing only, the ontology, and never written. `quote_class` and
    `persona` are passed in rather than imported so this module does not depend on `cli`, which
    imports the world; `cli` already owns both and hands them over.
    """

    buyer = Web3.to_checksum_address(buyer)
    provider = Web3.to_checksum_address(provider)
    evidence = build_history(outcomes, buyer=buyer, provider=provider)

    # Each side reads its own ontology, exactly as the live quote does. They tend to agree,
    # because both were shown the same public receipts, and that is a different fact from
    # sharing one database.
    dimensions = {side: load_dimensions(stores[side].memory) for side in ("buyer", "provider")}

    for side, held in dimensions.items():
        unreadable = sorted({
            row["event_type"] for row in evidence
            if not any(d.source_event_type == row["event_type"] for d in held)
        })
        if unreadable:
            raise SimulationRefused(
                f"the {side}'s memory has no reading yet for {', '.join(unreadable)}. A "
                "dimension is learned once from an outcome that really settled, and this one "
                "has not settled on Base yet. Scoring it as zero would say it was harmless, "
                "which is a different claim from not knowing."
            )

    recall = {
        "buyer": EvidenceRecall(provider, evidence, "match" if evidence else "empty_store"),
        "provider": EvidenceRecall(buyer, evidence, "match" if evidence else "empty_store"),
    }

    provider_terms = produce_provider_terms(
        evidence=recall["provider"].evidence,
        dimensions=dimensions["provider"],
        persona=persona,
        base_price_wei=base_price_wei,
        base_payout_delay=base_payout_delay,
        base_service_window=base_service_window,
    )
    from .engine import PROFILES

    buyer_terms = {
        name: produce_terms(
            evidence=recall["buyer"].evidence,
            dimensions=dimensions["buyer"],
            profile=profile,
            base_price_wei=base_price_wei,
            base_bond_bps=base_bond_bps,
            base_service_window=base_service_window,
            base_payout_delay=base_payout_delay,
        )
        for name, profile in PROFILES.items()
    }
    return quote_class(
        recall, persona, provider_terms, buyer_terms,
        baseline={
            "price_wei": base_price_wei,
            "provider_bond_bps": base_bond_bps,
            "service_window": base_service_window,
            "payout_delay": base_payout_delay,
        },
    )

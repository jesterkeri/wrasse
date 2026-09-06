"""Resolving two openings into one deal, or saying plainly that no deal exists.

Until this module, the four terms were taken unilaterally: bond and window from the buyer,
price and payout delay from the provider, with nothing between them. That is two monologues
stapled into one document, not a bargain.

**The shape.** Every term has one *proposer* and one *opposer*. The opposer publishes a limit,
the proposer publishes a walk-away, and the settlement is a comparison of four numbers. It is
a pure function: given the same eight numbers it always produces the same result, and a reader
holding only the document can reproduce it without consulting either memory.

**Which limits move.** A limit may move with its publisher's memory, but only if the movement
is monotone *against the party whose conduct moved it*: a worse record must make that party's
own outcome worse across the whole risk range, with no interval where it improves. The
provider's bond ceiling and price floor pass that test. A buyer price ceiling that fell with
the provider's misconduct failed it: the buyer paid less as it was wronged more, an
improvement, and then fell off a cliff into refusal with nothing at all. So the buyer's
ceiling is a profile constant and the inversion is gone.

**The monotonicity claim is component-wise and it excludes the service window.** Stated
exactly: a worse provider record cannot lower the price it is paid, cannot lower the bond it
must post, and cannot turn a deal into a refusal. It *can* lengthen the service window,
because `budget` and `sensitive` carry positive window buffers on purpose, and a cost- or
quality-sensitive buyer that has been let down may rationally grant a longer realistic
deadline rather than a tighter one. That is an improvement for a provider whose own limit on
the term is a minimum, and it is a deliberate design choice rather than an oversight. Claiming
blanket monotonicity would be false, and the honest statement is the narrow one.

**Attribution.** A number moves for one of three reasons and they are not interchangeable. A
memory adjustment cites receipts. A negotiation concession cites the counterparty's published
limit. A fixed limit binding cites the rule by name. "A receipt behind every number that
moved" is false, and writing it would let a constant masquerade as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .constants import (
    BASELINE_BOUNDS,
    BASELINE_WIDENED_DOWN,
    BASELINE_WIDENED_UP,
    CEILING,
    CONCESSION,
    FLOOR,
    LIMIT_KINDS,
    LIMIT_NAMES,
    MAX_BOND_BPS,
    MAX_PRICE_BPS,
    MEMORY,
    MIN_SETTLED_PRICE_WEI,
    PUBLISHED,
    RULE,
    TERM_SHAPES,
    TERMS,
    WALKAWAY_RULES,
)
from .policy_hash import _UINT256_MAX, BPS_DENOMINATOR, MAX_DURATION, MAX_PROVIDER_BOND_BPS

#: `CEILING` and `FLOOR` say which way an opposer's limit points: a ceiling admits proposals at
#: or below it, a floor admits proposals at or above it. `TERMS` is the fixed order the four are
#: settled and displayed in. `TERM_SHAPES` pairs them up.
#:
#: All three come from `constants`, and that is the point of this line rather than an accident
#: of layout. They sat here as a private dict, outside the manifest, which meant flipping
#: `price_bps` from a ceiling to a floor would invert every price outcome this build produces
#: and leave `ENGINE_VERSION` exactly as it was. A constant that can change a settled term
#: belongs to the digest that claims to cover the settled term.


class NoOverlap(ValueError):
    """The two sides left no value either would accept, so there is no deal to quote.

    Not an error in the ordinary sense. It is an outcome, and saying it is a better answer
    than quoting terms one side has already refused.
    """


@dataclass(frozen=True)
class Position:
    """One side's published numbers for one term."""

    proposal: int
    #: The counterparty's limit on this term.
    limit: int
    #: The furthest this proposer will concede. Never past its own baseline, and never past
    #: its own proposal: a side does not walk away from a number it just offered.
    walkaway: int
    #: What the limit is, so a refusal can name it rather than a bare number.
    limit_name: str
    #: `MEMORY` if the limit moves with its publisher's evidence, `RULE` if it is a constant.
    limit_kind: str


@dataclass(frozen=True)
class Move:
    """One movement, and the only honest account of what caused it."""

    term: str
    from_value: int
    to_value: int
    kind: str
    #: For a concession, the limit that bound. For a rule, the constant that bound. For a
    #: memory adjustment, this is not used: those are explained by the evidence commitment.
    because: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "from": self.from_value,
            "to": self.to_value,
            "kind": self.kind,
            "because": self.because,
        }


@dataclass(frozen=True)
class Settlement:
    agreed: bool
    terms: dict[str, int]
    moves: tuple[Move, ...]
    failed_on: str | None
    #: How far apart the two sides were on the term that failed, in that term's own units.
    gap: int | None

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"agreed": self.agreed}
        if self.agreed:
            body["moves"] = [move.as_dict() for move in self.moves]
        else:
            body["failed_on"] = self.failed_on
            body["gap"] = self.gap
        return body


def _settle_one(term: str, position: Position) -> tuple[int, Move | None]:
    """Settle one term, or raise `NoOverlap` naming it and the distance.

    Two shapes and no branch on the profile. Where the opposer publishes a **ceiling**, the
    settlement is `min(proposal, ceiling)` and it agrees when the ceiling is at least the
    proposer's walk-away. Where the opposer publishes a **floor**, it is `max(proposal, floor)`
    and it agrees when the floor is at most the walk-away.

    Comparisons are `<=` and `>=`, so a limit exactly equal to a proposal is an agreement that
    settles at that value. That is a deliberate choice at a boundary one unit wide.
    """

    shape = TERM_SHAPES[term]
    proposal, limit, walkaway = position.proposal, position.limit, position.walkaway

    if shape == CEILING:
        if proposal <= limit:
            return proposal, None
        if limit < walkaway:
            raise NoOverlap(term, walkaway - limit)
        settled = limit
    else:
        if proposal >= limit:
            return proposal, None
        if limit > walkaway:
            raise NoOverlap(term, limit - walkaway)
        settled = limit

    kind = CONCESSION if position.limit_kind == MEMORY else RULE
    return settled, Move(
        term=term,
        from_value=proposal,
        to_value=settled,
        kind=kind,
        because=f"{position.limit_name}={limit}",
    )


def settle(positions: dict[str, Position]) -> Settlement:
    """Resolve all four terms, or report the first that has no overlap.

    Terms are settled in a fixed order so that a refusal names the same term every time for
    the same inputs. Nothing about one term's outcome feeds another's: each is decided by its
    own four numbers, which is what makes the whole thing reproducible from the document.
    """

    missing = sorted(set(TERMS) - set(positions))
    if missing:
        raise ValueError(f"no position published for {', '.join(missing)}")

    settled: dict[str, int] = {}
    moves: list[Move] = []
    for term in TERMS:
        try:
            value, move = _settle_one(term, positions[term])
        except NoOverlap as failure:
            failed_term, gap = failure.args
            return Settlement(
                agreed=False, terms={}, moves=(), failed_on=failed_term, gap=gap
            )
        settled[term] = value
        if move is not None:
            moves.append(move)

    return Settlement(
        agreed=True, terms=settled, moves=tuple(moves), failed_on=None, gap=None
    )


# --------------------------------------------------------------------------------------
# Building the four positions, in one place
#
# The writer built them and the reader rebuilt them, from two copies of the same three `min`
# and `max` rules. Two copies of a rule are free to drift, and neither copy was in the digest
# claiming to describe the settlement. Worse, three of the four walk-aways were never published
# at all, so a refusal on bond, window or delay could not be checked from the document: with a
# bond proposal of 3 500, a ceiling of 400 and a baseline of 500, the unpublished rule is the
# only fact separating "refuse by 100" from "settle at 400".
#
# So the rules are named, hashed and applied here, the walk-aways are published, and the
# reader checks the published number against the rule rather than trusting either.
# --------------------------------------------------------------------------------------


def derived_walkaway(term: str, baseline: dict[str, int], proposal: int) -> int | None:
    """The walk-away `WALKAWAY_RULES` gives for a term, or `None` when the rule is `PUBLISHED`.

    `None` is not a failure. Price is published rather than derived because the provider
    concedes only part of the way back, and how far is a function of its memory rather than of
    the baseline. Every other term concedes to the baseline, widened to include the side's own
    proposal.
    """

    rule = WALKAWAY_RULES[term]
    if rule == PUBLISHED:
        return None
    if term not in baseline:
        raise ValueError(
            f"{term} concedes toward the baseline under {rule!r}, and the baseline has no "
            f"{term}. A derived rule needs a baseline field of the same name."
        )
    base = baseline[term]
    if rule == BASELINE_WIDENED_DOWN:
        return min(base, proposal)
    if rule == BASELINE_WIDENED_UP:
        return max(base, proposal)
    raise ValueError(f"no walk-away rule named {rule!r}")


def walkaways_for(
    baseline: dict[str, int], proposals: dict[str, int], published: dict[str, int]
) -> dict[str, int]:
    """Every term's walk-away: derived where there is a rule, taken as given where there is not.

    Called by the writer to fill the document. The reader calls `build_positions`, which
    applies the same rules and refuses a published number that disagrees with one.
    """

    resolved = {}
    for term in TERMS:
        derived = derived_walkaway(term, baseline, proposals[term])
        if derived is None:
            if term not in published:
                raise ValueError(f"{term} has no derivation rule and none was published")
            resolved[term] = published[term]
        else:
            resolved[term] = derived
    return resolved


def build_positions(
    *,
    baseline: dict[str, int],
    proposals: dict[str, int],
    limits: dict[str, int],
    walkaways: dict[str, int],
) -> dict[str, Position]:
    """The four positions a settlement is decided by, from published numbers alone.

    The one place they are built. The writer feeds it what memory produced and the reader feeds
    it what the document says, so the thing checked and the thing produced cannot be two
    different rules that happen to agree on today's data.

    A walk-away with a derivation rule is recomputed here and the published value must match.
    Publishing it as well as deriving it is not redundant: a reader who does not know Wrasse's
    rules still gets the number, and a reader who does gets a proof that the number is the one
    the rules give.
    """

    missing = sorted(set(TERMS) - set(proposals)) + sorted(set(TERMS) - set(limits))
    if missing:
        raise ValueError(f"no position published for {', '.join(missing)}")

    positions = {}
    for term in TERMS:
        if term not in walkaways:
            raise ValueError(f"no walk-away published for {term}")
        expected = derived_walkaway(term, baseline, proposals[term])
        if expected is not None and walkaways[term] != expected:
            raise ValueError(
                f"{term} publishes a walk-away of {walkaways[term]} where "
                f"{WALKAWAY_RULES[term]} over a baseline of {baseline[term]} and a proposal of "
                f"{proposals[term]} gives {expected}. A walk-away decides whether this term "
                "agrees or refuses, so a document may not choose its own."
            )
        positions[term] = Position(
            proposal=proposals[term],
            limit=limits[term],
            walkaway=walkaways[term],
            limit_name=LIMIT_NAMES[term],
            limit_kind=LIMIT_KINDS[term],
        )
    return positions


# --------------------------------------------------------------------------------------
# Bounds
#
# The opposer's limit constrains one direction of each term. The contract constrains the
# other, and nothing in the settlement would otherwise look at it: the window and delay limits
# are floors while the contract's binding constraint on both is a ceiling.
# --------------------------------------------------------------------------------------


def baseline_fault(values: dict[str, Any]) -> str | None:
    """Name the first baseline field outside its domain, or `None` if they all fit.

    Returns rather than raises, so the writer can refuse with its own error type and the
    reader with its own, from one definition of what is allowed.
    """

    for field, (low, high) in sorted(BASELINE_BOUNDS.items()):
        value = values.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{field}={value!r} is not an integer"
        if not low <= value <= high:
            return f"{field}={value} is outside {low}..{high}"
    return None


def clamp_bond_bps(value: int) -> int:
    return max(0, min(MAX_PROVIDER_BOND_BPS, value))


def clamp_duration(value: int) -> int:
    return max(1, min(MAX_DURATION, value))


def price_wei(base_price_wei: int, bps: int) -> int:
    """One conversion from basis points to wei, after every comparison has been made.

    Comparing in wei instead would let two distinct basis-point values collide under floor
    division whenever the baseline is small: at a baseline of 3 wei, a ceiling of 9 250 bps
    and an ask of 11 800 both floor to the same number and the negotiation becomes a coin
    toss.
    """

    product = base_price_wei * bps
    if product > _UINT256_MAX:
        raise ValueError(
            f"a baseline of {base_price_wei} wei at {bps} bps is {product} wei, which does not "
            f"fit the uint256 the contract stores it in. `BASELINE_BOUNDS` refuses baselines "
            "this large at the door; reaching here means something bypassed it."
        )
    return max(MIN_SETTLED_PRICE_WEI, product // BPS_DENOMINATOR)


def bond_is_collectible(price: int, bond_bps: int) -> bool:
    """A non-zero bond rate must not round to a zero-wei bond, which the contract rejects.

    Price settles down and bond settles up independently, so this is a joint property of the
    settlement rather than something either term has on its own.
    """

    return bond_bps == 0 or price * bond_bps >= BPS_DENOMINATOR


__all__ = [
    "BASELINE_BOUNDS",
    "LIMIT_KINDS",
    "LIMIT_NAMES",
    "MAX_PRICE_BPS",
    "MIN_SETTLED_PRICE_WEI",
    "WALKAWAY_RULES",
    "build_positions",
    "derived_walkaway",
    "walkaways_for",
    "CEILING",
    "CONCESSION",
    "FLOOR",
    "MAX_BOND_BPS",
    "MEMORY",
    "Move",
    "NoOverlap",
    "Position",
    "RULE",
    "Settlement",
    "TERMS",
    "baseline_fault",
    "bond_is_collectible",
    "clamp_bond_bps",
    "clamp_duration",
    "price_wei",
    "settle",
]

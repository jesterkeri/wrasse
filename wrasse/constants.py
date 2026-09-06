"""Every number that can move a term, in one place, with a digest over all of them.

This module exists so that a claim can be true rather than aspirational.

`engine_version` is part of the policy preimage and is hashed into `policyHash`, which makes
it tempting to say that the constants deciding a term are covered by the same commitment as
the term. With a hand-typed version string that is false: editing a constant and leaving the
string alone changes every quote and no hash. It is release discipline, not proof.

So the string is derived. Every constant and every table that *parameterises* the production or
settlement of a term lives in `NEGOTIATION_MANIFEST`, the manifest is canonicalised and hashed,
and the digest is part of `ENGINE_VERSION`. Change any of them and the version changes, the
commitment changes, and every document written before the change is refused by `load_policy`.
The manifest is published in the document too, so a reader recomputes the digest instead of
trusting it.

**What it does not cover, stated rather than left to be discovered.** The settlement
*procedure* is code: the comparison operators in `_settle_one`, the inclusive boundary, the
gap arithmetic, the floor division in `price_wei`. No digest here fingerprints any of it.
Hashing the module source would fix that in the narrowest sense and make a comment edit
invalidate every document ever written, which is a worse trade than the problem.

The procedure is checked a different way. Every document publishes all four numbers for each
term, the proposal, the opposing limit and the walk-away the comparison is stated over, so a
reader recomputes the settlement by hand without running this build at all. A procedure that
drifted would disagree with that arithmetic visibly. The claim is therefore: **the parameters
are committed, and the procedure is reproducible from the receipt.** Not "the whole rule is
hashed", which was written here once and was not true.

Nothing here imports anything from this package. That is deliberate: `policy_hash` needs the
digest, `engine` needs the numbers, and a cycle between them would be resolved by duplicating
a constant, which is the failure this module prevents.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: The most bond a provider will ever post, before its memory of this buyer reduces it. The
#: concession span is this same number, so the ceiling reaches zero and can never go negative.
MAX_BOND_BPS = 5_000

#: How much of its own risk premium a provider will concede: three quarters. It gives back most
#: of what memory added and not all of it, because the reason it added it has not gone away.
#:
#: Two integers rather than a float, so the arithmetic stays exact. This is a stated policy and
#: not a derivation, and it is the number that decides where the refusal boundary falls between
#: profiles. Say so rather than letting a reader find it.
CONCESSION_NUM = 3
CONCESSION_DEN = 4

#: A window this side may not tighten past, because a deadline the provider cannot physically
#: meet is not a harder bargain, it is a broken deal. Base Sepolia's safe head trails the tip by
#: roughly 66 seconds, and a provider has to clear its acceptance before it can deliver.
#:
#: It is a floor on the *adjustment*, never an override of a base the operator chose. A demo
#: that deliberately asks for 60 seconds because it wants a timeout still gets 60 seconds, and
#: the provider's published floor is `min(this, the operator's base)` for the same reason.
MIN_SERVICE_WINDOW_SECONDS = 300

#: Same shape, for the provider's side of the bargain.
MIN_PAYOUT_DELAY_SECONDS = 60

#: The three task profiles, as data rather than as objects, so they can be hashed.
#:
#: `window_buffer_seconds` is signed, and the sign is the whole point. Prior non-delivery does
#: not universally imply a tighter deadline: an urgent buyer wants one, while a cost- or
#: quality-sensitive buyer may rationally grant a longer realistic window instead.
#:
#: `price_ceiling_premium_bps` is what this profile will pay over the baseline rate, full stop,
#: with no risk term. Willingness to pay is a fact about the job, not about the counterparty. A
#: ceiling that fell as the provider misbehaved made the buyer pay less as it was wronged more
#: and then refused outright, which punished the party that suffered rather than the party that
#: caused it.
#:
#: `payout_delay_floor_bps` is a fraction of the operator's baseline delay rather than absolute
#: seconds, so it scales with the runbook. Being under 10 000 it is always reachable by a
#: provider conceding back to its own baseline, so payout delay can never be the term that
#: refuses.
PROFILE_FIELDS: dict[str, dict[str, Any]] = {
    "urgent": {
        "contexts": ["deadline_sensitive"],
        "risk_weight": "1.40",
        "bond_sensitivity_bps": 3_000,
        "window_buffer_seconds": -1_800,
        "price_ceiling_premium_bps": 2_000,
        "payout_delay_floor_bps": 5_000,
    },
    "budget": {
        "contexts": ["cost_sensitive"],
        "risk_weight": "0.85",
        "bond_sensitivity_bps": 1_000,
        "window_buffer_seconds": 3_600,
        "price_ceiling_premium_bps": 500,
        "payout_delay_floor_bps": 6_500,
    },
    "sensitive": {
        "contexts": ["quality_sensitive"],
        "risk_weight": "1.20",
        "bond_sensitivity_bps": 2_500,
        "window_buffer_seconds": 1_800,
        "price_ceiling_premium_bps": 1_500,
        "payout_delay_floor_bps": 8_000,
    },
}

#: How much a dimension counts to a buyer whose profile shares one of its contexts, and how
#: much when it does not. Literals in the scoring function until it turned out that editing
#: one changed every buyer's terms without changing the version that claims to cover them.
RELEVANT_MULTIPLIER = "1.5"
IRRELEVANT_MULTIPLIER = "0.5"

#: Risk is clamped to this interval before it is spent on anything.
RISK_FLOOR = "0"
RISK_CEILING = "1"

#: The provider has no task profile, so every dimension weighs the same to it. That "same" is
#: a number, and changing it changes the provider's risk and therefore its price, its payout
#: delay, its bond ceiling and its price floor. It was a literal in two places until a review
#: pointed out that the version claiming to cover the arithmetic did not cover this.
PROVIDER_RISK_WEIGHT = "1"

#: The mode every rounding in the engine uses, and the quantum each rounds to. `ROUND_HALF_UP`
#: against Python's default banker's rounding is a whole unit at exactly `.5`.
#:
#: These are *consumed* by `engine._round_decimal`, not merely described here. A manifest entry
#: nothing reads is a decoration: editing it would change the version without changing a term,
#: and editing the real mode would change terms without changing the version. Python's decimal
#: rounding modes are plain strings, so the value that is hashed is the value that rounds.
ROUNDING = "ROUND_HALF_UP"
INTEGER_QUANTUM = "1"

#: What a displayed risk is rounded to. Not a term, but it is compared by the provenance
#: rebuild and read by a person, so a build that changed it would disagree with this one about
#: a document while claiming the same engine version.
RISK_DISPLAY_QUANTUM = "0.0001"

#: Mirrored from `policy_hash`, which mirrors the deployed contract. Duplicated into the
#: manifest rather than imported because this module has no intra-package imports by design,
#: and `test_the_manifest_mirrors_the_contract_bounds` pins the two together.
BPS_DENOMINATOR = 10_000
MAX_PROVIDER_BOND_BPS = 10_000
MAX_DURATION = 30 * 24 * 60 * 60

#: How a movement is explained. The three are disjoint and a reader has to be able to tell them
#: apart, because only one of them is evidence. They are hashed because `LIMIT_KINDS` below
#: decides which one a move is labelled with, and relabelling an evidence-driven concession as a
#: constant is the exact dishonesty the three-way split exists to prevent.
MEMORY = "memory"
CONCESSION = "concession"
RULE = "rule"

#: What each term's opposing limit is called in a refusal and in a move's `because`, and whether
#: that limit moves with its publisher's memory or is a constant.
#:
#: Both were literals repeated in the writer and again in the reader, which is the same defect
#: the shape table had: two copies, neither hashed, free to drift from each other and from the
#: version claiming to describe them. Flipping one entry of `LIMIT_KINDS` relabels a concession
#: as a rule in every document this build writes.
LIMIT_NAMES: dict[str, str] = {
    "provider_bond_bps": "provider_max_bond_bps",
    "service_window": "provider_min_service_window",
    "price_bps": "buyer_max_price_bps",
    "payout_delay": "buyer_min_payout_delay",
}

LIMIT_KINDS: dict[str, str] = {
    "provider_bond_bps": MEMORY,
    "service_window": RULE,
    "price_bps": RULE,
    "payout_delay": RULE,
}

#: How far each proposer will concede, as a named rule rather than as arithmetic buried in two
#: places. A side concedes back toward what it would have asked of a stranger, and never past a
#: number it just offered itself: hence the baseline *widened* to include the proposal. That
#: second clause is what makes the service window work, because `budget` and `sensitive` propose
#: a longer window than the baseline and a walk-away pinned to the baseline alone would be
#: violated by their own opening.
#:
#: Price is the exception, and it is the exception that makes a refusal possible at all. The
#: provider concedes only three quarters of the way back, so its floor sits above the baseline
#: and can rise past a buyer's ceiling. With every walk-away at the baseline and every ceiling
#: above it, no term could ever refuse.
BASELINE_WIDENED_DOWN = "min(baseline, proposal)"
BASELINE_WIDENED_UP = "max(baseline, proposal)"
PUBLISHED = "published"

WALKAWAY_RULES: dict[str, str] = {
    "provider_bond_bps": BASELINE_WIDENED_DOWN,
    "service_window": BASELINE_WIDENED_UP,
    "price_bps": PUBLISHED,
    "payout_delay": BASELINE_WIDENED_UP,
}

#: Whose conduct a receipt is evidence about, which the contract decides and no model is asked
#: to guess. A timeout is a provider failing to deliver. A release withheld until the payout
#: delay expired is a buyer making the provider wait.
#:
#: This is consumed directly on the pricing path: it decides which receipts reach which side's
#: score, and therefore that side's proposals and its limits. Flipping the subject of an outcome
#: changes every term this build produces, so it belongs to the digest that claims to cover
#: them. `delivered_and_released_by_buyer` is evidence about both: a prompt release is good
#: conduct by the buyer and it is also proof the provider delivered. Attributing it to one side
#: would leave the closing beat unbuildable, because the only positive outcome could never
#: soften a buyer's view of a provider.
SUBJECTS_OF: dict[str, frozenset[str]] = {
    "timeout_claimed_without_delivery": frozenset({"provider"}),
    "delivered_and_released_by_buyer": frozenset({"buyer", "provider"}),
    "delivered_and_claimed_after_delay": frozenset({"buyer"}),
}

#: Whether an outcome is a reason to demand safer terms or to offer easier ones. Derived from
#: the contract, never chosen by a model, and it gates which stored dimensions are admissible:
#: a dimension whose direction disagrees with its outcome is refused before it can reach a
#: score. That makes it a constraint on the engine's inputs, like the baseline domain below.
#:
#: A live call proved why it is not the model's call. Asked what a timeout meant, the model
#: answered `positive` with a severity of zero, so a provider that took payment and never
#: delivered would have made itself cheaper.
VALENCE_OF: dict[str, str] = {
    "timeout_claimed_without_delivery": "negative",
    "delivered_and_released_by_buyer": "positive",
    "delivered_and_claimed_after_delay": "negative",
}

#: The widest any basis-point figure in a document may be. The validator bounded every one of
#: them against a bare `2**32` and the number appeared nowhere else, so it read as a sanity
#: check rather than as what it is: the factor the baseline price is multiplied by, and
#: therefore the thing that decides how large a baseline can be before the product leaves
#: uint256.
MAX_PRICE_BPS = 2**32

#: A settled price of zero is a deal the chain rejects, so the conversion floors at this.
MIN_SETTLED_PRICE_WEI = 1

#: The four terms, in the order they are settled, and which way each opposer's limit points.
#:
#: These decide a settled term as completely as any number here does, and they were not hashed.
#: Flipping `price_bps` from a ceiling to a floor inverts every price outcome in the build while
#: leaving `ENGINE_VERSION` untouched, which is precisely the claim this module exists to make
#: true. A review found them sitting in `negotiation.py` as a private dict.
#:
#: The order is hashed for the same reason. When two terms both have no overlap, the order
#: decides which one the document names in `failed_on`, so editing it changes what a refusal
#: says about the very same pair of memories.
CEILING = "ceiling"
FLOOR = "floor"

TERMS = ("provider_bond_bps", "service_window", "price_bps", "payout_delay")

TERM_SHAPES: dict[str, str] = {
    "provider_bond_bps": CEILING,
    "price_bps": CEILING,
    "service_window": FLOOR,
    "payout_delay": FLOOR,
}

#: What an operator baseline may be, decided **once** and used by the writer and the reader.
#:
#: It was decided twice. The engines required a positive price, window and delay and said
#: nothing about the bond, because a zero bond rate is valid on the deployed contract. The
#: validator independently required every baseline field to be at least one. So
#: `--base-bond-bps 0` produced a document that this same build then refused to read: a
#: successful quote and an unusable receipt, from one command to the next.
#:
#: Hashed, because widening it admits baselines that settle differently. Dropping the price
#: floor to zero would let a settlement land on `price_wei`'s own clamp rather than on the
#: number either side published.
BASELINE_BOUNDS: dict[str, tuple[int, int]] = {
    # Not `2**256 - 1`. A baseline is multiplied by a basis-point figure before anything is
    # signed, so the honest upper bound is the largest baseline whose product still fits the
    # word the contract stores it in. At the old bound a baseline `baseline_fault` explicitly
    # accepted killed `wrasse policy` with an ABI encoding error from inside the hashing, which
    # is a crash where the design promises a refusal.
    "price_wei": (MIN_SETTLED_PRICE_WEI, (2**256 - 1) // MAX_PRICE_BPS),
    "provider_bond_bps": (0, MAX_PROVIDER_BOND_BPS),
    "service_window": (1, MAX_DURATION),
    "payout_delay": (1, MAX_DURATION),
}

#: Everything above, in one object, in the order a reader can reproduce.
#:
#: The test of whether something belongs here is not "is it a negotiation constant". It is:
#: **can editing this change a term?** A constant that can and is missing makes the claim on
#: `ENGINE_VERSION` false, which is worse than not making the claim.
NEGOTIATION_MANIFEST: dict[str, Any] = {
    "max_bond_bps": MAX_BOND_BPS,
    "concession_num": CONCESSION_NUM,
    "concession_den": CONCESSION_DEN,
    "min_service_window_seconds": MIN_SERVICE_WINDOW_SECONDS,
    "min_payout_delay_seconds": MIN_PAYOUT_DELAY_SECONDS,
    "relevant_multiplier": RELEVANT_MULTIPLIER,
    "irrelevant_multiplier": IRRELEVANT_MULTIPLIER,
    "risk_floor": RISK_FLOOR,
    "risk_ceiling": RISK_CEILING,
    "provider_risk_weight": PROVIDER_RISK_WEIGHT,
    "rounding": ROUNDING,
    "integer_quantum": INTEGER_QUANTUM,
    "risk_display_quantum": RISK_DISPLAY_QUANTUM,
    "bps_denominator": BPS_DENOMINATOR,
    "max_provider_bond_bps": MAX_PROVIDER_BOND_BPS,
    "max_duration_seconds": MAX_DURATION,
    "profiles": PROFILE_FIELDS,
    "settlement_order": list(TERMS),
    "term_shapes": TERM_SHAPES,
    "baseline_bounds": {field: list(bounds) for field, bounds in BASELINE_BOUNDS.items()},
    "limit_names": LIMIT_NAMES,
    "limit_kinds": LIMIT_KINDS,
    "walkaway_rules": WALKAWAY_RULES,
    # Sorted lists rather than the sets themselves, because a set has no JSON spelling. The
    # lists are derived from the objects the engine reads, so editing one moves the digest.
    "evidence_subjects": {
        event_type: sorted(subjects) for event_type, subjects in SUBJECTS_OF.items()
    },
    "evidence_valence": VALENCE_OF,
    "max_price_bps": MAX_PRICE_BPS,
    "min_settled_price_wei": MIN_SETTLED_PRICE_WEI,
}


def canonical_manifest() -> str:
    """The exact bytes the digest is taken over, so a reader can reproduce it."""

    return json.dumps(NEGOTIATION_MANIFEST, sort_keys=True, separators=(",", ":"))


MANIFEST_DIGEST = hashlib.sha256(canonical_manifest().encode("utf-8")).hexdigest()

#: Twelve characters is enough to make an accidental edit change the version, which is what
#: this is for. It is not a security boundary: an attacker who can edit these constants can
#: edit this line too, and the trust model puts local write access out of scope.
ENGINE_VERSION = f"wrasse/0.2.0+{MANIFEST_DIGEST[:12]}"

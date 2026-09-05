"""Every number that can move a term, in one place, with a digest over all of them.

This module exists so that a claim can be true rather than aspirational.

`engine_version` is part of the policy preimage and is hashed into `policyHash`, which makes
it tempting to say that the constants deciding a term are covered by the same commitment as
the term. With a hand-typed version string that is false: editing a constant and leaving the
string alone changes every quote and no hash. It is release discipline, not proof.

So the string is derived. Every constant that participates in producing or settling a term
lives in `NEGOTIATION_MANIFEST`, the manifest is canonicalised and hashed, and the digest is
part of `ENGINE_VERSION`. Change any of these numbers and the version changes, the commitment
changes, and every document written before the change is refused by `load_policy`. The
manifest is published in the document too, so a reader recomputes the digest instead of
trusting it.

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
}


def canonical_manifest() -> str:
    """The exact bytes the digest is taken over, so a reader can reproduce it."""

    return json.dumps(NEGOTIATION_MANIFEST, sort_keys=True, separators=(",", ":"))


MANIFEST_DIGEST = hashlib.sha256(canonical_manifest().encode("utf-8")).hexdigest()

#: Twelve characters is enough to make an accidental edit change the version, which is what
#: this is for. It is not a security boundary: an attacker who can edit these constants can
#: edit this line too, and the trust model puts local write access out of scope.
ENGINE_VERSION = f"wrasse/0.2.0+{MANIFEST_DIGEST[:12]}"

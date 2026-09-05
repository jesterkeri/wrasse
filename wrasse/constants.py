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

#: Everything above, in one object, in the order a reader can reproduce.
NEGOTIATION_MANIFEST: dict[str, Any] = {
    "max_bond_bps": MAX_BOND_BPS,
    "concession_num": CONCESSION_NUM,
    "concession_den": CONCESSION_DEN,
    "min_service_window_seconds": MIN_SERVICE_WINDOW_SECONDS,
    "min_payout_delay_seconds": MIN_PAYOUT_DELAY_SECONDS,
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

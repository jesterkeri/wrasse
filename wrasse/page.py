"""The page's view of a quote, projected from the document rather than computed again.

The page needs a flatter shape than the document has. It wants one `memories` object with a
receipt list per side, and one `profiles` array, where the document nests profiles under the
buyer and keeps risk per profile. That is a reasonable thing for a renderer to want and a
dangerous thing to satisfy carelessly, because a projection is exactly where a displayed term
can quietly stop being the produced one.

**So two rules hold here and are tested.**

Every number is *copied*. Nothing in this module recomputes a term, a limit, a walk-away, a
gap or a risk. Where a value is derived it is derived from other published values by selection,
never by arithmetic, and `test_every_number_the_page_shows_is_in_the_document` walks the whole
projection and finds each one in the document it came from.

The document ships with it. The response carries this view *and* the document it came from, so
a reader who distrusts the projection can check it against the thing it projects without
making a second request that might answer differently.

Two values genuinely have no single home in the document and are chosen here. Risk and the
used-evidence set are per profile, and the page shows one of each per side. Both are handled by
selection and both say so below.
"""

from __future__ import annotations

from typing import Any

from .constants import LIMIT_KINDS, LIMIT_NAMES, PROFILE_FIELDS, SUBJECTS_OF, TERM_SHAPES, CEILING

#: The order the page lays profiles out in. Urgent first because it is the headline, budget
#: last because it is the refusal and the strongest thing to end on.
DISPLAY_ORDER = ("urgent", "sensitive", "budget")


def _receipts(side: dict[str, Any]) -> list[dict[str, Any]]:
    """Each recalled receipt, plus whose conduct it is evidence about.

    `subject` is the one field added, and it is a lookup in the hashed routing table rather
    than a judgement. An outcome that is evidence about both parties says so, because
    collapsing it to one would be the same mistake the routing table exists to prevent.
    """

    projected = []
    for event in side["recalled_evidence"]:
        subjects = sorted(SUBJECTS_OF.get(event["event_type"], frozenset()))
        projected.append({**event, "subject": " and ".join(subjects) or "unattributed"})
    return projected


def _one_or_range(values: list[str]) -> str:
    """One value when every profile agrees, otherwise the span, and never an average.

    Risk is per profile in the document and the page shows one per side. On the live data all
    three buyer profiles clamp to 1.0000, so they agree and this returns that. When they do not
    agree, showing a span is honest where showing the first would be a silent choice and
    showing a mean would be a number the engine never produced.
    """

    distinct = sorted(set(values))
    if len(distinct) == 1:
        return distinct[0]
    return f"{distinct[0]}–{distinct[-1]}"


def _stood(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """The terms that did not move, and the limit that admitted each one.

    A term whose proposal stood emits no move, which is correct and leaves the page with
    nothing to show for three quarters of a settlement. The value is the proposal, unchanged,
    and the reason is which side of the opposing limit it sat on. Both numbers are published;
    only the preposition is chosen here.
    """

    moved = {move["term"] for move in profile["settlement"]["moves"]}
    proposals = {**profile["buyer"]["proposes"], **profile["provider"]["proposes"]}
    limits = {
        "provider_bond_bps": profile["provider"]["limits"]["max_bond_bps"],
        "service_window": profile["provider"]["limits"]["min_service_window"],
        "price_bps": profile["buyer"]["limits"]["max_price_bps"],
        "payout_delay": profile["buyer"]["limits"]["min_payout_delay"],
    }

    rows = []
    for term, proposal in proposals.items():
        if term in moved:
            continue
        limit = limits[term]
        if proposal == limit:
            word = "equals"
        elif TERM_SHAPES[term] == CEILING:
            word = "inside"
        else:
            word = "above"
        rows.append({
            "term": term,
            "value": proposal,
            "because": f"{word} {LIMIT_NAMES[term]}={limit}",
        })
    return rows


def page_view(document: dict[str, Any], *, memory: bool) -> dict[str, Any]:
    """The whole projection. Selection only; no term is computed here."""

    profiles = document["buyer"]["profiles"]
    ordered = [name for name in DISPLAY_ORDER if name in profiles]

    sides = {}
    for side in ("buyer", "provider"):
        sides[side] = {
            "address": document[side]["address"],
            "verdict": document[side]["verdict"],
            "cold_start": document[side]["cold_start"],
            "risk": _one_or_range([profiles[n][side]["risk"] for n in ordered]),
            # The union across profiles, so a receipt that moved any profile's numbers is
            # marked as having moved something. Taking one profile's set would mark a receipt
            # as unused because a different profile happened not to need it.
            "used_evidence_ids": sorted({
                identifier
                for name in ordered
                for identifier in profiles[name][side]["used_evidence_ids"]
            }),
            "receipts": _receipts(document[side]),
        }

    rendered = []
    for name in ordered:
        profile = profiles[name]
        settlement = profile["settlement"]
        row = {
            "id": name,
            "label": name,
            "context": PROFILE_FIELDS[name]["contexts"][0],
            "agreed": settlement["agreed"],
        }
        if settlement["agreed"]:
            row.update({
                "terms": profile["terms"],
                "policy_hash": profile["policy_hash"],
                "moves": settlement["moves"],
                "stood": _stood(profile),
            })
        else:
            row.update({
                "failed_on": settlement["failed_on"],
                "gap": settlement["gap"],
                "provider_floor_bps": profile["provider"]["walkaways"]["price_bps"],
                "buyer_ceiling_bps": profile["buyer"]["limits"]["max_price_bps"],
            })
        rendered.append(row)

    return {
        "engine_version": document["engine_version"],
        "chain_id": document["chain_id"],
        "contract_address": document["contract_address"],
        "memory": memory,
        "baseline": profiles[ordered[0]]["baseline"],
        "limit_kinds": dict(LIMIT_KINDS),
        "limit_names": dict(LIMIT_NAMES),
        "persona": {
            "name": document["provider"]["persona"]["name"],
            "cashflow_sensitivity": document["provider"]["persona"]["cashflow_sensitivity"],
        },
        "memories": sides,
        "profiles": rendered,
        # Shipped so the projection above can be checked rather than trusted, in the same
        # response, because a second request could answer differently.
        "document": document,
    }

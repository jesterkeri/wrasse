"""The page's projection, checked against the two documents the repository actually contains.

Deliberately store-free. These assertions used to live in the service tests and were made
against `.wrasse/`, which is gitignored, so they passed on one laptop and failed everywhere
else. CI caught it. A test that depends on untracked state proves nothing to anyone cloning
the repository, which is every judge.

The tracked documents are the right subject anyway. They are pinned by digest, regenerated
when a constant moves, and they are exactly what the page renders.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wrasse.page import page_view

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "examples"
WARM = json.loads((EXAMPLES / "policy.schema3.json").read_text())
COLD = json.loads((EXAMPLES / "policy.cold.json").read_text())


def test_the_page_sees_the_receipts_the_document_holds():
    """The bug this projection exists to fix.

    The page reads `memories.<side>.receipts`; the document calls them `recalled_evidence` and
    nests them under each side. So it rendered "0 receipts / risk -- / verdict --" beside a
    live quote that had just read two out of both memories. A system that appears to remember
    nothing is the one thing this entry cannot show a judge.
    """

    view = page_view(WARM, memory=True)

    assert len(view["memories"]["buyer"]["receipts"]) == 2
    assert len(view["memories"]["provider"]["receipts"]) == 2
    assert view["memories"]["buyer"]["risk"] == "1.0000"
    assert view["memories"]["provider"]["risk"] == "0.7200"
    assert view["memories"]["buyer"]["verdict"] == "match"

    cold = page_view(COLD, memory=False)
    assert cold["memories"]["buyer"]["receipts"] == []
    assert cold["memories"]["buyer"]["risk"] == "0.0000"
    assert cold["memories"]["buyer"]["cold_start"] is True


@pytest.mark.parametrize("document,memory", [(WARM, True), (COLD, False)])
def test_every_number_the_page_shows_is_in_the_document(document, memory):
    """A projection is where a displayed term quietly stops being the produced one.

    The rule in `wrasse/page.py` is selection only: no term, limit, walk-away, gap or risk is
    recomputed. This walks the whole projection and finds each number in the place it claims to
    come from, so a projection that starts doing arithmetic fails here rather than at judging.
    """

    view = page_view(document, memory=memory)
    profiles = document["buyer"]["profiles"]

    assert view["engine_version"] == document["engine_version"]
    assert view["chain_id"] == document["chain_id"]
    assert view["contract_address"] == document["contract_address"]
    assert view["baseline"] == profiles["urgent"]["baseline"]
    assert view["persona"] == {
        "name": document["provider"]["persona"]["name"],
        "cashflow_sensitivity": document["provider"]["persona"]["cashflow_sensitivity"],
    }
    assert view["document"] is document, "the document rides along so the view can be checked"

    for side in ("buyer", "provider"):
        shown = view["memories"][side]
        assert shown["address"] == document[side]["address"]
        assert shown["verdict"] == document[side]["verdict"]
        assert shown["cold_start"] == document[side]["cold_start"]

        assert len(shown["receipts"]) == len(document[side]["recalled_evidence"])
        for projected, original in zip(shown["receipts"], document[side]["recalled_evidence"]):
            assert {k: v for k, v in projected.items() if k != "subject"} == original
            assert projected["subject"] in ("buyer", "provider", "buyer and provider")

        # one value per side, chosen from what the document contains, never averaged
        assert shown["risk"] in {profiles[n][side]["risk"] for n in profiles}
        assert set(shown["used_evidence_ids"]) == {
            i for n in profiles for i in profiles[n][side]["used_evidence_ids"]
        }

    for row in view["profiles"]:
        profile = profiles[row["id"]]
        settlement = profile["settlement"]
        assert row["agreed"] == settlement["agreed"]
        if row["agreed"]:
            assert row["terms"] == profile["terms"]
            assert row["policy_hash"] == profile["policy_hash"]
            assert row["moves"] == settlement["moves"]
            proposals = {**profile["buyer"]["proposes"], **profile["provider"]["proposes"]}
            moved = {m["term"] for m in settlement["moves"]}
            assert {s["term"] for s in row["stood"]} == set(proposals) - moved
            for stood in row["stood"]:
                assert stood["value"] == proposals[stood["term"]], "a term that stood is unchanged"
        else:
            assert row["failed_on"] == settlement["failed_on"]
            assert row["gap"] == settlement["gap"]
            assert row["provider_floor_bps"] == profile["provider"]["walkaways"]["price_bps"]
            assert row["buyer_ceiling_bps"] == profile["buyer"]["limits"]["max_price_bps"]


def test_the_refusal_survives_the_projection():
    """The best beat on the page, and the one a flattening layer would lose.

    A refused profile omits terms and a policy hash structurally, so a projection that filled
    them in with placeholders would turn "there is no deal" into "here is a deal", which is the
    opposite of what the settlement said.
    """

    budget = [p for p in page_view(WARM, memory=True)["profiles"] if p["id"] == "budget"][0]

    assert budget["agreed"] is False
    assert "terms" not in budget and "policy_hash" not in budget
    assert (budget["failed_on"], budget["gap"]) == ("price_bps", 850)
    assert budget["provider_floor_bps"] > budget["buyer_ceiling_bps"], (
        "and the reason is legible: the floor rose past the ceiling"
    )

    cold_budget = [p for p in page_view(COLD, memory=False)["profiles"] if p["id"] == "budget"][0]
    assert cold_budget["agreed"] is True, "the same profile agrees when nobody remembers anything"


def test_the_profiles_are_ordered_for_reading():
    """Urgent leads because it is the headline; budget closes because it is the refusal."""

    assert [p["id"] for p in page_view(WARM, memory=True)["profiles"]] == [
        "urgent", "sensitive", "budget",
    ]

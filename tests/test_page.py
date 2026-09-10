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


# The bundle is generated, and every hand change to it lives in `web/source-edits.json` so a
# fresh export from the design tool is one command away from correct. That only holds while a
# rebuild is a no-op. Twice in one day an edit was written whose replacement rewrote text that
# an EARLIER edit produces: the first run applied both, and the second could find neither the
# earlier edit's `old` (long gone from the bundle) nor its `new` (just overwritten), so
# `apply-overrides.py` refused with ANCHOR LOST and the page could not be rebuilt at all.
#
# Both times it was found by running the script twice by hand. Nothing in the suite covered
# it, which is the whole reason it happened twice.

BUNDLE = Path(__file__).resolve().parent.parent / "web" / "index.html"
EDITS = Path(__file__).resolve().parent.parent / "web" / "source-edits.json"
_OPEN = '  <script type="__bundler/template">\n'


def _document() -> str:
    """The application document the bundle carries, without writing the gitignored unpack."""

    text = BUNDLE.read_text(encoding="utf-8")
    start = text.index(_OPEN) + len(_OPEN)
    return json.loads(text[start : text.index("\n  </script>", start)])


@pytest.mark.parametrize("edit", json.loads(EDITS.read_text(encoding="utf-8")), ids=lambda e: e["name"])
def test_every_source_edit_is_still_present_in_the_bundle(edit):
    """Each edit's replacement survives every edit that runs after it.

    This is the exact condition `apply-overrides.py` tests to decide an edit is already
    applied. An edit whose `new` is absent from the committed bundle cannot be skipped on the
    next run, and its `old` is no longer there either, so the rebuild fails closed.
    """

    assert edit["new"] in _document(), (
        f"{edit['name']!r} is not present in web/index.html. A later edit has rewritten the "
        "text this one produces, so the next rebuild will refuse. Fold the wording into this "
        "edit rather than adding a second one that overwrites it."
    )


@pytest.mark.parametrize("edit", json.loads(EDITS.read_text(encoding="utf-8")), ids=lambda e: e["name"])
def test_no_source_edit_anchors_inside_an_injected_panel(edit):
    """The panels are re-injected from their own files on every run, so an edit there is lost.

    `apply-overrides.py` strips each `wrasse:<name>` region and re-appends it from
    `web/run-panel.html` or `web/sim-panel.html`. An edit whose anchor lies inside one of those
    regions would apply, be thrown away moments later in the same run, and read as applied in
    the diff. Panel text is changed in the panel file itself.
    """

    document = _document()
    for marker in ("prove-panel", "run-panel", "sim-panel"):
        start = document.find(f"<!-- wrasse:{marker}:start -->")
        end = document.find(f"<!-- wrasse:{marker}:end -->")
        if start == -1 or end == -1:
            continue
        region = document[start:end]
        assert edit["new"] not in region, (
            f"{edit['name']!r} anchors inside the {marker} region, which is regenerated from "
            f"web/{marker}.html on every run. Change that file instead."
        )

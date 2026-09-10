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

    document = _document()
    found = document.count(edit["new"])

    assert found, (
        f"{edit['name']!r} is not present in web/index.html. A later edit has rewritten the "
        "text this one produces, so the next rebuild will refuse. Fold the wording into this "
        "edit rather than adding a second one that overwrites it."
    )
    # Exactly once, not merely present. Changing the replacement of an edit that INSERTS a
    # block makes the old block unfindable, so the next rebuild inserts a second copy on top of
    # the first and reports success. The page then carried two "How to read this page" panels,
    # one of them stating the thing that had just been corrected, and the presence check was
    # happy because the new text was there. Editing an applied insertion means changing the
    # bundle in place, not changing the edit and re-running.
    assert found == 1, (
        f"{edit['name']!r} appears {found} times in web/index.html. An insertion has been "
        "applied on top of its own earlier output."
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


def test_the_page_does_not_name_an_edge_that_is_twice_as_far_as_the_real_one():
    """The stake guidance told a visitor the wrong place to stop.

    It said no seller stakes more than half the price, so asking for more than 50% is what
    breaks a deal. Measured against the live engine, the buyer's memory is ADDED to whatever is
    asked for, so with the two receipts this deployment holds the cliff is at 25%: every
    posting refuses at 2500 basis points and settles at 2400. A visitor following the page
    walked confidently past the edge and got raw engine output.

    This pins the shape of the claim rather than the number, because the number moves with the
    history. What must never come back is a sentence naming 50% as the point where a deal stops
    being possible, with no mention that memory adds to what you ask.
    """

    document = _document()

    assert "No seller will ever stake more than 50% of the price, whatever" not in document, (
        "the old stake guidance is back: it names 50% as the edge and does not say that what "
        "the buyer remembers is added to whatever the visitor asks for"
    )
    assert "no seller will ever stake more than half the price, so ask for more" not in document

    # And the mechanism has to be stated wherever the limit is, or the number is a magic one.
    for phrase in (
        "what the buyer remembers is added on top of it",
        "what the buyer remembers is added on top",
    ):
        if phrase in document:
            break
    else:
        raise AssertionError("no surface explains that memory is added to the stake you ask for")


def test_a_refusal_reads_as_a_result_rather_than_as_engine_output():
    """"urgent refused on provider_bond_bps by 320" is right for a log and useless to a person.

    It names a field nothing on screen uses, a number in basis points, and no way forward. The
    raw line stays on the step, which is the trace; the sentence underneath has to say what
    happened and what to do about it.
    """

    document = _document()

    assert "No deal, and it is the stake." in document, (
        "the settlement panel no longer translates a refusal into something a visitor can act on"
    )
    assert "Nothing was sent and nothing was spent." in document, (
        "a refusal has to say that it cost nothing, or it reads as a failed payment"
    )
    # And it has to be reachable from the branch a refusal actually takes. The explanation
    # first shipped only on the failed-run path, so a normal refusal printed a generic sentence
    # that never named the term to change: the advice existed and the visitor never saw it.
    assert "refusalText((run.settled || {}).failed_on" in document, (
        "the refused branch writes its own sentence again instead of the shared explanation"
    )


def test_the_simulator_names_no_stake_threshold_it_cannot_know():
    """A number that is right on one tab and wrong on the other is worse than no number.

    The simulator prices the history the visitor invents, not the two receipts this deployment
    holds. With an empty history a 25% stake settles perfectly well, so a fixed "at or above 25
    percent everything refuses" warning is an on-screen assertion about a run that has not
    happened. That is the same mistake as the 50% figure it replaced, one tab over.
    """

    document = _document()
    simulator = document[document.find("<!-- wrasse:sim-panel:start -->"):]

    assert "At or above 25 percent" not in simulator, (
        "the simulator names a threshold computed from a history it does not price"
    )
    assert "depends entirely on the history YOU build" in simulator, (
        "the simulator has to say that the limit follows the history the visitor builds"
    )

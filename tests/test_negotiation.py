"""The settlement rule, alone, against numbers written by hand.

This is where the design risk of the whole gate sits, and it needs no memory to test. If the
rule is wrong here it is wrong everywhere, and no amount of correct plumbing will save it.

The properties that matter are not "it computes a number". They are: a settled term is one
both sides said they would take; a refusal happens exactly when no such value exists; and the
account the document gives of *why* a number moved is the true one rather than the flattering
one.
"""

from __future__ import annotations

import pytest

from wrasse.negotiation import (
    CONCESSION,
    MEMORY,
    RULE,
    Position,
    bond_is_collectible,
    clamp_bond_bps,
    clamp_duration,
    price_wei,
    settle,
)
from wrasse.policy_hash import MAX_DURATION, MAX_PROVIDER_BOND_BPS


def _positions(**overrides) -> dict[str, Position]:
    """Four positions that agree comfortably, so a test can break exactly one of them."""

    base = {
        # ceilings: the proposal is below the limit, so it stands
        "provider_bond_bps": Position(3_500, 4_000, 500, "provider_max_bond_bps", MEMORY),
        "price_bps": Position(11_800, 12_000, 11_350, "buyer_max_price_bps", RULE),
        # floors: the proposal is above the limit, so it stands
        "service_window": Position(600, 300, 600, "provider_min_window", RULE),
        "payout_delay": Position(1_200, 900, 1_800, "buyer_min_payout_delay", RULE),
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------------------
# The proposal stands, or the proposer concedes exactly to the limit
# --------------------------------------------------------------------------------------


def test_a_proposal_the_limit_admits_stands_untouched():
    result = settle(_positions())
    assert result.agreed
    assert result.terms == {
        "provider_bond_bps": 3_500,
        "price_bps": 11_800,
        "service_window": 600,
        "payout_delay": 1_200,
    }
    assert result.moves == (), "nothing moved, so nothing may be reported as having moved"


@pytest.mark.parametrize(
    "term,position,expected",
    [
        # a ceiling below the proposal drags it down
        ("provider_bond_bps", Position(3_500, 2_480, 500, "provider_max_bond_bps", MEMORY), 2_480),
        ("price_bps", Position(11_800, 11_500, 11_350, "buyer_max_price_bps", RULE), 11_500),
        # a floor above the proposal pushes it up
        ("service_window", Position(300, 516, 600, "provider_min_window", RULE), 516),
        ("payout_delay", Position(893, 1_200, 1_800, "buyer_min_payout_delay", RULE), 1_200),
    ],
)
def test_a_proposal_the_limit_refuses_concedes_exactly_to_it(term, position, expected):
    result = settle(_positions(**{term: position}))
    assert result.agreed
    assert result.terms[term] == expected
    (move,) = [m for m in result.moves if m.term == term]
    assert (move.from_value, move.to_value) == (position.proposal, expected)


def test_a_limit_equal_to_the_proposal_is_an_agreement_not_a_refusal():
    """A boundary one unit wide, chosen deliberately rather than inherited from an operator."""

    exact = Position(3_500, 3_500, 500, "provider_max_bond_bps", MEMORY)
    result = settle(_positions(provider_bond_bps=exact))
    assert result.agreed
    assert result.terms["provider_bond_bps"] == 3_500
    assert result.moves == (), "settling at a limit that already admitted the proposal is not a move"


# --------------------------------------------------------------------------------------
# Attribution: three causes, and they are not interchangeable
# --------------------------------------------------------------------------------------


def test_a_concession_to_a_moving_limit_cites_the_limit_not_a_receipt():
    """The receipt explains the limit. It does not explain the concession.

    A reader who wants the evidence follows the limit back to the side that published it. If
    a concession cited receipts directly, the document would claim the counterparty's history
    moved this side's number, which is one step too far and unprovable from here.
    """

    result = settle(_positions(
        provider_bond_bps=Position(3_500, 2_480, 500, "provider_max_bond_bps", MEMORY)
    ))
    (move,) = [m for m in result.moves if m.term == "provider_bond_bps"]
    assert move.kind == CONCESSION
    assert move.because == "provider_max_bond_bps=2480"
    assert "0x" not in move.because, "a concession must never cite an event id"


def test_a_fixed_limit_binding_cites_the_rule_and_never_evidence():
    """The case that made 'a receipt behind every number that moved' a false claim.

    A constant provider window floor can lift a 300 second proposal to 516 with no receipt
    involved anywhere. Reporting that as evidence-driven would let a constant masquerade as
    memory.
    """

    result = settle(_positions(
        service_window=Position(300, 516, 600, "provider_min_window", RULE)
    ))
    (move,) = [m for m in result.moves if m.term == "service_window"]
    assert move.kind == RULE
    assert move.because == "provider_min_window=516"


# --------------------------------------------------------------------------------------
# Refusal
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "term,position,gap",
    [
        # a ceiling below what the proposer will accept
        ("price_bps", Position(11_800, 10_500, 11_350, "buyer_max_price_bps", RULE), 850),
        ("provider_bond_bps", Position(3_500, 400, 500, "provider_max_bond_bps", MEMORY), 100),
        # a floor above what the proposer will accept
        ("payout_delay", Position(893, 2_400, 1_800, "buyer_min_payout_delay", RULE), 600),
        ("service_window", Position(300, 900, 600, "provider_min_window", RULE), 300),
    ],
)
def test_no_overlap_refuses_and_names_the_term_and_the_distance(term, position, gap):
    result = settle(_positions(**{term: position}))
    assert not result.agreed
    assert result.failed_on == term
    assert result.gap == gap


def test_a_refusal_carries_no_settled_terms_at_all():
    """Structurally unusable, not a flag on an otherwise complete answer."""

    result = settle(_positions(
        price_bps=Position(11_800, 10_500, 11_350, "buyer_max_price_bps", RULE)
    ))
    assert result.terms == {}
    assert result.moves == ()
    assert result.as_dict() == {"agreed": False, "failed_on": "price_bps", "gap": 850}


def test_a_refusal_on_one_term_does_not_report_another_terms_outcome():
    """Three terms agree here. None of their values may leak into the refusal."""

    result = settle(_positions(
        payout_delay=Position(893, 2_400, 1_800, "buyer_min_payout_delay", RULE)
    ))
    assert not result.agreed
    assert "provider_bond_bps" not in result.terms


def test_the_refused_term_is_the_same_one_every_time():
    """Two terms with no overlap. A fixed order makes the answer reproducible."""

    positions = _positions(
        provider_bond_bps=Position(3_500, 400, 500, "provider_max_bond_bps", MEMORY),
        price_bps=Position(11_800, 10_500, 11_350, "buyer_max_price_bps", RULE),
    )
    assert {settle(positions).failed_on for _ in range(5)} == {"provider_bond_bps"}


# --------------------------------------------------------------------------------------
# The shape table, which is published and therefore has to be the one that decides
# --------------------------------------------------------------------------------------


def test_the_shape_the_manifest_publishes_is_the_shape_that_settles(monkeypatch):
    """A manifest entry nothing reads is a decoration. A rule nothing hashes is a hole.

    The table saying which way each limit points lived here as a private dict, outside the
    digest that `ENGINE_VERSION` claims covers every constant deciding a term. It holds no
    numbers, which is why it survived three rounds of looking for numbers.

    Flip one entry and the outcome inverts: the ceiling that drags an ask down to the buyer's
    limit becomes a floor the ask already clears, and the buyer pays the full 11 800 it had
    published a refusal to pay. Before the fix that flip changed no version, no
    `engineVersionHash`, and no `policyHash`.
    """

    from wrasse import constants

    ceiling_below_the_ask = Position(11_800, 11_500, 11_350, "buyer_max_price_bps", RULE)
    assert settle(_positions(price_bps=ceiling_below_the_ask)).terms["price_bps"] == 11_500

    monkeypatch.setitem(constants.TERM_SHAPES, "price_bps", constants.FLOOR)
    assert settle(_positions(price_bps=ceiling_below_the_ask)).terms["price_bps"] == 11_800


def test_the_settlement_order_the_manifest_publishes_is_the_order_that_refuses(monkeypatch):
    """Two terms with no overlap, and the published order decides which one is named."""

    from wrasse import constants, negotiation

    assert negotiation.TERMS is constants.TERMS, (
        "one order, hashed and read. A second private copy is how the shape table got out of "
        "the manifest in the first place."
    )
    assert negotiation.TERM_SHAPES is constants.TERM_SHAPES
    assert negotiation.BASELINE_BOUNDS is constants.BASELINE_BOUNDS

    positions = _positions(
        provider_bond_bps=Position(3_500, 400, 500, "provider_max_bond_bps", MEMORY),
        price_bps=Position(11_800, 10_500, 11_350, "buyer_max_price_bps", RULE),
    )
    assert settle(positions).failed_on == "provider_bond_bps"

    monkeypatch.setattr(
        "wrasse.negotiation.TERMS", tuple(reversed(constants.TERMS)), raising=True
    )
    assert settle(positions).failed_on == "price_bps"


def test_the_baseline_domain_the_manifest_publishes_is_the_one_that_refuses(monkeypatch):
    """The published bounds are read, not described alongside a second private copy."""

    from wrasse import constants
    from wrasse.negotiation import baseline_fault

    values = {
        "price_wei": 10**14,
        "provider_bond_bps": 500,
        "service_window": 600,
        "payout_delay": 1_800,
    }
    assert baseline_fault({**values, "service_window": 0}) is not None

    monkeypatch.setitem(constants.BASELINE_BOUNDS, "service_window", (0, MAX_DURATION))
    assert baseline_fault({**values, "service_window": 0}) is None


# --------------------------------------------------------------------------------------
# Purity and bounds
# --------------------------------------------------------------------------------------


def test_settlement_is_a_pure_function_of_the_published_numbers():
    """A reader holding only the document must reach the same answer, without either memory."""

    positions = _positions(
        provider_bond_bps=Position(3_500, 2_480, 500, "provider_max_bond_bps", MEMORY),
        price_bps=Position(11_800, 11_500, 11_350, "buyer_max_price_bps", RULE),
    )
    first = settle(positions)
    assert all(settle(positions).as_dict() == first.as_dict() for _ in range(5))


def test_a_position_that_was_never_published_is_refused_rather_than_defaulted():
    positions = _positions()
    del positions["price_bps"]
    with pytest.raises(ValueError, match="no position published for price_bps"):
        settle(positions)


@pytest.mark.parametrize("value,expected", [(-1, 0), (0, 0), (5_000, 5_000), (10_001, 10_000)])
def test_a_bond_limit_is_clamped_into_what_the_contract_accepts(value, expected):
    assert clamp_bond_bps(value) == expected
    assert 0 <= clamp_bond_bps(value) <= MAX_PROVIDER_BOND_BPS


@pytest.mark.parametrize("value", [-5, 0, 1, MAX_DURATION, MAX_DURATION + 1])
def test_a_duration_limit_is_clamped_into_what_the_contract_accepts(value):
    assert 1 <= clamp_duration(value) <= MAX_DURATION


def test_price_is_converted_once_and_never_reaches_zero():
    """Comparing in wei would let distinct basis points collide at a small baseline."""

    assert price_wei(10**14, 11_800) == 118 * 10**12
    assert price_wei(1, 9_250) == 1, "a floored price of zero is a deal the chain rejects"
    assert price_wei(1, 1) >= 1

    # The collision the comparison order exists to avoid: at a baseline of one wei a ceiling
    # of 9 250 bps and an ask of 11 800 land on the same number, so a settlement decided in
    # wei would be a coin toss. Decided in basis points, they are two clearly different
    # positions and the ceiling binds.
    assert price_wei(1, 9_250) == price_wei(1, 11_800)
    assert 9_250 < 11_800


def test_a_baseline_too_large_to_multiply_refuses_rather_than_crashing():
    """`BASELINE_BOUNDS` accepted a price the conversion could not survive.

    At the old ceiling of `2**256 - 1`, a baseline the validator explicitly allowed produced a
    settled price above uint256, and the first thing to notice was an ABI encoding error thrown
    from inside the hashing. A domain that admits an input the next step cannot process is not
    a domain, and a crash is not a refusal.
    """

    from wrasse.constants import BASELINE_BOUNDS, MAX_PRICE_BPS
    from wrasse.negotiation import baseline_fault

    largest = BASELINE_BOUNDS["price_wei"][1]
    assert baseline_fault({**GOOD_BASELINE, "price_wei": largest}) is None
    assert price_wei(largest, MAX_PRICE_BPS) > 0, "the whole admitted domain converts"

    assert baseline_fault({**GOOD_BASELINE, "price_wei": largest + 1}) is not None
    with pytest.raises(ValueError, match="does not fit the uint256"):
        price_wei(largest + 1, MAX_PRICE_BPS)


GOOD_BASELINE = {
    "price_wei": 10**14,
    "provider_bond_bps": 500,
    "service_window": 600,
    "payout_delay": 1_800,
}


def test_a_nonzero_bond_rate_that_would_round_to_nothing_is_caught():
    """Price settles down and bond settles up independently, so this is a joint condition."""

    assert bond_is_collectible(10**14, 3_500)
    assert bond_is_collectible(1, 0), "a zero rate owes no bond and is not a rounding failure"
    assert not bond_is_collectible(1, 9_999)


# --------------------------------------------------------------------------------------
# Walk-aways: one rule, applied by the writer and checked by the reader
# --------------------------------------------------------------------------------------


BASE = {"provider_bond_bps": 500, "service_window": 600, "payout_delay": 1_800}


def _numbers():
    """The proposals and limits of the comfortable four, keyed by term."""

    positions = _positions()
    return (
        {term: p.proposal for term, p in positions.items()},
        {term: p.limit for term, p in positions.items()},
    )


@pytest.mark.parametrize(
    "term,proposal,expected",
    [
        # a bond proposal above the baseline concedes back down to the baseline
        ("provider_bond_bps", 3_500, 500),
        # and one below it concedes no further than its own opening
        ("provider_bond_bps", 200, 200),
        # a window proposal above the baseline holds its own opening
        ("service_window", 2_400, 2_400),
        # and one below it concedes back up to the baseline
        ("service_window", 300, 600),
        ("payout_delay", 893, 1_800),
        ("payout_delay", 2_400, 2_400),
    ],
)
def test_a_derived_walkaway_is_the_baseline_widened_by_the_proposal(term, proposal, expected):
    """Concede back to what a stranger would have been asked, and never past your own offer.

    The second clause is what makes the service window work. `budget` and `sensitive` propose a
    longer window than the baseline, so a walk-away pinned to the baseline alone would be
    violated by their own opening.
    """

    from wrasse.negotiation import derived_walkaway

    assert derived_walkaway(term, BASE, proposal) == expected


def test_the_price_walkaway_has_no_derivation_and_must_be_published():
    """The exception that makes a refusal possible at all.

    The provider concedes three quarters of the way back, not all of it, so its floor sits
    above the baseline and can rise past a buyer's ceiling. That distance is a function of its
    memory, not of the baseline, so no rule over the baseline can produce it.
    """

    from wrasse.negotiation import derived_walkaway

    assert derived_walkaway("price_bps", BASE, 11_800) is None

    with pytest.raises(ValueError, match="no derivation rule and none was published"):
        from wrasse.negotiation import walkaways_for

        walkaways_for(BASE, _numbers()[0], {})


def test_a_document_may_not_publish_a_walkaway_its_own_rule_does_not_give():
    """The reason publishing and deriving are both done rather than one or the other.

    A reader who does not know Wrasse's rules gets the number. A reader who does gets a proof
    that the number is the one the rules give. Without the check, a document could publish a
    walk-away chosen to make a refusal look inevitable, and every other field would still
    reconcile.
    """

    from wrasse.negotiation import build_positions

    proposals, limits = _numbers()
    honest = {"provider_bond_bps": 500, "service_window": 600, "price_bps": 11_350,
              "payout_delay": 1_800}
    build_positions(baseline=BASE, proposals=proposals, limits=limits, walkaways=honest)

    with pytest.raises(ValueError, match="publishes a walk-away of 3400"):
        build_positions(
            baseline=BASE,
            proposals=proposals,
            limits=limits,
            walkaways={**honest, "provider_bond_bps": 3_400},
        )


def test_the_published_price_walkaway_is_taken_as_given():
    """It has no rule, so there is nothing to check it against and it is used as published."""

    from wrasse.negotiation import build_positions

    proposals, limits = _numbers()
    built = build_positions(
        baseline=BASE,
        proposals=proposals,
        limits=limits,
        walkaways={"provider_bond_bps": 500, "service_window": 600, "price_bps": 9_999,
                   "payout_delay": 1_800},
    )
    assert built["price_bps"].walkaway == 9_999


def test_the_limit_names_and_kinds_come_from_the_hashed_tables(monkeypatch):
    """A move's `kind` is the difference between evidence and a constant.

    Both tables were literals repeated in the writer and again in the reader, hashed in
    neither. Relabelling the bond ceiling would tell every reader that an evidence-driven
    concession was a rule.
    """

    from wrasse import constants
    from wrasse.negotiation import build_positions

    proposals, limits = _numbers()
    walkaways = {"provider_bond_bps": 500, "service_window": 600, "price_bps": 11_350,
                 "payout_delay": 1_800}
    built = build_positions(
        baseline=BASE, proposals=proposals, limits=limits, walkaways=walkaways
    )
    assert built["provider_bond_bps"].limit_kind == MEMORY
    assert built["provider_bond_bps"].limit_name == "provider_max_bond_bps"

    monkeypatch.setitem(constants.LIMIT_KINDS, "provider_bond_bps", constants.RULE)
    monkeypatch.setitem(constants.LIMIT_NAMES, "provider_bond_bps", "renamed")
    relabelled = build_positions(
        baseline=BASE, proposals=proposals, limits=limits, walkaways=walkaways
    )
    assert relabelled["provider_bond_bps"].limit_kind == RULE
    assert relabelled["provider_bond_bps"].limit_name == "renamed"


# --------------------------------------------------------------------------------------
# The baseline domain, decided once
# --------------------------------------------------------------------------------------


def test_a_zero_bond_baseline_is_allowed_because_the_contract_allows_it():
    """The writer and the reader disagreed about this, and the writer was right.

    A zero bond rate is valid on the deployed contract, the engines never rejected it, and the
    validator independently demanded at least one. So `--base-bond-bps 0` produced a document
    that the same build then refused to read: a successful quote and an unusable receipt, one
    command apart.
    """

    from wrasse.negotiation import baseline_fault

    good = {"price_wei": 10**14, "provider_bond_bps": 0, "service_window": 600, "payout_delay": 1_800}
    assert baseline_fault(good) is None


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("price_wei", 0, "outside"),
        ("provider_bond_bps", -1, "outside"),
        ("provider_bond_bps", 10_001, "outside"),
        ("service_window", 0, "outside"),
        ("payout_delay", MAX_DURATION + 1, "outside"),
        ("service_window", True, "not an integer"),
        ("payout_delay", "1800", "not an integer"),
    ],
)
def test_a_baseline_outside_the_domain_is_named(field, value, reason):
    from wrasse.negotiation import baseline_fault

    values = {"price_wei": 10**14, "provider_bond_bps": 500, "service_window": 600, "payout_delay": 1_800}
    values[field] = value
    fault = baseline_fault(values)
    assert fault is not None and field in fault and reason in fault

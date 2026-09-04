"""Every rule `WrasseEscrow.createDeal` enforces, checked before a quote is shown.

A commitment the chain would refuse reads as an executable quote and is not one, and the
failure only surfaces after both sides have already agreed terms.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from wrasse.policy_hash import (
    BPS_DENOMINATOR,
    EMPTY_EVIDENCE_HASH,
    MAX_DURATION,
    MAX_PROVIDER_BOND_BPS,
    PolicyNotCreatable,
    PolicyPreimage,
    validate_creatable,
)

BUYER = "0x4444444444444444444444444444444444444444"
PROVIDER = "0x3333333333333333333333333333333333333333"
REFERENCE = 1_700_000_000

CONTRACT = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "src" / "WrasseEscrow.sol"


def _preimage(**overrides) -> PolicyPreimage:
    base = dict(
        buyer=BUYER,
        provider=PROVIDER,
        price=10**18,
        bond_bps=2_000,
        accept_by=REFERENCE + 3_600,
        service_window=7_200,
        payout_delay=1_800,
        engine_version="wrasse/0.1.0",
        buyer_evidence_hash=EMPTY_EVIDENCE_HASH,
        provider_evidence_hash=EMPTY_EVIDENCE_HASH,
    )
    base.update(overrides)
    return PolicyPreimage(**base)


def _check(**overrides) -> None:
    validate_creatable(_preimage(**overrides), reference_timestamp=REFERENCE)


def test_the_bounds_match_the_deployed_contract():
    """Duplicated constants drift silently. Read the Solidity and fail loudly if they do."""
    source = CONTRACT.read_text(encoding="utf-8")
    assert re.search(r"MAX_DURATION\s*=\s*30 days;", source)
    assert MAX_DURATION == 30 * 24 * 60 * 60
    assert re.search(rf"BPS_DENOMINATOR\s*=\s*{BPS_DENOMINATOR:_};", source)
    assert re.search(rf"MAX_PROVIDER_BOND_BPS\s*=\s*{MAX_PROVIDER_BOND_BPS:_};", source)


def test_a_normal_policy_is_creatable():
    _check()


@pytest.mark.parametrize(
    "overrides",
    [
        {"accept_by": REFERENCE + MAX_DURATION},
        {"service_window": MAX_DURATION},
        {"payout_delay": MAX_DURATION},
        {"bond_bps": MAX_PROVIDER_BOND_BPS},
        {"bond_bps": 0},
        {"price": 1, "bond_bps": 0},
    ],
)
def test_the_bounds_themselves_are_accepted(overrides):
    """A bound that rejects its own limit is an off-by-one, not a bound."""
    _check(**overrides)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"price": 0}, "price must be greater than zero"),
        ({"bond_bps": MAX_PROVIDER_BOND_BPS + 1}, "basis point ceiling"),
        ({"accept_by": REFERENCE}, "not in the future"),
        ({"accept_by": REFERENCE - 1}, "not in the future"),
        ({"accept_by": REFERENCE + MAX_DURATION + 1}, "seconds away"),
        ({"service_window": 0}, "service_window must be greater than zero"),
        ({"service_window": MAX_DURATION + 1}, "service_window exceeds"),
        ({"payout_delay": 0}, "payout_delay must be greater than zero"),
        ({"payout_delay": MAX_DURATION + 1}, "payout_delay exceeds"),
        ({"price": 1, "bond_bps": 1}, "rounds to zero wei"),
        ({"price": -1}, "outside the range"),
        ({"accept_by": 2**64}, "outside the range"),
    ],
)
def test_terms_the_contract_would_refuse_are_rejected(overrides, reason):
    with pytest.raises(PolicyNotCreatable, match=reason):
        _check(**overrides)


def test_the_zero_bond_rule_tracks_the_price():
    """The rounding boundary moves with the price, so it is not a fixed minimum."""
    smallest_bonded_price = BPS_DENOMINATOR
    _check(price=smallest_bonded_price, bond_bps=1)
    with pytest.raises(PolicyNotCreatable, match="rounds to zero wei"):
        _check(price=smallest_bonded_price - 1, bond_bps=1)


def test_identity_rules_are_enforced_before_anything_else():
    with pytest.raises(ValueError, match="must differ"):
        _check(buyer=PROVIDER)
    with pytest.raises(ValueError, match="zero address"):
        _check(provider="0x" + "00" * 20)
    with pytest.raises(ValueError, match="not an EVM address"):
        _check(buyer="not-an-address")


def test_the_verdict_does_not_depend_on_the_clock():
    """The same preimage must be judged the same way on every run."""
    preimage = _preimage(accept_by=REFERENCE + 10)
    validate_creatable(preimage, reference_timestamp=REFERENCE)
    with pytest.raises(PolicyNotCreatable, match="not in the future"):
        validate_creatable(preimage, reference_timestamp=REFERENCE + 10)

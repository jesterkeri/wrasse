from __future__ import annotations

import pytest

from wrasse.policy_hash import (
    EMPTY_EVIDENCE_HASH,
    PolicyPreimage,
    evidence_hash,
    policy_hash,
)

BUYER = "0x4444444444444444444444444444444444444444"
PROVIDER = "0x3333333333333333333333333333333333333333"

# Shared with contracts/test/WrasseEscrow.t.sol. Solidity recomputes these from its own
# abi.encode, so agreement is proven rather than copied.
CANONICAL_BUYER_EVIDENCE = "0x2f685994ab703309ca4d0393ec2524b0368f819050ff85e7e3fb719cc5b48de3"
CANONICAL_EMPTY_EVIDENCE = "0x569e75fc77c1a856f6daaf9e69d8a9566ca34aa47f9133711ce065a571af0cfd"
CANONICAL_POLICY_HASH = "0x2feccea0356143b90ef1b559f53a0cfa28c8c52f3e47d046a5d5f68909317d2b"


def _canonical(**overrides) -> PolicyPreimage:
    base = dict(
        buyer=BUYER,
        provider=PROVIDER,
        price=10**18,
        bond_bps=2_000,
        accept_by=1_700_000_000,
        service_window=7_200,
        payout_delay=1_800,
        engine_version="wrasse/0.1.0",
        buyer_evidence_hash=evidence_hash(["0x" + "11" * 32, "0x" + "22" * 32]),
        provider_evidence_hash=EMPTY_EVIDENCE_HASH,
    )
    base.update(overrides)
    return PolicyPreimage(**base)


def test_evidence_hash_is_order_independent():
    first = "0x" + "11" * 32
    second = "0x" + "22" * 32
    assert evidence_hash([first, second]) == evidence_hash([second, first])


def test_empty_evidence_set_has_a_canonical_hash():
    """Every first deal of a relationship commits an empty set on at least one side."""
    assert evidence_hash([]) == EMPTY_EVIDENCE_HASH
    assert EMPTY_EVIDENCE_HASH == CANONICAL_EMPTY_EVIDENCE


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", 999),
        ("bond_bps", 2_001),
        ("accept_by", 1_700_000_001),
        ("service_window", 7_201),
        ("payout_delay", 1_801),
        ("engine_version", "wrasse/0.2.0"),
        ("buyer", "0x5555555555555555555555555555555555555555"),
        ("provider", "0x6666666666666666666666666666666666666666"),
    ],
)
def test_every_committed_field_changes_the_hash(field, value):
    assert policy_hash(_canonical()) != policy_hash(_canonical(**{field: value}))


def test_swapping_the_two_evidence_sides_changes_the_hash():
    """The two sides are not interchangeable.

    buyer_evidence_hash commits to what the BUYER recalled about the PROVIDER; the other
    commits to the reverse. If swapping them produced the same commitment, two parameters
    would carry no more meaning than one.
    """
    original = _canonical()
    swapped = _canonical(
        buyer_evidence_hash=original.provider_evidence_hash,
        provider_evidence_hash=original.buyer_evidence_hash,
    )
    assert policy_hash(original) != policy_hash(swapped)


def test_policy_hash_matches_solidity_fixture():
    preimage = _canonical()
    assert preimage.buyer_evidence_hash == CANONICAL_BUYER_EVIDENCE
    assert preimage.provider_evidence_hash == CANONICAL_EMPTY_EVIDENCE
    assert policy_hash(preimage) == CANONICAL_POLICY_HASH


def test_buyer_and_provider_must_differ():
    with pytest.raises(ValueError, match="must differ"):
        policy_hash(_canonical(buyer=PROVIDER))


def test_non_address_is_rejected():
    with pytest.raises(ValueError, match="not an EVM address"):
        policy_hash(_canonical(buyer="not-an-address"))

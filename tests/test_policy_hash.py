from __future__ import annotations

from wrasse.policy_hash import PolicyPreimage, evidence_hash, policy_hash


def test_evidence_hash_is_order_independent():
    first = "0x" + "11" * 32
    second = "0x" + "22" * 32
    assert evidence_hash([first, second]) == evidence_hash([second, first])


def test_policy_hash_changes_with_committed_terms():
    evidence = evidence_hash(["0x" + "11" * 32])
    original = PolicyPreimage(
        provider="0x3333333333333333333333333333333333333333",
        price=1_000,
        bond_bps=2_000,
        service_window=3_600,
        payout_delay=1_800,
        engine_version="wrasse/0.1.0",
        evidence_hash=evidence,
    )
    changed = PolicyPreimage(**{**original.as_dict(), "price": 999})
    assert policy_hash(original) != policy_hash(changed)


def test_policy_hash_matches_solidity_fixture():
    evidence = evidence_hash(["0x" + "11" * 32, "0x" + "22" * 32])
    preimage = PolicyPreimage(
        provider="0x3333333333333333333333333333333333333333",
        price=10**18,
        bond_bps=2_000,
        service_window=7_200,
        payout_delay=1_800,
        engine_version="wrasse/0.1.0",
        evidence_hash=evidence,
    )
    assert evidence == "0x2f685994ab703309ca4d0393ec2524b0368f819050ff85e7e3fb719cc5b48de3"
    assert policy_hash(preimage) == "0x42d7fe689da394b89bd8f6bc4ffc06bcf6f4720ebfae05b132bbb756e2224358"

from __future__ import annotations

import json

import pytest

from wrasse.cli import main
from wrasse.policy_hash import EMPTY_EVIDENCE_HASH


BUYER = "0x4444444444444444444444444444444444444444"
PROVIDER = "0x3333333333333333333333333333333333333333"
ACCEPT_BY = 1_700_000_000
# The fixture deadline is fixed in the past so the commitment never moves. Executability is
# judged against a supplied reference time rather than the clock, so both stay deterministic.
REFERENCE = ACCEPT_BY - 3_600


def _run(tmp_path, monkeypatch, extra=()):
    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    output_path = tmp_path / "policy.json"
    argv = [
        "policy",
        PROVIDER,
        "--buyer",
        BUYER,
        "--accept-by",
        str(ACCEPT_BY),
        "--reference-timestamp",
        str(REFERENCE),
        "--output",
        str(output_path),
        *extra,
    ]
    return main(argv), output_path


def test_cold_start_policy_is_emitted_as_json(tmp_path, monkeypatch, capsys):
    code, output_path = _run(tmp_path, monkeypatch)
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text())
    assert output == written
    assert output["buyer"]["cold_start"] is True
    assert set(output["buyer"]["profiles"]) == {"urgent", "budget", "sensitive"}
    assert all(value["policy_hash"].startswith("0x") for value in output["buyer"]["profiles"].values())


def test_cold_start_commits_an_empty_provider_evidence_set(tmp_path, monkeypatch, capsys):
    """The provider has recalled nothing about this buyer until gate 6.

    That side must commit the canonical empty set rather than borrow the buyer's own
    evidence, which would make the two commitments indistinguishable.
    """
    _run(tmp_path, monkeypatch)
    output = json.loads(capsys.readouterr().out)
    for value in output["buyer"]["profiles"].values():
        preimage = value["policy_preimage"]
        assert preimage["provider_evidence_hash"] == EMPTY_EVIDENCE_HASH
        assert preimage["buyer"].lower() == BUYER.lower()
        assert preimage["accept_by"] == ACCEPT_BY


def test_acceptance_deadline_is_not_derived_from_the_clock(tmp_path, monkeypatch):
    """A commitment that moves on every run is not a commitment.

    The deadline must be supplied, so the same inputs always produce the same hash.
    """
    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    with pytest.raises(SystemExit):
        main(["policy", PROVIDER, "--buyer", BUYER])


def test_a_deadline_the_contract_would_refuse_is_not_quoted(tmp_path, monkeypatch):
    """A quote the chain rejects is worse than no quote: it looks executable and is not."""
    from wrasse.policy_hash import PolicyNotCreatable

    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    with pytest.raises(PolicyNotCreatable, match="not in the future"):
        main([
            "policy",
            PROVIDER,
            "--buyer",
            BUYER,
            "--accept-by",
            str(ACCEPT_BY),
            "--reference-timestamp",
            str(ACCEPT_BY + 1),
        ])


def test_same_inputs_produce_the_same_commitment(tmp_path, monkeypatch, capsys):
    _run(tmp_path, monkeypatch)
    first = json.loads(capsys.readouterr().out)
    _run(tmp_path, monkeypatch)
    second = json.loads(capsys.readouterr().out)
    for name, value in first["buyer"]["profiles"].items():
        assert value["policy_hash"] == second["buyer"]["profiles"][name]["policy_hash"]

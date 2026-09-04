"""The deadline is enforced by the block that mines the transaction, not by this machine."""

from __future__ import annotations

import json

import pytest

from wrasse.chain_time import (
    ChainObservation,
    ChainTimeUnavailable,
    observe_chain_time,
    require_recent,
)
from wrasse.cli import main

BUYER = "0x4444444444444444444444444444444444444444"
PROVIDER = "0x3333333333333333333333333333333333333333"
CHAIN_ID = 84532


class _Eth:
    def __init__(self, chain_id, timestamp, number=1_000, error=None, block=None):
        self.chain_id = chain_id
        self._timestamp = timestamp
        self._number = number
        self._error = error
        self._block = block

    def get_block(self, _which):
        if self._error is not None:
            raise self._error
        if self._block is not None:
            return self._block
        return {"number": self._number, "timestamp": self._timestamp}


class _Web3:
    def __init__(self, **kwargs):
        self.eth = _Eth(**kwargs)


def _stub_chain(monkeypatch, **kwargs):
    monkeypatch.setattr("wrasse.cli._web3", lambda: _Web3(**kwargs))


def test_a_chain_on_the_wrong_id_is_refused():
    with pytest.raises(ChainTimeUnavailable, match="expected 84532"):
        observe_chain_time(_Web3(chain_id=1, timestamp=1_000), expected_chain_id=CHAIN_ID)


def test_an_unreachable_node_refuses_rather_than_guessing():
    web3 = _Web3(chain_id=CHAIN_ID, timestamp=1_000, error=ConnectionError("no route"))
    with pytest.raises(ChainTimeUnavailable, match="could not read a usable latest block"):
        observe_chain_time(web3, expected_chain_id=CHAIN_ID)


def test_a_skewed_observation_can_only_ever_refuse():
    """Local time is a sanity check on the reading, never the authority behind it."""
    observation = ChainObservation(chain_id=CHAIN_ID, block_number=7, timestamp=1_000)
    require_recent(observation, local_now=1_010, max_skew_seconds=300)
    with pytest.raises(ChainTimeUnavailable, match="beyond the 300s bound"):
        require_recent(observation, local_now=9_999, max_skew_seconds=300)


def _policy_argv(*extra):
    return ["policy", PROVIDER, "--buyer", BUYER, *extra]


def test_a_supplied_reference_produces_a_fixture_and_says_so(tmp_path, monkeypatch, capsys):
    """A reproducible run is useful, but it is not evidence the chain would accept it."""
    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    assert main(_policy_argv("--accept-by", "1700003600", "--reference-timestamp", "1700000000")) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["executability"]["basis"] == "supplied-reference"
    assert output["executability"]["executable"] is False
    assert output["executability"]["chain"] is None


def test_a_live_quote_derives_the_deadline_from_the_observed_block(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    now = 1_760_000_000
    _stub_chain(monkeypatch, chain_id=CHAIN_ID, timestamp=now, number=4_242)
    monkeypatch.setattr("wrasse.cli.time.time", lambda: now)

    assert main(_policy_argv("--accept-window", "3600")) == 0
    output = json.loads(capsys.readouterr().out)
    executability = output["executability"]
    assert executability["basis"] == "chain-observation"
    assert executability["executable"] is True
    assert executability["chain"] == {"chain_id": CHAIN_ID, "block_number": 4_242, "block_timestamp": now}
    for value in output["buyer"]["profiles"].values():
        assert value["policy_preimage"]["accept_by"] == now + 3_600


def test_a_deadline_inside_the_inclusion_margin_is_refused(tmp_path, monkeypatch):
    """Still open by the contract's rule, but not open long enough to be worth signing."""
    from wrasse.policy_hash import PolicyNotCreatable

    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    now = 1_760_000_000
    _stub_chain(monkeypatch, chain_id=CHAIN_ID, timestamp=now)
    monkeypatch.setattr("wrasse.cli.time.time", lambda: now)

    with pytest.raises(PolicyNotCreatable, match="leaves at most 120s"):
        main(_policy_argv("--accept-window", "30"))


def test_a_stale_node_stops_a_live_quote(tmp_path, monkeypatch):
    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    _stub_chain(monkeypatch, chain_id=CHAIN_ID, timestamp=1_000)
    monkeypatch.setattr("wrasse.cli.time.time", lambda: 1_760_000_000)

    with pytest.raises(ChainTimeUnavailable, match="stale or skewed"):
        main(_policy_argv("--accept-window", "3600"))


@pytest.mark.parametrize(
    "extra,reason",
    [
        ((), "exactly one"),
        (("--accept-by", "1700003600", "--accept-window", "3600"), "exactly one"),
        (("--accept-window", "3600", "--reference-timestamp", "1700000000"), "needs chain time"),
    ],
)
def test_the_time_basis_must_be_unambiguous(tmp_path, monkeypatch, capsys, extra, reason):
    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    with pytest.raises(SystemExit):
        main(_policy_argv(*extra))
    assert reason in capsys.readouterr().err


@pytest.mark.parametrize(
    "block",
    [
        {"number": 1},
        {"number": 1, "timestamp": None},
        {"number": 1, "timestamp": "not-a-number"},
        {"number": 1, "timestamp": 0},
        {"number": 1, "timestamp": -5},
    ],
)
def test_a_malformed_block_surfaces_as_this_modules_own_error(block):
    """Fail closed is not enough; it has to fail closed through the declared boundary.

    A KeyError escaping from here would bypass the handling a caller writes for an
    unreliable node.
    """
    web3 = _Web3(chain_id=CHAIN_ID, timestamp=0, block=block)
    with pytest.raises(ChainTimeUnavailable):
        observe_chain_time(web3, expected_chain_id=CHAIN_ID)


def test_node_lag_is_spent_out_of_the_inclusion_margin():
    """A lagging node and an inclusion margin are the same distance from the real tip.

    Allowing the full skew bound alongside the full margin let an already expired deadline
    be labelled executable, because each check passed on its own.
    """
    from wrasse.policy_hash import PolicyNotCreatable, PolicyPreimage, require_inclusion_margin

    def _preimage(accept_by):
        return PolicyPreimage(
            buyer=BUYER,
            provider=PROVIDER,
            price=10**18,
            bond_bps=2_000,
            accept_by=accept_by,
            service_window=7_200,
            payout_delay=1_800,
            engine_version="wrasse/0.1.0",
            buyer_evidence_hash="0x" + "00" * 32,
            provider_evidence_hash="0x" + "00" * 32,
        )

    block_time = 1_000

    # No lag: the margin alone decides, and equality is rejected.
    require_inclusion_margin(
        _preimage(block_time + 121), chain_timestamp=block_time, observed_lag_seconds=0, margin_seconds=120
    )
    with pytest.raises(PolicyNotCreatable):
        require_inclusion_margin(
            _preimage(block_time + 120), chain_timestamp=block_time, observed_lag_seconds=0, margin_seconds=120
        )

    # The case the composition used to let through: the deadline is already in the past
    # relative to local time, yet cleared the margin against a stale block.
    with pytest.raises(PolicyNotCreatable, match="300s of node lag"):
        require_inclusion_margin(
            _preimage(block_time + 121), chain_timestamp=block_time, observed_lag_seconds=300, margin_seconds=120
        )

    # Lag is spent, not merely noticed: the deadline has to clear both.
    require_inclusion_margin(
        _preimage(block_time + 421), chain_timestamp=block_time, observed_lag_seconds=300, margin_seconds=120
    )
    with pytest.raises(PolicyNotCreatable):
        require_inclusion_margin(
            _preimage(block_time + 420), chain_timestamp=block_time, observed_lag_seconds=300, margin_seconds=120
        )
    with pytest.raises(PolicyNotCreatable):
        require_inclusion_margin(
            _preimage(block_time + 419), chain_timestamp=block_time, observed_lag_seconds=299, margin_seconds=120
        )


def test_a_lagging_node_cannot_produce_a_live_quote_for_a_short_window(tmp_path, monkeypatch):
    """End to end, through the CLI, at the boundary the two checks used to miss together."""
    from wrasse.policy_hash import PolicyNotCreatable

    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    block_time = 1_760_000_000
    _stub_chain(monkeypatch, chain_id=CHAIN_ID, timestamp=block_time)
    monkeypatch.setattr("wrasse.cli.time.time", lambda: block_time + 300)

    with pytest.raises(PolicyNotCreatable, match="300s of node lag"):
        main(_policy_argv("--accept-window", "121"))


def test_an_absolute_deadline_can_be_checked_against_the_live_chain(tmp_path, monkeypatch, capsys):
    """The third supported form: a deadline agreed during negotiation, verified live."""
    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    now = 1_760_000_000
    _stub_chain(monkeypatch, chain_id=CHAIN_ID, timestamp=now, number=99)
    monkeypatch.setattr("wrasse.cli.time.time", lambda: now)

    assert main(_policy_argv("--accept-by", str(now + 3_600))) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["executability"]["basis"] == "chain-observation"
    assert output["executability"]["executable"] is True
    assert output["executability"]["observed_lag_seconds"] == 0
    for value in output["buyer"]["profiles"].values():
        assert value["policy_preimage"]["accept_by"] == now + 3_600

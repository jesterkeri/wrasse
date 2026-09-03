from __future__ import annotations

import pytest

from rapport.providers import Provider, counteroffer, request_bid


def test_provider_bid_is_reproducible_from_logged_seed():
    provider = Provider("Atlas", "0x" + "11" * 20, 200, 750)
    first = request_bid(provider, reference_price_wei=10_000, service_window=3_600, seed=42)
    second = request_bid(provider, reference_price_wei=10_000, service_window=3_600, seed=42)
    assert first == second
    assert first.seed == 42


def test_exactly_one_counteroffer_is_allowed():
    provider = Provider("Atlas", "0x" + "11" * 20, 200, 750)
    bid = request_bid(provider, reference_price_wei=10_000, service_window=3_600, seed=42)
    accepted = counteroffer(bid, proposed_price_wei=9_000, proposed_bond_bps=1_250)
    assert accepted.round == 1
    assert accepted.price_wei == (bid.price_wei + 9_000) // 2
    with pytest.raises(ValueError, match="exactly one"):
        counteroffer(accepted, proposed_price_wei=9_000, proposed_bond_bps=1_250)


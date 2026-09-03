"""Three transparent, seeded provider simulations and one counteroffer round."""

from __future__ import annotations

import random
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Provider:
    name: str
    address: str
    price_bias_bps: int
    preferred_bond_bps: int


@dataclass(frozen=True)
class Bid:
    provider: Provider
    price_wei: int
    requested_bond_bps: int
    service_window: int
    seed: int
    round: int = 0


def request_bid(
    provider: Provider,
    *,
    reference_price_wei: int,
    service_window: int,
    seed: int,
) -> Bid:
    if reference_price_wei <= 0 or service_window <= 0:
        raise ValueError("price and service window must be positive")
    rng = random.Random(f"rapport:{seed}:{provider.address.lower()}")
    jitter_bps = rng.randint(-75, 75)
    price = reference_price_wei * (10_000 + provider.price_bias_bps + jitter_bps) // 10_000
    return Bid(provider, price, provider.preferred_bond_bps, service_window, seed)


def counteroffer(bid: Bid, *, proposed_price_wei: int, proposed_bond_bps: int) -> Bid:
    """One deterministic compromise; callers must not invoke a second round."""

    if bid.round != 0:
        raise ValueError("Rapport permits exactly one counteroffer")
    if proposed_price_wei <= 0 or not 0 <= proposed_bond_bps <= 10_000:
        raise ValueError("invalid counteroffer")
    price = (bid.price_wei + proposed_price_wei) // 2
    bond = (bid.requested_bond_bps + proposed_bond_bps) // 2
    return replace(bid, price_wei=price, requested_bond_bps=bond, round=1)

"""Three transparent, seeded provider simulations and one counteroffer round."""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from decimal import Decimal


@dataclass(frozen=True)
class ProviderPersona:
    """Who a provider is, fixed before it has anything to react to.

    `cashflow_sensitivity` is the one that carries meaning in the demo: a provider that needs
    its money sooner pushes harder on the payout delay when a buyer has made it wait before.
    The file this comes from is committed to the repository and its hash is recorded in the
    provider's store at creation, so the persona demonstrably predates the evidence rather
    than being asserted to.
    """

    name: str
    address: str
    cashflow_sensitivity: Decimal
    price_sensitivity_bps: int
    delay_sensitivity_seconds: int

    @classmethod
    def from_document(cls, document: dict) -> "ProviderPersona":
        allowed = {"name", "address", "cashflow_sensitivity", "price_sensitivity_bps",
                   "delay_sensitivity_seconds"}
        unknown = sorted(set(document) - allowed - {"_comment"})
        if unknown:
            raise ValueError(f"persona has unknown fields: {', '.join(unknown)}")
        missing = sorted(allowed - set(document))
        if missing:
            raise ValueError(f"persona is missing: {', '.join(missing)}")

        sensitivity = Decimal(str(document["cashflow_sensitivity"]))
        if not Decimal("0") <= sensitivity <= Decimal("1"):
            raise ValueError("cashflow_sensitivity must be between 0 and 1")
        for field in ("price_sensitivity_bps", "delay_sensitivity_seconds"):
            value = document[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        return cls(
            name=str(document["name"]),
            address=str(document["address"]),
            cashflow_sensitivity=sensitivity,
            price_sensitivity_bps=document["price_sensitivity_bps"],
            delay_sensitivity_seconds=document["delay_sensitivity_seconds"],
        )


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
    rng = random.Random(f"wrasse:{seed}:{provider.address.lower()}")
    jitter_bps = rng.randint(-75, 75)
    price = reference_price_wei * (10_000 + provider.price_bias_bps + jitter_bps) // 10_000
    return Bid(provider, price, provider.preferred_bond_bps, service_window, seed)


def counteroffer(bid: Bid, *, proposed_price_wei: int, proposed_bond_bps: int) -> Bid:
    """One deterministic compromise; callers must not invoke a second round."""

    if bid.round != 0:
        raise ValueError("Wrasse permits exactly one counteroffer")
    if proposed_price_wei <= 0 or not 0 <= proposed_bond_bps <= 10_000:
        raise ValueError("invalid counteroffer")
    price = (bid.price_wei + proposed_price_wei) // 2
    bond = (bid.requested_bond_bps + proposed_bond_bps) // 2
    return replace(bid, price_wei=price, requested_bond_bps=bond, round=1)

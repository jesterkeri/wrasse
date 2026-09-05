"""Who a provider is, committed before it has anything to react to.

This module once also held a seeded bidding simulation with a midpoint `counteroffer`. It was
reachable only from its own test file and it implemented a *different* rule from the one that
ships: the settlement in `negotiation.py` meets at the accepting side's limit, it does not
split the difference. A function advertising a negotiation the build does not perform is a
false claim sitting in the source, so it is gone.
"""

from __future__ import annotations

from dataclasses import dataclass
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

"""Cross-language canonical policy commitments.

The tuple encoded here must match `WrasseEscrow.computePolicyHash` exactly, in order.
Every field is a term the contract itself enforces: committing to anything less would make
the claim that the contract commits to the terms it enforces untrue.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from eth_abi import encode
from web3 import Web3


#: Mirrors the constants of the same name in `WrasseEscrow`. Duplicated rather than read
#: from a deployment because the terms have to be checkable before any contract exists;
#: `tests/test_policy_rules.py` pins them against the Solidity fixture.
BPS_DENOMINATOR = 10_000
MAX_PROVIDER_BOND_BPS = 10_000
MAX_DURATION = 30 * 24 * 60 * 60

_UINT64_MAX = 2**64 - 1
_UINT256_MAX = 2**256 - 1


class PolicyNotCreatable(ValueError):
    """The preimage is well formed, but `createDeal` would revert on it.

    A commitment the chain will refuse is worse than no commitment: it reads as an
    executable quote and is not one, and the failure only surfaces after both sides have
    agreed terms.
    """


#: ABI tuple shared with Solidity. All static types, so both languages reproduce it byte
#: for byte. Order is load-bearing.
POLICY_ABI_TYPES = [
    "address",  # buyer
    "address",  # provider
    "uint256",  # price
    "uint256",  # bond_bps
    "uint64",  # accept_by
    "uint64",  # service_window
    "uint64",  # payout_delay
    "bytes32",  # engine version hash
    "bytes32",  # buyer evidence hash
    "bytes32",  # provider evidence hash
]


@dataclass(frozen=True)
class PolicyPreimage:
    """The exact payload the onchain commitment covers.

    ``buyer_evidence_hash`` commits to the receipts the **buyer recalled about the
    provider**. ``provider_evidence_hash`` commits to the receipts the **provider recalled
    about the buyer**. They are not interchangeable, and swapping them must change the
    commitment.
    """

    buyer: str
    provider: str
    price: int
    bond_bps: int
    accept_by: int
    service_window: int
    payout_delay: int
    engine_version: str
    buyer_evidence_hash: str
    provider_evidence_hash: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _bytes32(value: str) -> bytes:
    raw = bytes.fromhex(value.removeprefix("0x"))
    if len(raw) != 32:
        raise ValueError("expected bytes32")
    return raw


def evidence_hash(event_ids: Iterable[str]) -> str:
    """Commit to a set of evidence ids, order-independently.

    An empty set is legitimate: on the first deal of a relationship one side has recalled
    nothing. Its hash must agree with Solidity's ``keccak256(abi.encode(new bytes32[](0)))``.

    This is a set commitment, so repeated identifiers collapse. Recalling the same receipt
    twice describes the same evidence and must not produce a different commitment.
    """

    ordered = sorted({_bytes32(item) for item in event_ids})
    return "0x" + bytes(Web3.keccak(encode(["bytes32[]"], [ordered]))).hex()


#: The commitment for "this side recalled nothing". Used whenever a counterparty is a
#: stranger, which is every first deal.
EMPTY_EVIDENCE_HASH = evidence_hash([])


def validate_creatable(preimage: PolicyPreimage, *, reference_timestamp: int) -> None:
    """Reject any preimage `WrasseEscrow.createDeal` would revert on.

    Mirrors every creation-time contract rule, in the order the contract applies them, so a
    quote is never displayed as executable unless the chain would in fact accept it.

    ``reference_timestamp`` is the time acceptance is measured against. It is supplied
    rather than read from the clock, so the same inputs always reach the same verdict.
    """

    _require_addresses(preimage)

    for label, value, ceiling in (
        ("price", preimage.price, _UINT256_MAX),
        ("bond_bps", preimage.bond_bps, _UINT256_MAX),
        ("accept_by", preimage.accept_by, _UINT64_MAX),
        ("service_window", preimage.service_window, _UINT64_MAX),
        ("payout_delay", preimage.payout_delay, _UINT64_MAX),
        ("reference_timestamp", reference_timestamp, _UINT64_MAX),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise PolicyNotCreatable(f"{label} must be an integer")
        if value < 0 or value > ceiling:
            raise PolicyNotCreatable(f"{label} is outside the range the contract can hold")

    # InvalidTerms
    if preimage.price == 0:
        raise PolicyNotCreatable("price must be greater than zero; the contract rejects a zero deposit")
    if preimage.bond_bps > MAX_PROVIDER_BOND_BPS:
        raise PolicyNotCreatable(f"bond_bps exceeds the {MAX_PROVIDER_BOND_BPS} basis point ceiling")

    # DurationOutOfRange
    if preimage.accept_by <= reference_timestamp:
        raise PolicyNotCreatable("accept_by is not in the future; acceptance would already be closed")
    if preimage.accept_by - reference_timestamp > MAX_DURATION:
        raise PolicyNotCreatable(f"accept_by is more than {MAX_DURATION} seconds away")
    for label, duration in (
        ("service_window", preimage.service_window),
        ("payout_delay", preimage.payout_delay),
    ):
        if duration == 0:
            raise PolicyNotCreatable(f"{label} must be greater than zero; a zero window is unresolvable")
        if duration > MAX_DURATION:
            raise PolicyNotCreatable(f"{label} exceeds the {MAX_DURATION} second bound")

    # ZeroBond
    bond_wei = (preimage.price * preimage.bond_bps) // BPS_DENOMINATOR
    if preimage.bond_bps > 0 and bond_wei == 0:
        raise PolicyNotCreatable(
            "a nonzero bond rate rounds to zero wei at this price; the contract refuses to "
            "sell unbonded protection"
        )


def _require_addresses(preimage: PolicyPreimage) -> None:
    for label, address in (("buyer", preimage.buyer), ("provider", preimage.provider)):
        if not Web3.is_address(address):
            raise ValueError(f"{label} is not an EVM address")
        if int(address, 16) == 0:
            raise ValueError(f"{label} is the zero address")
    if preimage.buyer.lower() == preimage.provider.lower():
        raise ValueError("buyer and provider must differ")


def policy_hash(preimage: PolicyPreimage) -> str:
    _require_addresses(preimage)

    payload = encode(
        POLICY_ABI_TYPES,
        [
            Web3.to_checksum_address(preimage.buyer),
            Web3.to_checksum_address(preimage.provider),
            preimage.price,
            preimage.bond_bps,
            preimage.accept_by,
            preimage.service_window,
            preimage.payout_delay,
            Web3.keccak(text=preimage.engine_version),
            _bytes32(preimage.buyer_evidence_hash),
            _bytes32(preimage.provider_evidence_hash),
        ],
    )
    return "0x" + bytes(Web3.keccak(payload)).hex()

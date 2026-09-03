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
    """

    ordered = sorted(_bytes32(item) for item in event_ids)
    return "0x" + bytes(Web3.keccak(encode(["bytes32[]"], [ordered]))).hex()


#: The commitment for "this side recalled nothing". Used whenever a counterparty is a
#: stranger, which is every first deal.
EMPTY_EVIDENCE_HASH = evidence_hash([])


def policy_hash(preimage: PolicyPreimage) -> str:
    for label, address in (("buyer", preimage.buyer), ("provider", preimage.provider)):
        if not Web3.is_address(address):
            raise ValueError(f"{label} is not an EVM address")
    if preimage.buyer.lower() == preimage.provider.lower():
        raise ValueError("buyer and provider must differ")

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

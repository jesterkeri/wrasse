"""Cross-language canonical policy commitments."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from eth_abi import encode
from web3 import Web3


@dataclass(frozen=True)
class PolicyPreimage:
    provider: str
    price: int
    bond_bps: int
    service_window: int
    payout_delay: int
    engine_version: str
    evidence_hash: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _bytes32(value: str) -> bytes:
    raw = bytes.fromhex(value.removeprefix("0x"))
    if len(raw) != 32:
        raise ValueError("expected bytes32")
    return raw


def evidence_hash(event_ids: Iterable[str]) -> str:
    ordered = sorted(_bytes32(item) for item in event_ids)
    return "0x" + bytes(Web3.keccak(encode(["bytes32[]"], [ordered]))).hex()


def policy_hash(preimage: PolicyPreimage) -> str:
    if not Web3.is_address(preimage.provider):
        raise ValueError("provider is not an EVM address")
    payload = encode(
        ["address", "uint256", "uint256", "uint64", "uint64", "bytes32", "bytes32"],
        [
            Web3.to_checksum_address(preimage.provider),
            preimage.price,
            preimage.bond_bps,
            preimage.service_window,
            preimage.payout_delay,
            Web3.keccak(text=preimage.engine_version),
            _bytes32(preimage.evidence_hash),
        ],
    )
    return "0x" + bytes(Web3.keccak(payload)).hex()

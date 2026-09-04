"""The deployed contract, and proving the address in front of us is the build we reviewed.

Constants and one pure function can be imitated by different bytecode. Identity here is the
hash of the runtime code, compared against the artifact this repository compiled. Everything
else in this module is the small ABI surface the orchestrator actually calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from web3 import Web3

ESCROW_ABI: list[dict[str, Any]] = [
    {
        "inputs": [
            {"name": "provider", "type": "address"},
            {"name": "providerBondBps", "type": "uint256"},
            {"name": "acceptBy", "type": "uint64"},
            {"name": "serviceWindow", "type": "uint64"},
            {"name": "payoutDelay", "type": "uint64"},
            {"name": "engineVersionHash", "type": "bytes32"},
            {"name": "buyerEvidenceHash", "type": "bytes32"},
            {"name": "providerEvidenceHash", "type": "bytes32"},
        ],
        "name": "createDeal",
        "outputs": [{"name": "dealId", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "buyer", "type": "address"},
            {"name": "provider", "type": "address"},
            {"name": "price", "type": "uint256"},
            {"name": "providerBondBps", "type": "uint256"},
            {"name": "acceptBy", "type": "uint64"},
            {"name": "serviceWindow", "type": "uint64"},
            {"name": "payoutDelay", "type": "uint64"},
            {"name": "engineVersionHash", "type": "bytes32"},
            {"name": "buyerEvidenceHash", "type": "bytes32"},
            {"name": "providerEvidenceHash", "type": "bytes32"},
        ],
        "name": "computePolicyHash",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "MAX_DURATION",
        "outputs": [{"name": "", "type": "uint64"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "BPS_DENOMINATOR",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "MAX_PROVIDER_BOND_BPS",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "nextDealId",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "withdrawable",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

DEFAULT_ARTIFACT = Path("contracts/out/WrasseEscrow.sol/WrasseEscrow.json")


class DeploymentMismatch(RuntimeError):
    """The address in front of us is not the contract this build compiled and reviewed."""


def contract(web3: Any, address: str) -> Any:
    return web3.eth.contract(address=Web3.to_checksum_address(address), abi=ESCROW_ABI)


def artifact_runtime_hash(artifact: Path | str = DEFAULT_ARTIFACT) -> str:
    """Hash the runtime bytecode this repository compiled.

    `WrasseEscrow` takes no constructor arguments and stores no immutables, so its deployed
    code is byte-identical to the artifact's and can be compared directly.
    """

    path = Path(artifact)
    if not path.exists():
        raise DeploymentMismatch(
            f"{path} is missing; run `forge build` so there is a reviewed artifact to compare against"
        )
    document = json.loads(path.read_text(encoding="utf-8"))
    runtime = document["deployedBytecode"]["object"]
    if not runtime or runtime == "0x":
        raise DeploymentMismatch(f"{path} carries no deployed bytecode")
    return "0x" + bytes(Web3.keccak(hexstr=runtime)).hex()


def deployed_runtime_hash(web3: Any, address: str) -> str:
    code = web3.eth.get_code(Web3.to_checksum_address(address))
    if not code or len(code) == 0:
        raise DeploymentMismatch(f"there is no contract at {address}")
    return "0x" + bytes(Web3.keccak(code)).hex()


def create_deal_calldata(
    web3: Any,
    address: str,
    *,
    provider: str,
    bond_bps: int,
    accept_by: int,
    service_window: int,
    payout_delay: int,
    engine_version_hash: str,
    buyer_evidence_hash: str,
    provider_evidence_hash: str,
) -> str:
    return contract(web3, address).encode_abi(
        "createDeal",
        args=[
            Web3.to_checksum_address(provider),
            bond_bps,
            accept_by,
            service_window,
            payout_delay,
            Web3.to_bytes(hexstr=engine_version_hash),
            Web3.to_bytes(hexstr=buyer_evidence_hash),
            Web3.to_bytes(hexstr=provider_evidence_hash),
        ],
    )


def compute_policy_hash_onchain(web3: Any, address: str, preimage: Any) -> str:
    """Ask the deployed contract for the commitment it would store.

    This is the differential check moved from the test fixture to the live deployment: if the
    Python encoding has drifted from the contract in front of us, the two answers differ here
    rather than after a deal exists.
    """

    result = contract(web3, address).functions.computePolicyHash(
        Web3.to_checksum_address(preimage.buyer),
        Web3.to_checksum_address(preimage.provider),
        preimage.price,
        preimage.bond_bps,
        preimage.accept_by,
        preimage.service_window,
        preimage.payout_delay,
        Web3.keccak(text=preimage.engine_version),
        Web3.to_bytes(hexstr=preimage.buyer_evidence_hash),
        Web3.to_bytes(hexstr=preimage.provider_evidence_hash),
    ).call()
    return "0x" + bytes(result).hex()

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
        "inputs": [{"name": "dealId", "type": "uint256"}],
        "name": "acceptDeal",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"name": "dealId", "type": "uint256"}],
        "name": "markDelivered",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "dealId", "type": "uint256"}],
        "name": "releaseDeal",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "dealId", "type": "uint256"}],
        "name": "claimPayment",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "dealId", "type": "uint256"}],
        "name": "claimTimeout",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "dealId", "type": "uint256"}],
        "name": "cancelUnaccepted",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "recipient", "type": "address"}],
        "name": "withdraw",
        "outputs": [{"name": "amount", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "deals",
        "outputs": [
            {"name": "buyer", "type": "address"},
            {"name": "provider", "type": "address"},
            {"name": "price", "type": "uint256"},
            {"name": "providerBond", "type": "uint256"},
            {"name": "acceptBy", "type": "uint64"},
            {"name": "acceptedAt", "type": "uint64"},
            {"name": "serviceWindow", "type": "uint64"},
            {"name": "deadline", "type": "uint64"},
            {"name": "payoutDelay", "type": "uint64"},
            {"name": "payoutAvailableAt", "type": "uint64"},
            {"name": "policyHash", "type": "bytes32"},
            {"name": "state", "type": "uint8"},
        ],
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


CREATE_DEAL_TYPES = [
    "address",
    "uint256",
    "uint64",
    "uint64",
    "uint64",
    "bytes32",
    "bytes32",
    "bytes32",
]
CREATE_DEAL_SELECTOR = "0x" + bytes(
    Web3.keccak(text="createDeal(address,uint256,uint64,uint64,uint64,bytes32,bytes32,bytes32)")
)[:4].hex()


def decode_create_deal(calldata: str) -> dict[str, Any] | None:
    """Pull the committed arguments back out of the calldata, or None if it is not a createDeal.

    The ledger stores a description of a transaction beside the bytes. Only reading the bytes
    can show that the description is true.
    """

    from eth_abi import decode

    raw = bytes.fromhex(calldata.removeprefix("0x"))
    if len(raw) < 4 or "0x" + raw[:4].hex() != CREATE_DEAL_SELECTOR:
        return None

    values = decode(CREATE_DEAL_TYPES, raw[4:])
    return {
        "provider": Web3.to_checksum_address(values[0]),
        "bond_bps": int(values[1]),
        "accept_by": int(values[2]),
        "service_window": int(values[3]),
        "payout_delay": int(values[4]),
        "engine_version_hash": "0x" + values[5].hex(),
        "buyer_evidence_hash": "0x" + values[6].hex(),
        "provider_evidence_hash": "0x" + values[7].hex(),
    }


#: Every deal action the loop performs, with the role allowed to send it and the deal state
#: the contract requires. State is a signing-time precondition, never part of the commitment:
#: it can change before inclusion, and the contract reverts safely if it does.
DEAL_ACTIONS: dict[str, dict[str, Any]] = {
    "acceptDeal": {"role": "provider", "expects": "Offered", "payable": True},
    "markDelivered": {"role": "provider", "expects": "Accepted", "payable": False},
    "releaseDeal": {"role": "buyer", "expects": "Delivered", "payable": False},
    "claimPayment": {"role": "provider", "expects": "Delivered", "payable": False},
    "claimTimeout": {"role": "buyer", "expects": "Accepted", "payable": False},
    "cancelUnaccepted": {"role": "buyer", "expects": "Offered", "payable": False},
}

#: `WrasseEscrow.State`, in declaration order.
DEAL_STATES = ("Offered", "Accepted", "Delivered", "Released", "TimedOut", "Cancelled")

_DEAL_ACTION_SELECTORS = {
    "0x" + bytes(Web3.keccak(text=f"{name}(uint256)"))[:4].hex(): name for name in DEAL_ACTIONS
}
WITHDRAW_SELECTOR = "0x" + bytes(Web3.keccak(text="withdraw(address)"))[:4].hex()


def deal_action_calldata(web3: Any, address: str, action: str, deal_id: int) -> str:
    if action not in DEAL_ACTIONS:
        raise DeploymentMismatch(f"{action} is not a deal action")
    return contract(web3, address).encode_abi(action, args=[deal_id])


def withdraw_calldata(web3: Any, address: str, recipient: str) -> str:
    return contract(web3, address).encode_abi(
        "withdraw", args=[Web3.to_checksum_address(recipient)]
    )


def decode_deal_action(calldata: str) -> dict[str, Any] | None:
    """Recover the action and deal id from calldata, or None if it is not a deal action."""

    from eth_abi import decode

    raw = bytes.fromhex(calldata.removeprefix("0x"))
    if len(raw) != 36:
        return None
    action = _DEAL_ACTION_SELECTORS.get("0x" + raw[:4].hex())
    if action is None:
        return None
    return {"action": action, "deal_id": int(decode(["uint256"], raw[4:])[0])}


def decode_withdraw(calldata: str) -> dict[str, Any] | None:
    from eth_abi import decode

    raw = bytes.fromhex(calldata.removeprefix("0x"))
    if len(raw) != 36 or "0x" + raw[:4].hex() != WITHDRAW_SELECTOR:
        return None
    return {"action": "withdraw", "recipient": Web3.to_checksum_address(decode(["address"], raw[4:])[0])}


def read_deal(web3: Any, address: str, deal_id: int, block: Any = "latest") -> dict[str, Any]:
    """The deal as the contract holds it, named rather than positional."""

    values = contract(web3, address).functions.deals(deal_id).call(block_identifier=block)
    return {
        "buyer": Web3.to_checksum_address(values[0]),
        "provider": Web3.to_checksum_address(values[1]),
        "price": int(values[2]),
        "provider_bond": int(values[3]),
        "accept_by": int(values[4]),
        "accepted_at": int(values[5]),
        "service_window": int(values[6]),
        "deadline": int(values[7]),
        "payout_delay": int(values[8]),
        "payout_available_at": int(values[9]),
        "policy_hash": "0x" + bytes(values[10]).hex(),
        "state": DEAL_STATES[int(values[11])],
    }


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


#: `DealCreated`'s topic zero. The deal id is `topics[1]`, indexed, so it needs no ABI decode.
DEAL_CREATED_SIGNATURE = Web3.keccak(
    text="DealCreated(uint256,address,address,uint256,uint256,uint256,uint64,uint64,uint64,bytes32)"
)


def deal_id_from_receipt(web3: Any, address: str, tx_hash: str) -> int:
    """The id the contract assigned to a deal, read from its own creation receipt.

    `createDeal` returns the id to a caller, and a transaction has no return value, so the id
    exists nowhere the sender can see until the log does. Nothing downstream can proceed
    without it: every later action in the lifecycle names the deal by id.

    Exactly one `DealCreated` from the configured escrow is accepted. Two would mean this
    receipt describes two creations and no rule here could say which one the caller meant;
    none means the transaction did not create a deal at all. Both refuse rather than guess,
    for the same reason `reconciler.verify_outcome` refuses a receipt carrying two outcomes.
    """

    escrow_address = Web3.to_checksum_address(address)
    receipt = web3.eth.get_transaction_receipt(tx_hash)
    logs = receipt["logs"] if isinstance(receipt, dict) else receipt.logs
    found: list[int] = []
    for log in logs:
        emitter = log["address"] if isinstance(log, dict) else log.address
        if Web3.to_checksum_address(emitter) != escrow_address:
            continue
        topics = log["topics"] if isinstance(log, dict) else log.topics
        if not topics or bytes(topics[0]) != bytes(DEAL_CREATED_SIGNATURE):
            continue
        if len(topics) != 4:
            raise DeploymentMismatch(
                f"a DealCreated in {tx_hash} carries {len(topics)} topics, not 4"
            )
        found.append(int.from_bytes(bytes(topics[1]), "big"))

    if len(found) != 1:
        raise DeploymentMismatch(
            f"{tx_hash} carries {len(found)} DealCreated logs from {address}, and exactly one "
            "is the only count that names a single deal"
        )
    return found[0]

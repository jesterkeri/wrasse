"""The orchestrator against real bytecode on a real EVM, before any testnet funds move.

The fake node in `test_chain.py` proves the failure handling. This proves the parts a fake
cannot: that the compiled contract, the ABI encoding, the gas estimate, the signature and the
receipt all fit together, and that a deliberate crash mid-broadcast really is recoverable by
a process that starts from nothing but the ledger.

Skipped cleanly when anvil is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from eth_account import Account
from web3 import Web3

from wrasse import chain
from wrasse.cli import main

ANVIL = shutil.which("anvil") or str(Path.home() / ".foundry" / "bin" / "anvil")
ARTIFACT = Path(__file__).resolve().parents[1] / "contracts/out/WrasseEscrow.sol/WrasseEscrow.json"

pytestmark = pytest.mark.skipif(
    not Path(ANVIL).exists() or not ARTIFACT.exists(),
    reason="needs anvil and a compiled artifact",
)

CHAIN_ID = 84532
PROVIDER = Web3.to_checksum_address("0x" + "0b" * 20)
PASSWORD = "rehearsal-only"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def anvil_url() -> str:
    """A local chain pinned to the same id the configuration uses.

    Pinning matters: a rehearsal on chain 31337 would never exercise the chain-id checks that
    stand between a policy document and the wrong network.
    """

    port = _free_port()
    process = subprocess.Popen(
        [ANVIL, "--chain-id", str(CHAIN_ID), "--port", str(port), "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        web3 = Web3(Web3.HTTPProvider(url))
        for _ in range(120):  # wait for readiness rather than sleeping a guessed amount
            try:
                if web3.eth.chain_id == CHAIN_ID:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("anvil did not become ready")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def rehearsal(anvil_url, tmp_path, monkeypatch):
    """A funded buyer, a deployed escrow, a deployment record, and an isolated ledger."""

    web3 = Web3(Web3.HTTPProvider(anvil_url))
    funder = web3.eth.accounts[0]
    # A fresh wallet per test. The chain is shared for speed, so sharing a wallet would let
    # one test's nonce leak into another's assertions about how many deals were sent.
    buyer = Account.create()

    web3.eth.send_transaction({"from": funder, "to": buyer.address, "value": Web3.to_wei(1, "ether")})

    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    deploy_hash = web3.eth.send_transaction(
        {"from": funder, "data": artifact["bytecode"]["object"], "gas": 3_000_000}
    )
    receipt = web3.eth.wait_for_transaction_receipt(deploy_hash)
    address = Web3.to_checksum_address(receipt["contractAddress"])

    keystore = tmp_path / "buyer-keystore"
    keystore.write_text(json.dumps(Account.encrypt(buyer.key, PASSWORD, kdf="pbkdf2")))
    password_file = tmp_path / "keystore.password"
    password_file.write_text(PASSWORD + "\n")

    record = tmp_path / "deployment.json"
    record.write_text(json.dumps({
        "chain_id": CHAIN_ID,
        "address": address,
        "deployment_tx": deploy_hash.hex(),
        "block_number": int(receipt["blockNumber"]),
        "block_hash": receipt["blockHash"].hex(),
        "runtime_bytecode_hash": "0x" + bytes(Web3.keccak(web3.eth.get_code(address))).hex(),
        "commit": "rehearsal",
        "solc_version": "0.8.30",
        "optimizer": {"enabled": True, "runs": 200},
    }))

    for name, value in {
        "BASE_SEPOLIA_RPC_URL": anvil_url,
        "BASE_SEPOLIA_CHAIN_ID": str(CHAIN_ID),
        "WRASSE_ESCROW_ADDRESS": address,
        "WRASSE_BUYER_ADDRESS": buyer.address,
        "WRASSE_PROVIDER_A_ADDRESS": PROVIDER,
        "WRASSE_KEYSTORE": str(keystore),
        "WRASSE_KEYSTORE_PASSWORD_FILE": str(password_file),
        "WRASSE_TX_DB": str(tmp_path / "state" / "transactions.db"),
        "WRASSE_DEPLOYMENT_RECORD": str(record),
        "WRASSE_MEMORY_PATH": str(tmp_path / "memory.db"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(chain.BROADCAST_ENV, raising=False)

    return {"web3": web3, "address": address, "buyer": buyer, "tmp": tmp_path}


def _quote(rehearsal, capsys) -> Path:
    output = rehearsal["tmp"] / "policy.json"
    assert main([
        "policy", PROVIDER, "--buyer", rehearsal["buyer"].address,
        "--accept-window", "3600", "--output", str(output),
    ]) == 0
    capsys.readouterr()
    return output


def test_deploy_check_accepts_the_build_it_compiled(rehearsal, capsys):
    assert main(["deploy-check", "--require-fresh"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert all(item["ok"] for item in report["checks"])


def test_deploy_check_rejects_an_address_holding_other_code(rehearsal, capsys, monkeypatch):
    """Different bytecode is the case constants alone would wave through."""
    web3 = rehearsal["web3"]
    other = web3.eth.send_transaction({"from": web3.eth.accounts[0], "data": "0x60016000f3"})
    address = web3.eth.wait_for_transaction_receipt(other)["contractAddress"]
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", Web3.to_checksum_address(address))

    assert main(["deploy-check"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert any(not item["ok"] and "bytecode" in item["check"] for item in report["checks"])


def test_a_deal_is_created_confirmed_and_rebuilds_its_own_commitment(rehearsal, capsys, monkeypatch):
    policy_path = _quote(rehearsal, capsys)
    web3 = rehearsal["web3"]
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")

    # Let chain time move between the quote and the signature, which is what happens in any
    # real run. The deadline is re-derived at signing, so the commitment must move with it.
    web3.provider.make_request("evm_increaseTime", [5])
    web3.provider.make_request("evm_mine", [])

    assert main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == chain.PENDING
    assert created["moved_fields"] == ["accept_by"]
    assert created["signed"]["accept_by"] > created["quoted"]["accept_by"]
    assert created["signed"]["policy_hash"] != created["quoted"]["policy_hash"], (
        "a moved deadline must move the commitment, or the hash is not covering it"
    )

    receipt = web3.eth.wait_for_transaction_receipt(created["tx_hash"])
    assert receipt["status"] == 1

    assert main(["tx-resolve", "--local-confirmation-blocks", "0"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved[0]["status"] == chain.CONFIRMED_SUCCESS

    # The claim the contract review was built around: the receipt alone rebuilds the
    # commitment, and it equals the one this run signed.
    logs = _decode_creation(web3, rehearsal["address"], receipt)
    assert logs["policy_hash"] == created["signed"]["policy_hash"]
    assert logs["accept_by"] == created["signed"]["accept_by"]


def test_the_same_quote_and_profile_never_sends_twice(rehearsal, capsys, monkeypatch):
    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")

    assert main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"]) == 0
    first = json.loads(capsys.readouterr().out)

    assert main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert second["already_signed"] is True
    assert second["tx_hash"] == first["tx_hash"]
    nonce = rehearsal["web3"].eth.get_transaction_count(rehearsal["buyer"].address, "latest")
    assert nonce == 1, "a retry must not put a second transaction on chain"


def test_a_crash_after_broadcast_is_recovered_from_the_ledger(rehearsal, capsys, monkeypatch):
    """The gate's real test.

    The failpoint exits after the bytes are gone and before anything records that they went,
    which is precisely the window that used to produce a second funded offer.
    """
    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    monkeypatch.setenv(chain.FAILPOINT_ENV, chain.CRASH_AFTER_SEND)

    with pytest.raises(SystemExit) as exit_info:
        main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"])
    assert exit_info.value.code == 97
    capsys.readouterr()

    # A fresh process: nothing in memory, only the ledger and the chain.
    chain.arm_failpoint(None)
    monkeypatch.delenv(chain.FAILPOINT_ENV, raising=False)

    assert main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"]) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["already_signed"] is True
    assert "Nothing was built or sent" in recovered["note"]

    assert main(["tx-resolve", "--local-confirmation-blocks", "0"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved[0]["status"] == chain.CONFIRMED_SUCCESS

    nonce = rehearsal["web3"].eth.get_transaction_count(rehearsal["buyer"].address, "latest")
    assert nonce == 1, "the crash must not have produced a second deal"


def test_tx_status_never_writes_and_never_sends(rehearsal, capsys, monkeypatch):
    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    assert main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"]) == 0
    capsys.readouterr()

    ledger = chain.TransactionLedger(os.environ["WRASSE_TX_DB"])
    before = [(row.intent_id, row.status, row.updated_at) for row in ledger.rows()]

    assert main(["tx-status"]) == 0
    capsys.readouterr()

    after = [(row.intent_id, row.status, row.updated_at) for row in ledger.rows()]
    assert before == after, "tx-status must not persist anything it learns"


def test_broadcasting_without_the_opt_in_is_refused(rehearsal, capsys):
    policy_path = _quote(rehearsal, capsys)
    with pytest.raises(chain.BroadcastNotAuthorised):
        main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"])


def _decode_creation(web3: Web3, address: str, receipt) -> dict:
    """Rebuild the committed terms from the creation receipt alone."""

    created_sig = Web3.keccak(
        text="DealCreated(uint256,address,address,uint256,uint256,uint256,uint64,uint64,uint64,bytes32)"
    )
    from eth_abi import decode

    for log in receipt["logs"]:
        if Web3.to_checksum_address(log["address"]) != Web3.to_checksum_address(address):
            continue
        if bytes(log["topics"][0]) != bytes(created_sig):
            continue
        values = decode(
            ["uint256", "uint256", "uint256", "uint64", "uint64", "uint64", "bytes32"],
            bytes(log["data"]),
        )
        return {"accept_by": values[3], "policy_hash": "0x" + values[6].hex()}
    raise AssertionError("no DealCreated log from the escrow in this receipt")

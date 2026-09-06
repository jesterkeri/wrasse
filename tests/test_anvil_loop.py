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
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from eth_account import Account
from web3 import Web3

from conftest import write_persona

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
        "WRASSE_BUYER_MEMORY_PATH": str(tmp_path / "buyer-memory.db"),
        "WRASSE_PROVIDER_MEMORY_PATH": str(tmp_path / "provider-memory.db"),
        "WRASSE_PROVIDER_PERSONA": str(write_persona(tmp_path, PROVIDER)),
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


def test_a_released_nonce_is_reused_and_actually_mines(rehearsal, capsys, monkeypatch):
    """The end-to-end version of the nonce-gap failure.

    A send refused before it entered a mempool leaves nonce 0 unused. If the ledger kept it
    allocated, the next transaction would sign at nonce 1 and sit unmined forever behind the
    gap. Signing is not the property under test here; mining is.
    """
    ledger = chain.TransactionLedger(os.environ["WRASSE_TX_DB"])
    buyer = rehearsal["buyer"]

    # Stand in for a deterministic rejection on the very first attempt.
    bounced = Account.from_key(buyer.key).sign_transaction({
        "chainId": CHAIN_ID, "nonce": 0, "to": rehearsal["address"], "data": "0xdeadbeef",
        "value": 1, "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**6,
        "gas": 250_000, "type": 2,
    })
    raw = "0x" + bytes(bounced.raw_transaction).hex()
    row, created = ledger.record_signed(
        chain_id=CHAIN_ID, wallet=buyer.address, contract_address=rehearsal["address"],
        intent_id="bounced:urgent", read_chain_nonce=lambda: 0,
        sign=lambda nonce: chain.SignedIntent(
            nonce=nonce, calldata="0xdeadbeef", value_wei=1, max_fee_wei=10**9,
            max_priority_wei=10**6, gas_limit=250_000, accept_by=0, preimage={},
            tx_hash="0x" + bytes(Web3.keccak(hexstr=raw)).hex(), raw=raw,
        ),
    )
    assert created is True and row.nonce == 0
    ledger.set_status(row, chain.UNBROADCAST, last_error="insufficient funds for gas * price + value")

    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    assert main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"]) == 0
    created_deal = json.loads(capsys.readouterr().out)
    assert created_deal["nonce"] == 0, "the released nonce must be reused, not skipped"

    receipt = rehearsal["web3"].eth.wait_for_transaction_receipt(created_deal["tx_hash"])
    assert receipt["status"] == 1, "the replacement must actually mine, not merely sign"


def test_an_included_receipt_that_stops_being_canonical_is_recorded_as_reorged(
    rehearsal, capsys, monkeypatch
):
    """The CLI lifecycle, not the helper.

    A reorg turns an included row's verdict into one the unmined resolver would return, which
    the state graph refuses. Judging included rows as included first is what keeps that from
    raising instead of being recorded.
    """
    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    assert main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"]) == 0
    created = json.loads(capsys.readouterr().out)
    rehearsal["web3"].eth.wait_for_transaction_receipt(created["tx_hash"])

    # Anvil pins its safe head at zero, so the real rule leaves the row included, not confirmed.
    assert main(["tx-resolve"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == chain.INCLUDED_SUCCESS

    # The block this was included in is no longer the block we recorded.
    ledger = chain.TransactionLedger(os.environ["WRASSE_TX_DB"])
    row = ledger.rows()[0]
    with sqlite3.connect(os.environ["WRASSE_TX_DB"]) as connection:
        connection.execute(
            "UPDATE transactions SET block_hash = ? WHERE intent_id = ?",
            ("0x" + "de" * 32, row.intent_id),
        )

    assert main(["tx-resolve"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == chain.REORGED


def test_the_rehearsal_confirmation_policy_is_refused_against_a_real_endpoint(
    rehearsal, capsys, monkeypatch
):
    """The flag must be fenced by something a real network cannot satisfy."""
    monkeypatch.setenv("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org")
    with pytest.raises(chain.NotALocalChain, match="not a loopback"):
        main(["tx-resolve", "--local-confirmation-blocks", "0"])


def test_create_deal_refuses_an_address_that_is_not_the_reviewed_build(
    rehearsal, capsys, monkeypatch
):
    """deploy-check reports; the money path has to refuse.

    A contract can implement one matching pure function and still make createDeal do something
    else entirely, so proving the hash of one function is not proving the deployment.
    """
    web3 = rehearsal["web3"]
    other = web3.eth.send_transaction({"from": web3.eth.accounts[0], "data": "0x60016000f3"})
    address = Web3.to_checksum_address(web3.eth.wait_for_transaction_receipt(other)["contractAddress"])

    # Quote against the decoy too, so the document itself is consistent and the only thing
    # left standing between the wallet and the wrong contract is the identity gate.
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", address)
    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")

    with pytest.raises(RuntimeError, match="deployment record names"):
        main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"])

    # And with the record pointed at the decoy as well, the bytecode is what refuses.
    record = Path(os.environ["WRASSE_DEPLOYMENT_RECORD"])
    body = json.loads(record.read_text())
    body["address"] = address
    body["block_number"] = 0
    body["block_hash"] = "0x" + bytes(web3.eth.get_block(0)["hash"]).hex()
    record.write_text(json.dumps(body))

    with pytest.raises(RuntimeError, match="not the artifact this build compiled"):
        main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"])


def _unsent_row_with_deadline(rehearsal, accept_by: int):
    """A signed, never-broadcast createDeal whose acceptance deadline is already in the past."""
    from wrasse import escrow
    from wrasse.policy_hash import EMPTY_EVIDENCE_HASH, ENGINE_VERSION, PolicyPreimage

    buyer = rehearsal["buyer"]
    address = rehearsal["address"]
    preimage = PolicyPreimage(
        buyer=buyer.address, provider=PROVIDER, price=10**13, bond_bps=2_000,
        accept_by=accept_by, service_window=7_200, payout_delay=1_800,
        engine_version=ENGINE_VERSION,
        buyer_evidence_hash=EMPTY_EVIDENCE_HASH, provider_evidence_hash=EMPTY_EVIDENCE_HASH,
    )
    calldata = escrow.create_deal_calldata(
        rehearsal["web3"], address, provider=PROVIDER, bond_bps=2_000, accept_by=accept_by,
        service_window=7_200, payout_delay=1_800,
        engine_version_hash="0x" + bytes(Web3.keccak(text=ENGINE_VERSION)).hex(),
        buyer_evidence_hash=EMPTY_EVIDENCE_HASH, provider_evidence_hash=EMPTY_EVIDENCE_HASH,
    )
    signed = Account.from_key(buyer.key).sign_transaction({
        "chainId": CHAIN_ID, "nonce": 0, "to": address, "data": calldata, "value": 10**13,
        "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**6, "gas": 400_000, "type": 2,
    })
    raw = "0x" + bytes(signed.raw_transaction).hex()
    ledger = chain.TransactionLedger(os.environ["WRASSE_TX_DB"])
    row, _ = ledger.record_signed(
        chain_id=CHAIN_ID, wallet=buyer.address, contract_address=address,
        intent_id="expired:urgent", read_chain_nonce=lambda: 0,
        sign=lambda nonce: chain.SignedIntent(
            nonce=nonce, calldata=calldata, value_wei=10**13, max_fee_wei=10**9,
            max_priority_wei=10**6, gas_limit=400_000, accept_by=accept_by,
            preimage=preimage.as_dict(),
            tx_hash="0x" + bytes(Web3.keccak(hexstr=raw)).hex(), raw=raw,
        ),
    )
    return ledger, ledger.set_status(row, chain.SEND_ATTEMPTED, bump_attempts=True)


def test_an_expired_transaction_is_not_resent_by_the_command_that_can_resend(
    rehearsal, capsys, monkeypatch
):
    """Only the chain's clock decides whether a deadline has passed.

    The helper takes chain time as an argument, so a command that never supplies it would
    report `unknown` and cheerfully resend bytes the contract must reject.
    """
    web3 = rehearsal["web3"]
    past = int(web3.eth.get_block("latest")["timestamp"]) - 10
    _, row = _unsent_row_with_deadline(rehearsal, past)
    before = web3.eth.get_transaction_count(rehearsal["buyer"].address, "latest")

    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    assert main(["tx-resolve", "--rebroadcast"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report[0]["status"] == chain.STUCK
    assert web3.eth.get_transaction_count(rehearsal["buyer"].address, "latest") == before


def test_a_live_deadline_still_allows_the_deliberate_resend(rehearsal, capsys, monkeypatch):
    """The counterpart, so the test above is proving the deadline and not just refusing."""
    web3 = rehearsal["web3"]
    future = int(web3.eth.get_block("latest")["timestamp"]) + 3_600
    _, row = _unsent_row_with_deadline(rehearsal, future)

    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    assert main(["tx-resolve", "--rebroadcast"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert "resent" in report[0]["action"]


def test_replay_refuses_when_the_deployment_no_longer_matches(rehearsal, capsys, monkeypatch):
    """A resend is a send, so it carries the same preconditions as the first one.

    If the deployment were reorged out or the address repointed, replay would otherwise push
    value at code nobody reviewed.
    """
    web3 = rehearsal["web3"]
    future = int(web3.eth.get_block("latest")["timestamp"]) + 3_600
    _unsent_row_with_deadline(rehearsal, future)

    record = Path(os.environ["WRASSE_DEPLOYMENT_RECORD"])
    body = json.loads(record.read_text())
    body["runtime_bytecode_hash"] = "0x" + "ab" * 32
    record.write_text(json.dumps(body))

    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    before = web3.eth.get_transaction_count(rehearsal["buyer"].address, "latest")
    with pytest.raises(RuntimeError, match="does not match the recorded deployment"):
        main(["tx-resolve", "--rebroadcast"])
    assert web3.eth.get_transaction_count(rehearsal["buyer"].address, "latest") == before


def test_a_deadline_that_expires_during_resolution_stops_the_resend(
    rehearsal, capsys, monkeypatch
):
    """The opening observation is not the one that matters.

    Receipt, transaction and nonce reads all sit between it and the send, each able to retry
    against a twenty second timeout. A transaction can be live when the command starts and
    dead by the time it would go out.
    """
    from wrasse import cli

    web3 = rehearsal["web3"]
    now = int(web3.eth.get_block("latest")["timestamp"])
    _unsent_row_with_deadline(rehearsal, now + 3_600)

    readings = iter([now, now + 7_200])  # live when resolution starts, expired at the send
    monkeypatch.setattr(cli, "_chain_now", lambda _web3: next(readings, now + 7_200))
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")

    before = web3.eth.get_transaction_count(rehearsal["buyer"].address, "latest")
    assert main(["tx-resolve", "--rebroadcast"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report[0]["status"] == chain.STUCK
    assert "passed while resolving" in report[0]["action"]
    assert web3.eth.get_transaction_count(rehearsal["buyer"].address, "latest") == before


@pytest.fixture
def both_roles(rehearsal, monkeypatch):
    """A funded provider with its own keystore, so role separation is real rather than named."""
    web3 = rehearsal["web3"]
    provider = Account.create()
    web3.eth.send_transaction(
        {"from": web3.eth.accounts[0], "to": provider.address, "value": Web3.to_wei(1, "ether")}
    )
    keystore = rehearsal["tmp"] / "provider-keystore"
    keystore.write_text(json.dumps(Account.encrypt(provider.key, PASSWORD, kdf="pbkdf2")))

    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", provider.address)
    monkeypatch.setenv("WRASSE_PROVIDER_A_KEYSTORE", str(keystore))
    monkeypatch.setenv(
        "WRASSE_PROVIDER_PERSONA",
        str(write_persona(rehearsal["tmp"], provider.address)),
    )
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    return {**rehearsal, "provider": provider}


def _open_deal(both_roles, capsys, *, service_window=600, payout_delay=60) -> int:
    output = both_roles["tmp"] / "policy.json"
    assert main([
        "policy", os.environ["WRASSE_PROVIDER_A_ADDRESS"],
        "--buyer", both_roles["buyer"].address, "--accept-window", "600",
        "--service-window", str(service_window), "--payout-delay", str(payout_delay),
        "--output", str(output),
    ]) == 0
    capsys.readouterr()
    # The same baselines the quote was produced with. `create-deal` derives the terms again
    # from the two memories and refuses a document it cannot reproduce, so a baseline it was
    # not told about looks exactly like an edited price.
    assert main([
        "create-deal", "--policy", str(output), "--profile", "urgent",
        "--service-window", str(service_window), "--payout-delay", str(payout_delay),
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    both_roles["web3"].eth.wait_for_transaction_receipt(created["tx_hash"])
    assert main(["tx-resolve"]) == 0
    capsys.readouterr()
    return 0 if "deal_id" not in created else created["deal_id"]


def test_the_delivered_lifecycle_settles_and_both_sides_collect(both_roles, capsys):
    """create, accept, deliver, release, and both parties collect what they are owed."""
    web3 = both_roles["web3"]
    deal_id = _open_deal(both_roles, capsys)

    for command in (["accept-deal", "--deal-id", str(deal_id)],
                    ["mark-delivered", "--deal-id", str(deal_id)],
                    ["release-deal", "--deal-id", str(deal_id)]):
        assert main(command) == 0
        sent = json.loads(capsys.readouterr().out)
        web3.eth.wait_for_transaction_receipt(sent["tx_hash"])
        assert main(["tx-resolve"]) == 0
        capsys.readouterr()

    state = escrow_state(web3, both_roles["address"], deal_id)
    assert state == "Released"

    assert main(["withdraw", "--role", "provider", "--to", os.environ["WRASSE_PROVIDER_A_ADDRESS"]]) == 0
    collected = json.loads(capsys.readouterr().out)
    web3.eth.wait_for_transaction_receipt(collected["tx_hash"])
    assert "an estimate" in collected["note"]


def test_a_role_cannot_perform_the_other_roles_action(both_roles, capsys):
    """`markDelivered` is the provider's. The buyer's wallet is refused on identity."""
    deal_id = _open_deal(both_roles, capsys)
    assert main(["accept-deal", "--deal-id", str(deal_id)]) == 0
    sent = json.loads(capsys.readouterr().out)
    both_roles["web3"].eth.wait_for_transaction_receipt(sent["tx_hash"])
    assert main(["tx-resolve"]) == 0
    capsys.readouterr()

    # Point the provider role at the buyer's keystore: the derived address will not match.
    os.environ["WRASSE_PROVIDER_A_KEYSTORE"] = os.environ["WRASSE_KEYSTORE"]
    with pytest.raises(chain.RoleMismatch):
        main(["mark-delivered", "--deal-id", str(deal_id)])


def test_an_action_the_deal_is_not_ready_for_is_refused_before_signing(both_roles, capsys):
    deal_id = _open_deal(both_roles, capsys)
    with pytest.raises(RuntimeError, match="is Offered, and markDelivered needs it Accepted"):
        main(["mark-delivered", "--deal-id", str(deal_id)])


def test_a_repeated_deal_action_sends_nothing(both_roles, capsys):
    deal_id = _open_deal(both_roles, capsys)
    assert main(["accept-deal", "--deal-id", str(deal_id)]) == 0
    first = json.loads(capsys.readouterr().out)
    both_roles["web3"].eth.wait_for_transaction_receipt(first["tx_hash"])

    assert main(["accept-deal", "--deal-id", str(deal_id)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["already_signed"] is True and second["tx_hash"] == first["tx_hash"]


def escrow_state(web3, address, deal_id):
    from wrasse import escrow
    return escrow.read_deal(web3, address, deal_id)["state"]


def test_a_reconciled_receipt_is_findable_by_the_side_that_will_price_it(both_roles, capsys):
    """Storing the record is only half of reconciliation.

    A live run found this: reconcile wrote the canonical fact into both memories and never
    indexed it, so both sides held the receipt and neither could find it when pricing. The
    index is repairable, which is how that run recovered, but reconcile must not create the
    damage in the first place.
    """
    from wrasse.store import WrasseStore

    web3 = both_roles["web3"]
    deal_id = _open_deal(both_roles, capsys, service_window=1)

    assert main(["accept-deal", "--deal-id", str(deal_id)]) == 0
    accepted = json.loads(capsys.readouterr().out)
    web3.eth.wait_for_transaction_receipt(accepted["tx_hash"])
    assert main(["tx-resolve", "--local-confirmation-blocks", "0"]) == 0
    capsys.readouterr()

    web3.provider.make_request("evm_increaseTime", [120])
    web3.provider.make_request("evm_mine", [])

    assert main(["claim-timeout", "--deal-id", str(deal_id)]) == 0
    claimed = json.loads(capsys.readouterr().out)
    web3.eth.wait_for_transaction_receipt(claimed["tx_hash"])

    # Anvil pins its safe head at zero forever, so the rehearsal names its local policy.
    assert main(["tx-resolve", "--local-confirmation-blocks", "0"]) == 0
    capsys.readouterr()

    assert main(["reconcile", "--tx", claimed["tx_hash"]]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["event_type"] == "timeout_claimed_without_delivery"
    assert report["delivered_to"]["buyer"]["indexed_under"] == os.environ["WRASSE_PROVIDER_A_ADDRESS"]
    assert report["delivered_to"]["provider"]["indexed_under"] == both_roles["buyer"].address

    # The point: each side can now find it on the path that sets prices.
    for role, counterparty in (
        ("buyer", os.environ["WRASSE_PROVIDER_A_ADDRESS"]),
        ("provider", both_roles["buyer"].address),
    ):
        owner = both_roles["buyer"].address if role == "buyer" else os.environ["WRASSE_PROVIDER_A_ADDRESS"]
        store = WrasseStore.open(
            os.environ[f"WRASSE_{role.upper()}_MEMORY_PATH"], role=role, owner_address=owner,
            chain_id=CHAIN_ID, escrow_address=both_roles["address"],
        )
        recalled = store.recall(counterparty)
        assert len(recalled.evidence) == 1, f"the {role} cannot find what it was just told"
        assert recalled.evidence[0]["event_type"] == "timeout_claimed_without_delivery"


def test_a_quote_refuses_when_the_two_memories_disagree(both_roles, capsys):
    """A partial dual-store delivery is an ordinary crash, not an exotic one.

    Quoting across it would price one side on a history the other cannot see, and the document
    would look complete on both.
    """
    from wrasse.store import INDEX_CATEGORY, WrasseStore

    web3 = both_roles["web3"]
    deal_id = _open_deal(both_roles, capsys, service_window=1)
    assert main(["accept-deal", "--deal-id", str(deal_id)]) == 0
    accepted = json.loads(capsys.readouterr().out)
    web3.eth.wait_for_transaction_receipt(accepted["tx_hash"])
    assert main(["tx-resolve", "--local-confirmation-blocks", "0"]) == 0
    capsys.readouterr()

    web3.provider.make_request("evm_increaseTime", [120])
    web3.provider.make_request("evm_mine", [])
    assert main(["claim-timeout", "--deal-id", str(deal_id)]) == 0
    claimed = json.loads(capsys.readouterr().out)
    web3.eth.wait_for_transaction_receipt(claimed["tx_hash"])
    assert main(["tx-resolve", "--local-confirmation-blocks", "0"]) == 0
    capsys.readouterr()
    assert main(["reconcile", "--tx", claimed["tx_hash"]]) == 0
    capsys.readouterr()

    # Take the receipt away from one side only, as a crash between the two writes would.
    provider = WrasseStore.open(
        os.environ["WRASSE_PROVIDER_MEMORY_PATH"], role="provider",
        owner_address=os.environ["WRASSE_PROVIDER_A_ADDRESS"], chain_id=CHAIN_ID,
        escrow_address=both_roles["address"],
    )
    name = provider._index_name(both_roles["buyer"].address)
    provider.memory.set_entity(INDEX_CATEGORY, name, {"event_ids": []}, status="verified")

    with pytest.raises(RuntimeError, match="disagree about what happened"):
        main([
            "policy", os.environ["WRASSE_PROVIDER_A_ADDRESS"],
            "--buyer", both_roles["buyer"].address, "--accept-window", "600",
            "--output", str(both_roles["tmp"] / "split.json"),
        ])


# --------------------------------------------------------------------------------------
# The document is a display artifact; the memories are the authority
# --------------------------------------------------------------------------------------


def _forge(path: Path, mutate) -> Path:
    """Edit a document and make every hash in it agree with the edit.

    This is the attack that internal consistency cannot see. `policy_hash` is public and
    unkeyed, so an editor who changes a term everywhere it appears and recomputes the
    commitment produces a file that passes every self-consistency check in the validator.
    """

    from wrasse.policy_hash import PolicyPreimage, policy_hash

    body = json.loads(path.read_text())
    mutate(body)
    for profile in body["buyer"]["profiles"].values():
        if "policy_preimage" not in profile:
            continue  # a refused profile has nothing to sign and nothing to re-hash
        preimage = PolicyPreimage(**profile["policy_preimage"])
        profile["policy_hash"] = policy_hash(preimage)
    path.write_text(json.dumps(body))
    return path


def test_a_forged_price_that_agrees_with_itself_is_still_not_signed(rehearsal, capsys, monkeypatch):
    """The one that matters: a self-consistent edit changes what the wallet funds.

    Every check inside the document passes. The displayed price matches the preimage, the
    preimage hashes to the quoted hash, and the hash is the one the deployed contract would
    compute. Nothing in the file is wrong about the file. It is wrong about where it came
    from, and that is the only question the signer actually needs answered.
    """

    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    ninefold = 9

    def raise_the_baseline(body):
        """Move the baseline, not the settled price.

        Raising the price alone is caught by the document itself now: the settlement is
        recomputed from the published positions and no longer produces the displayed terms.
        The baseline is the one input the document cannot check against itself, because every
        number in the profile is consistent with it. Only rebuilding from the two memories,
        against the operator's own arguments, can tell.
        """
        for profile in body["buyer"]["profiles"].values():
            profile["baseline"]["price_wei"] *= ninefold
            if not profile["settlement"]["agreed"]:
                continue
            profile["terms"]["price_wei"] *= ninefold
            profile["policy_preimage"]["price"] *= ninefold

    _forge(policy_path, raise_the_baseline)

    # It passes validation, which is exactly why validation is not enough.
    from wrasse.policy_document import load_policy

    validated = load_policy(
        policy_path, profile="urgent", chain_id=CHAIN_ID,
        contract_address=rehearsal["address"], buyer=rehearsal["buyer"].address,
        provider=PROVIDER,
    )
    assert validated.price_wei == 10**14 * ninefold

    with pytest.raises(RuntimeError, match="disagree about 'profiles'"):
        main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"])

    ledger = chain.TransactionLedger(Path(os.environ["WRASSE_TX_DB"]))
    assert ledger.rows() == [], "a refused document must not leave a signed transaction"


def test_a_forged_reason_beside_a_genuine_price_is_refused(rehearsal, capsys, monkeypatch):
    """The half a person reads has to be the half the memory holds.

    The money would still follow the committed terms. What would not follow is the
    explanation: a real receipt id beside an invented account of what happened is a false
    reason for a true transaction, which is the claim this entry is built on.
    """

    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")

    def invent_a_reason(body):
        body["buyer"]["recalled_evidence"] = [{
            "event_id": "0x" + "7a" * 32,
            "event_type": "timeout_claimed_without_delivery",
            "chain_id": CHAIN_ID,
        }]
        body["buyer"]["cold_start"] = False
        body["buyer"]["verdict"] = "match"

    _forge(policy_path, invent_a_reason)

    with pytest.raises(RuntimeError, match="disagree about 'cold_start'"):
        main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"])


def test_neither_memory_can_change_between_the_two_recalls(rehearsal, capsys, monkeypatch):
    """Comparing two sequential snapshots cannot establish a common one.

    Reading the buyer, releasing it, then reading the provider catches a receipt that lands in
    the provider in between and misses the same receipt landing in the buyer: both returned
    sets are then the old one, they agree, and the quote proceeds on a history the code
    already knows has reached only one memory. One ordering of a race is not a check.

    So the property is not "we compare afterwards", it is that no writer can be inside either
    memory while the quote reads them. `flock` is per open file description, so a second
    `open` contends exactly as another process would.
    """

    import fcntl

    from wrasse.store import WrasseStore

    observed = {}
    original = WrasseStore.recall

    def watch(self, counterparty):
        if self.identity.role == "buyer":
            observed["between"] = sorted(
                path.name for path in Path(rehearsal["tmp"]).glob("*-memory.db.lock")
                if _can_lock(path)
            )
        return original(self, counterparty)

    def _can_lock(path: Path) -> bool:
        with open(path, "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(handle, fcntl.LOCK_UN)
            return True

    monkeypatch.setattr(WrasseStore, "recall", watch)
    assert main([
        "policy", PROVIDER, "--buyer", rehearsal["buyer"].address, "--accept-window", "600",
        "--output", str(rehearsal["tmp"] / "snapshot.json"),
    ]) == 0
    capsys.readouterr()

    assert observed["between"] == [], (
        "a memory was open to writers while the quote was reading the other one, so the two "
        f"halves could be priced on different histories: {observed['between']}"
    )


def test_a_receipt_arriving_before_the_signature_stops_it(rehearsal, capsys, monkeypatch):
    """Provenance at one instant is not a binding between memory and a signature.

    Between the check and the signing callback this command decrypts a keystore, reads the
    deployment, observes chain time, estimates gas and reads fees and balance. A `reconcile`
    running alongside it can land a newly confirmed receipt in both memories inside that
    window, and the terms would be signed against a history that had already moved. The
    operator would see a successful transaction rather than the refusal they were promised.
    """

    from wrasse.evidence import ChainEvent
    from wrasse.store import WrasseStore

    from wrasse.dimensions import DIMENSION_CATEGORY, DimensionDefinition

    address, buyer = rehearsal["address"], rehearsal["buyer"].address
    reading = DimensionDefinition(
        dimension_id="abandoned_after_accepting",
        source_event_type="timeout_claimed_without_delivery",
        signal_direction="negative",
        severity=0.8,
        confidence=0.9,
        applies_when=("deadline_sensitive",),
    )
    for role, path, owner in (
        ("buyer", os.environ["WRASSE_BUYER_MEMORY_PATH"], buyer),
        ("provider", os.environ["WRASSE_PROVIDER_MEMORY_PATH"], PROVIDER),
    ):
        WrasseStore.open(
            path, role=role, owner_address=owner, chain_id=CHAIN_ID, escrow_address=address,
        ).memory.set_entity(
            DIMENSION_CATEGORY, reading.dimension_id, reading.body(), status="active"
        )

    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    arriving = ChainEvent(
        chain_id=CHAIN_ID,
        contract_address=address,
        tx_hash="0x" + "f1" * 32,
        log_index=0,
        block_number=1,
        event_type="timeout_claimed_without_delivery",
        deal_id=41,
        buyer=buyer,
        provider=PROVIDER,
        observed_at="2026-09-05T00:00:00+00:00",
    )

    # Land it in both memories after the first check has passed, the way a separate
    # reconciliation would. Both stores stay in agreement, so this is not the disagreement
    # refusal: it is a history that legitimately moved.
    landed = []
    original = chain.load_signer

    def deliver_then_load(*args, **kwargs):
        if not landed:
            landed.append(True)
            for role, path, owner in (
                ("buyer", os.environ["WRASSE_BUYER_MEMORY_PATH"], buyer),
                ("provider", os.environ["WRASSE_PROVIDER_MEMORY_PATH"], PROVIDER),
            ):
                WrasseStore.open(
                    path, role=role, owner_address=owner,
                    chain_id=CHAIN_ID, escrow_address=address,
                ).ingest(arriving)
        return original(*args, **kwargs)

    monkeypatch.setattr(chain, "load_signer", deliver_then_load)

    with pytest.raises(RuntimeError, match="memory has moved since"):
        main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"])

    ledger = chain.TransactionLedger(Path(os.environ["WRASSE_TX_DB"]))
    assert ledger.rows() == [], "nothing may be signed once the terms are known to be stale"


@pytest.mark.parametrize(
    "field,edit",
    [
        ("risk", lambda body: body["buyer"]["profiles"]["urgent"]["buyer"].update(
            {"risk": "0.9900"})),
        ("persona name", lambda body: body["provider"]["persona"].update({"name": "someone else"})),
        ("persona commitment", lambda body: body["provider"]["persona"].update(
            {"commitment": "b" * 64})),
        ("an unselected profile", lambda body: (
            body["buyer"]["profiles"]["sensitive"]["terms"].update({"provider_bond_bps": 9_000}),
            body["buyer"]["profiles"]["sensitive"]["policy_preimage"].update({"bond_bps": 9_000}),
        )),
        ("the set of profiles", lambda body: body["buyer"]["profiles"].pop("sensitive")),
    ],
)
def test_the_whole_explanation_is_checked_not_only_the_signed_terms(
    rehearsal, capsys, monkeypatch, field, edit
):
    """The money follows the committed terms; the judge reads everything else.

    A risk score, a persona, or the profile a reader was not shown are not covered by the
    signature, so a validator that checks only the selected economics leaves an invented
    account of the deal beside a genuine transaction. That is the explainability claim
    failing, even though nothing was misdirected.
    """

    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    _forge(policy_path, edit)

    # Either layer may catch it. The document checks what it can check against itself, and
    # what it cannot is caught by rebuilding from memory. What matters is that no edit to the
    # explanation survives to a signature.
    from wrasse.policy_document import PolicyDocumentError

    with pytest.raises(
        (RuntimeError, PolicyDocumentError),
        match="disagree about|may not add, drop or rename|its own settlement produces",
    ):
        main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"])


def test_a_reading_that_changes_before_the_signature_stops_it(rehearsal, capsys, monkeypatch):
    """Memory moving is not only receipts arriving.

    A `learn-dimension --relearn` changes what a receipt is *worth* without changing which
    receipts exist. The recalled evidence, the verdicts and the cold-start flags all stay
    identical, so a recheck that drops the profiles compares everything except the numbers
    that moved, passes, and signs the old bond and window.
    """

    from wrasse.dimensions import DIMENSION_CATEGORY, DimensionDefinition
    from wrasse.evidence import ChainEvent
    from wrasse.store import WrasseStore

    address, buyer = rehearsal["address"], rehearsal["buyer"].address

    def reading(severity: float) -> DimensionDefinition:
        return DimensionDefinition(
            dimension_id="abandoned_after_accepting",
            source_event_type="timeout_claimed_without_delivery",
            signal_direction="negative",
            severity=severity,
            confidence=0.9,
            applies_when=("deadline_sensitive",),
        )

    receipt = ChainEvent(
        chain_id=CHAIN_ID, contract_address=address, tx_hash="0x" + "d1" * 32, log_index=0,
        block_number=1, event_type="timeout_claimed_without_delivery", deal_id=17,
        buyer=buyer, provider=PROVIDER, observed_at="2026-09-05T00:00:00+00:00",
    )

    def store_for(role: str) -> WrasseStore:
        owner = buyer if role == "buyer" else PROVIDER
        path = os.environ[f"WRASSE_{role.upper()}_MEMORY_PATH"]
        return WrasseStore.open(
            path, role=role, owner_address=owner, chain_id=CHAIN_ID, escrow_address=address,
        )

    # The persona is committed before any receipt exists, which is the claim it makes, so it
    # has to be written before this test plants one.
    from wrasse.store import persona_digest

    digest, document = persona_digest(os.environ["WRASSE_PROVIDER_PERSONA"])
    store_for("provider").commit_persona(name=document["name"], digest=digest)

    for role in ("buyer", "provider"):
        store = store_for(role)
        store.ingest(receipt)
        store.memory.set_entity(
            DIMENSION_CATEGORY, "abandoned_after_accepting", reading(0.2).body(), status="active"
        )

    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")

    # The receipts do not move. Only the reading of them does, in the memory that owns the
    # terms this profile commits to.
    relearned = []
    original = chain.load_signer

    def relearn_then_load(*args, **kwargs):
        if not relearned:
            relearned.append(True)
            store_for("buyer").memory.set_entity(
                DIMENSION_CATEGORY, "abandoned_after_accepting", reading(0.9).body(),
                status="active",
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(chain, "load_signer", relearn_then_load)

    with pytest.raises(RuntimeError, match="disagree about 'profiles'"):
        main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"])

    ledger = chain.TransactionLedger(Path(os.environ["WRASSE_TX_DB"]))
    assert ledger.rows() == [], "terms nobody can reproduce must not reach a signature"


def test_both_memories_are_held_until_the_bytes_exist(rehearsal, capsys, monkeypatch):
    """Checking provenance and then releasing is still a snapshot.

    A reconciliation waiting on the lock ingests the moment it is freed and finishes while
    this observes chain time, asks the contract for its own hash and builds the transaction.
    "Immediately before signing" has to mean the memories cannot move in between.
    """

    import fcntl

    policy_path = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")

    observed = {}
    original = chain.sign_transaction

    def watch(*args, **kwargs):
        observed["open"] = sorted(
            path.name for path in Path(rehearsal["tmp"]).glob("*-memory.db.lock")
            if _unlocked(path)
        )
        return original(*args, **kwargs)

    def _unlocked(path: Path) -> bool:
        with open(path, "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(handle, fcntl.LOCK_UN)
            return True

    monkeypatch.setattr(chain, "sign_transaction", watch)
    assert main(["create-deal", "--policy", str(policy_path), "--profile", "urgent"]) == 0
    capsys.readouterr()

    assert observed["open"] == [], (
        f"a memory could be written while the transaction was being signed: {observed['open']}"
    )


def test_a_fixture_relabelled_as_a_live_quote_is_refused(rehearsal, capsys, monkeypatch):
    """`executability` is the label a reader trusts, and memory cannot rebuild it.

    It records what a past run saw, so nothing in the two stores can confirm or deny it. A
    document that says it was judged against the latest Base block is therefore checked
    against the thing it describes: a block number and timestamp the chain does not agree with
    never came from the chain.
    """

    output = rehearsal["tmp"] / "fixture.json"
    assert main([
        "policy", PROVIDER, "--buyer", rehearsal["buyer"].address,
        "--accept-by", "1900000000", "--reference-timestamp", "1899990000",
        "--output", str(output),
    ]) == 0
    capsys.readouterr()
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")

    body = json.loads(output.read_text())
    assert body["executability"]["basis"] == "supplied-reference"

    from wrasse.policy_document import _NOTES

    live_note = next(note for note in _NOTES if "latest observed Base block" in note)
    body["executability"] = {
        "basis": "chain-observation",
        "reference_timestamp": body["executability"]["reference_timestamp"],
        "chain": {"chain_id": CHAIN_ID, "block_number": 1, "block_timestamp": 1_899_990_000},
        "inclusion_margin_seconds": 45,
        "observed_lag_seconds": 0,
        "executable": True,
        "note": live_note,
    }
    output.write_text(json.dumps(body))

    with pytest.raises(RuntimeError, match="did not happen|cannot be read"):
        main(["create-deal", "--policy", str(output), "--profile", "urgent"])


def test_an_executability_note_this_build_never_wrote_is_refused(rehearsal, capsys):
    """The sentence a reader is given is a closed set, not free text."""

    from wrasse.policy_document import PolicyDocumentError, load_policy

    output = _quote(rehearsal, capsys)
    body = json.loads(output.read_text())
    body["executability"]["note"] = "LIVE-CHECKED ON BASE"
    output.write_text(json.dumps(body))

    with pytest.raises(PolicyDocumentError, match="not one this build writes"):
        load_policy(
            output, profile="urgent", chain_id=CHAIN_ID,
            contract_address=rehearsal["address"], buyer=rehearsal["buyer"].address,
            provider=PROVIDER,
        )


def test_a_genuine_but_old_block_is_not_the_latest_observed_one(rehearsal, capsys, monkeypatch):
    """Any real block satisfies "this block exists". Recency is what the label claims.

    An editor with an RPC can read a historical block and write a self-consistent quote around
    it, so checking that the coordinates are genuine establishes almost nothing on its own.

    The age bound is narrowed here rather than the chain being aged, because the chain is
    shared with every other test in this module and moving its clock is not this test's to do.
    """

    import wrasse.cli as cli
    from wrasse.policy_document import _NOTES

    web3 = rehearsal["web3"]
    earlier = web3.eth.get_block(1)
    web3.provider.make_request("evm_mine", [])
    assert int(web3.eth.get_block("latest")["timestamp"]) > int(earlier["timestamp"])

    output = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    monkeypatch.setattr(cli, "MAX_OBSERVATION_AGE_SECONDS", 0)

    body = json.loads(output.read_text())
    body["executability"] = {
        "basis": "chain-observation",
        "reference_timestamp": int(earlier["timestamp"]),
        "chain": {
            "chain_id": CHAIN_ID,
            "block_number": int(earlier["number"]),
            "block_timestamp": int(earlier["timestamp"]),
        },
        "inclusion_margin_seconds": 45,
        "observed_lag_seconds": 0,
        "executable": True,
        "note": next(note for note in _NOTES if "latest observed Base block" in note),
    }
    output.write_text(json.dumps(body))

    with pytest.raises(RuntimeError, match="not the latest observed block"):
        main(["create-deal", "--policy", str(output), "--profile", "urgent"])


def test_a_live_quote_must_reason_from_the_block_it_observed(rehearsal, capsys, monkeypatch):
    """Two fields, one number. A writer that observed a block reasons from that block."""

    output = _quote(rehearsal, capsys)
    monkeypatch.setenv(chain.BROADCAST_ENV, "1")

    body = json.loads(output.read_text())
    assert body["executability"]["basis"] == "chain-observation"
    body["executability"]["reference_timestamp"] += 900
    output.write_text(json.dumps(body))

    with pytest.raises(RuntimeError, match="takes its reference from the block it observed"):
        main(["create-deal", "--policy", str(output), "--profile", "urgent"])


def test_a_document_that_says_the_same_thing_twice_is_refused(rehearsal, capsys):
    """`json.loads` keeps the last of a repeated key and says nothing about it.

    A file carrying both a small price and the real one parses to whichever this build keeps,
    while a reader or a first-wins parser sees the other. Canonical encoding cannot see it:
    by the time anything is compared, the ambiguity is already resolved and thrown away.
    """

    from wrasse.policy_document import PolicyDocumentError, load_policy

    output = _quote(rehearsal, capsys)
    raw = output.read_text()
    doubled = raw.replace('"price_wei":', '"price_wei": 1, "price_wei":', 1)
    assert doubled != raw
    output.write_text(doubled)

    with pytest.raises(PolicyDocumentError, match="twice in one object"):
        load_policy(
            output, profile="urgent", chain_id=CHAIN_ID,
            contract_address=rehearsal["address"], buyer=rehearsal["buyer"].address,
            provider=PROVIDER,
        )


def test_a_refused_opt_in_costs_the_wallet_nothing(both_roles, capsys, monkeypatch):
    """The failure that made a dead wallet likely rather than theoretical, end to end.

    `broadcast` checks the opt-in as its first statement, and by then the caller has signed,
    taken a nonce and written a row. Only `DeterministicRejection` was caught, so one command
    run without the flag left a nonce held for bytes no node ever saw. The next `tx-resolve`
    found no receipt, hit the deadline gate, and that wallet was finished.

    A judge closing a tab or a service restarting mid-action is the same shape. The property
    that matters is not the exception, which was always raised. It is that the next attempt
    still works: a phantom row holding the nonce would make this raise `WalletBusy` instead.
    """

    deal_id = _open_deal(both_roles, capsys)

    monkeypatch.delenv(chain.BROADCAST_ENV, raising=False)
    with pytest.raises(chain.BroadcastNotAuthorised):
        main(["accept-deal", "--deal-id", str(deal_id)])
    capsys.readouterr()

    ledger = chain.TransactionLedger(os.environ["WRASSE_TX_DB"])
    provider = Web3.to_checksum_address(os.environ["WRASSE_PROVIDER_A_ADDRESS"])
    held = [row for row in ledger.rows() if row.wallet == provider]
    assert held == [], (
        "nothing was sent, so nothing may be recorded; a row here is a nonce nobody can free"
    )

    monkeypatch.setenv(chain.BROADCAST_ENV, "1")
    assert main(["accept-deal", "--deal-id", str(deal_id)]) == 0
    sent = json.loads(capsys.readouterr().out)
    both_roles["web3"].eth.wait_for_transaction_receipt(sent["tx_hash"])
    assert main(["tx-resolve"]) == 0
    capsys.readouterr()
    assert escrow_state(both_roles["web3"], both_roles["address"], deal_id) == "Accepted"

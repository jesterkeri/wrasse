"""The hosted quote service, held to the one rule that makes it cheap and safe.

The service holds no keys, signs nothing and writes nothing. Every test here checks that rule
from a different direction, because it is the constraint that deletes the queue, the worker,
the per-session copies and the wallet-death problem all at once. If it ever stops holding, the
right response is to find where the read and write paths were joined, not to add a lock.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web3 import Web3

BUYER = Web3.to_checksum_address("0x30C95B7eb3E08F83992E803Be2A5AB0E0af93d22")
PROVIDER = Web3.to_checksum_address("0x0b920573ADf657f45Fecd9f7e48e66B5535A90C0")
ESCROW = Web3.to_checksum_address("0x5525653f05990DA1479578893b5a624183AFa22E")
LIVE = Path(__file__).resolve().parent.parent / ".wrasse"


@pytest.fixture
def service(tmp_path, monkeypatch):
    """The service as it is deployed: two memory databases, four values, and nothing else.

    Deliberately no `WRASSE_KEYSTORE`, no `WRASSE_KEYSTORE_PASSWORD_FILE` and no
    `WRASSE_TX_DB`. If any of those becomes necessary the read path has grown a write, which is
    the thing this fixture exists to catch. Every one of them is removed rather than merely
    left unset, so a value leaking in from a developer's shell cannot make the suite pass.
    """

    for secret in (
        "WRASSE_KEYSTORE", "WRASSE_PROVIDER_A_KEYSTORE",
        "WRASSE_KEYSTORE_PASSWORD_FILE", "WRASSE_TX_DB", "WRASSE_ALLOW_BROADCAST",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(secret, raising=False)

    warm = tmp_path / "warm"
    warm.mkdir()
    for role in ("buyer", "provider"):
        shutil.copy(LIVE / f"{role}-memory.db", warm / f"{role}-memory.db")

    monkeypatch.setenv("WRASSE_BUYER_ADDRESS", BUYER)
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", PROVIDER)
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", "84532")
    # The committed persona, not the suite's stand-in. These stores are copies of the live
    # ones, so their provider is the real address and the persona has to be the file whose
    # hash they already committed to. A mismatch is refused, correctly, and that refusal is
    # what this line exists to avoid rather than to suppress.
    monkeypatch.setenv(
        "WRASSE_PROVIDER_PERSONA", str(LIVE.parent / "personas" / "provider-a.json")
    )
    monkeypatch.setenv("BASE_SEPOLIA_RPC_URL", "http://127.0.0.1:1/never-reached")

    from wrasse import service as module

    monkeypatch.setattr(module, "_stores", {})
    monkeypatch.setitem(module._PATHS, module.WARM, {
        "buyer": warm / "buyer-memory.db", "provider": warm / "provider-memory.db",
    })
    monkeypatch.setitem(module._PATHS, module.COLD, {
        "buyer": tmp_path / "cold" / "buyer-memory.db",
        "provider": tmp_path / "cold" / "provider-memory.db",
    })
    return TestClient(module.app), warm


def test_it_answers_with_no_keystore_no_password_and_no_ledger(service):
    """The acceptance check that is the whole architecture.

    A quote needs two memory databases and four public values. It does not need a key, because
    nothing on this path signs, and it does not need the transaction ledger, because the ledger
    is settlement state and settlement is not hosted.
    """

    client, _ = service
    assert "WRASSE_KEYSTORE" not in os.environ
    assert "WRASSE_TX_DB" not in os.environ

    response = client.get("/api/quote", params={"memory": "on"})
    assert response.status_code == 200
    assert response.json()["buyer"]["profiles"]["urgent"]["terms"]["provider_bond_bps"] == 2_480


def test_a_quote_makes_no_network_call_at_all(service):
    """The RPC URL points at a port nothing listens on, and the quote still answers.

    A quote is memory plus arithmetic. The only chain read on this path is the time
    observation, and supplying both times returns before a provider is constructed. If this
    ever hangs or fails, something started reaching for the chain.
    """

    client, _ = service
    body = client.get("/api/quote", params={"memory": "on"}).json()

    assert body["executability"]["executable"] is False
    assert body["executability"]["basis"] == "supplied-reference"
    assert body["executability"]["chain"] is None


def test_memory_off_and_on_are_the_same_engine_and_different_answers(service):
    """The page's central interaction, and the entry's whole argument in one comparison."""

    client, _ = service
    warm = client.get("/api/quote", params={"memory": "on"}).json()
    cold = client.get("/api/quote", params={"memory": "off"}).json()

    assert warm["engine_version"] == cold["engine_version"]
    assert warm["memory"] == "on" and cold["memory"] == "off"

    assert cold["buyer"]["cold_start"] is True
    assert warm["buyer"]["cold_start"] is False

    assert cold["buyer"]["profiles"]["urgent"]["terms"]["provider_bond_bps"] == 500
    assert warm["buyer"]["profiles"]["urgent"]["terms"]["provider_bond_bps"] == 2_480

    assert cold["buyer"]["profiles"]["budget"]["settlement"]["agreed"] is True
    assert warm["buyer"]["profiles"]["budget"]["settlement"] == {
        "agreed": False, "failed_on": "price_bps", "gap": 850,
    }


def test_the_service_writes_nothing_to_the_memory_it_reads(service):
    """Checked by hashing the files, not by reading the code.

    The persona commitment is the one write on the quote path, and it is idempotent after the
    first open. A store that already holds it takes the verify branch. So a served quote must
    leave both databases byte-identical, which is what makes a read-only mount viable.
    """

    import hashlib

    client, warm = service
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(warm.glob("*.db"))
    }

    for _ in range(3):
        assert client.get("/api/quote", params={"memory": "on"}).status_code == 200

    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(warm.glob("*.db"))
    }
    assert before == after, "a quote changed the memory it was quoting from"


def test_two_requests_at_once_both_succeed_with_no_queue(service):
    """Two readers, which the stores already permit. If this ever needs a lock, a write crept in."""

    from concurrent.futures import ThreadPoolExecutor

    client, _ = service
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(client.get, "/api/quote", params={"memory": memory})
            for memory in ("on", "off")
        ]
        codes = [future.result().status_code for future in results]
    assert codes == [200, 200]


def test_the_document_is_returned_as_produced(service):
    """No field renamed, none recomputed, and the manifest still recomputes to the version.

    A reshaping layer is one more place the displayed terms can drift from the produced ones.
    The single addition is `memory`, which names which pair of stores answered.
    """

    import hashlib

    client, _ = service
    body = client.get("/api/quote", params={"memory": "on"}).json()

    assert set(body) == {
        "schema_version", "request_id", "chain_id", "contract_address", "engine_version",
        "engine", "executability", "buyer", "provider", "memory",
    }
    published = body["engine"]["negotiation_manifest"]
    digest = hashlib.sha256(
        json.dumps(published, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert body["engine_version"].endswith(digest[:12])


def test_a_baseline_outside_the_domain_is_refused_with_its_bound(service):
    """The page prints the bound under the field, so the bound is returned rather than described."""

    client, _ = service
    response = client.get("/api/quote", params={"memory": "on", "service_window": 0})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "service_window" in detail["error"]
    assert detail["baseline_bounds"]["service_window"][0] == 1


def test_health_says_plainly_what_it_does_not_do(service):
    client, _ = service
    body = client.get("/api/health").json()
    assert body["signs"] is False and body["writes"] is False
    assert body["chain_id"] == 84532


def test_a_read_only_source_is_copied_before_anything_opens_it(tmp_path, monkeypatch):
    """The acceptance check as it can actually pass, and why it changed shape.

    The intent was to mount both databases read-only so the operating system enforced the
    guarantee rather than the code path alone. That is not possible: the Sibyl SDK opens SQLite
    read-write and sets WAL, and the store's own lock file is created with `open(path, "w")`.
    A genuinely read-only file does not open at all.

    So the source is read-only and the service copies it into a writable working directory at
    startup. The deployed artifact cannot be mutated by anything, the working copy is recreated
    on every restart, and the hash test above shows nothing writes to it anyway.
    """

    for secret in ("WRASSE_KEYSTORE", "WRASSE_KEYSTORE_PASSWORD_FILE", "WRASSE_TX_DB"):
        monkeypatch.delenv(secret, raising=False)

    source = tmp_path / "source"
    source.mkdir()
    for role in ("buyer", "provider"):
        shutil.copy(LIVE / f"{role}-memory.db", source / f"{role}-memory.db")
        (source / f"{role}-memory.db").chmod(0o444)

    working = tmp_path / "working"
    monkeypatch.setenv("WRASSE_BUYER_ADDRESS", BUYER)
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", PROVIDER)
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", "84532")
    monkeypatch.setenv(
        "WRASSE_PROVIDER_PERSONA", str(LIVE.parent / "personas" / "provider-a.json")
    )

    from wrasse import service as module

    monkeypatch.setattr(module, "_stores", {})
    monkeypatch.setattr(module, "SOURCE_DIR", str(source))
    monkeypatch.setitem(module._PATHS, module.WARM, {
        "buyer": working / "buyer-memory.db", "provider": working / "provider-memory.db",
    })
    monkeypatch.setitem(module._PATHS, module.COLD, {
        "buyer": tmp_path / "cold" / "buyer-memory.db",
        "provider": tmp_path / "cold" / "provider-memory.db",
    })

    body = TestClient(module.app).get("/api/quote", params={"memory": "on"}).json()
    assert body["buyer"]["profiles"]["urgent"]["terms"]["provider_bond_bps"] == 2_480

    # the source is untouched, which is the property the mount is for
    assert all(path.stat().st_mode & 0o200 == 0 for path in source.glob("*.db"))


def test_a_configured_source_that_is_missing_fails_loudly(tmp_path, monkeypatch):
    """The failure that would otherwise be invisible.

    A missing mount would serve `memory=on` from an empty store, and the page would show a
    system that remembers nothing rather than a broken deployment. Those look identical from
    the outside and only one of them is recoverable by restarting.
    """

    monkeypatch.setenv("WRASSE_BUYER_ADDRESS", BUYER)
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", PROVIDER)
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", "84532")

    from wrasse import service as module

    monkeypatch.setattr(module, "_stores", {})
    monkeypatch.setattr(module, "SOURCE_DIR", str(tmp_path / "nothing-here"))
    monkeypatch.setitem(module._PATHS, module.WARM, {
        "buyer": tmp_path / "w" / "buyer-memory.db",
        "provider": tmp_path / "w" / "provider-memory.db",
    })

    with pytest.raises(RuntimeError, match="is missing"):
        module.prepare_working_copies()

"""The hosted quote service, held to the one rule that makes it cheap and safe.

The service holds no keys, signs nothing and writes nothing. Every test here checks that rule
from a different direction, because it is the constraint that deletes the queue, the worker,
the per-session copies and the wallet-death problem all at once. If it ever stops holding, the
right response is to find where the read and write paths were joined, not to add a lock.

**The stores are built here, not copied from `.wrasse/`.** The first version of this file
copied the live memories, which are gitignored, so it passed on one laptop and failed in CI
with a missing file. A test that depends on untracked state proves nothing to anyone cloning
the repository, which is every judge. These stores are small and synthetic on purpose: what is
under test is the service, and the exact live numbers are pinned by `test_page.py` against the
two tracked documents.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web3 import Web3

from wrasse.dimensions import DIMENSION_CATEGORY, DimensionDefinition
from wrasse.evidence import ChainEvent
from wrasse.store import WrasseStore, persona_digest

BUYER = Web3.to_checksum_address("0x30C95B7eb3E08F83992E803Be2A5AB0E0af93d22")
PROVIDER = Web3.to_checksum_address("0x0b920573ADf657f45Fecd9f7e48e66B5535A90C0")
ESCROW = Web3.to_checksum_address("0x5525653f05990DA1479578893b5a624183AFa22E")
REPO = Path(__file__).resolve().parent.parent

#: One receipt and one reading of it. Enough for a warm quote to differ from a cold one, which
#: is the only property these tests need from the memories themselves.
TIMEOUT = ChainEvent(
    chain_id=84532, contract_address=ESCROW, tx_hash="0x" + "b1" * 32, log_index=1,
    block_number=46_390_048, event_type="timeout_claimed_without_delivery", deal_id=1,
    buyer=BUYER, provider=PROVIDER, observed_at=datetime(2026, 9, 4, tzinfo=UTC).isoformat(),
)
READING = DimensionDefinition(
    dimension_id="non_delivery_after_payment",
    source_event_type="timeout_claimed_without_delivery",
    signal_direction="negative", severity=0.9, confidence=0.9,
    applies_when=("deadline_sensitive", "cost_sensitive", "quality_sensitive"),
)


def _warm_pair(directory: Path) -> None:
    """Two identified memories that have both seen the same receipt.

    The persona is committed *before* the receipt is ingested, and the order is not incidental.
    `commit_persona` refuses once a store holds evidence, because a persona chosen after the
    fact could have been chosen with that evidence in view, and the claim being made is that it
    was fixed first. Building this fixture the other way round is refused, correctly, and that
    refusal is how the constraint was rediscovered here.
    """

    digest, persona = persona_digest(REPO / "personas" / "provider-a.json")
    directory.mkdir(parents=True, exist_ok=True)
    for role, owner in (("buyer", BUYER), ("provider", PROVIDER)):
        store = WrasseStore.open(
            directory / f"{role}-memory.db", role=role, owner_address=owner,
            chain_id=84532, escrow_address=ESCROW,
        )
        if role == "provider":
            store.commit_persona(name=persona["name"], digest=digest)
        store.memory.set_entity(
            DIMENSION_CATEGORY, READING.dimension_id, READING.body(), status="active"
        )
        store.ingest(TIMEOUT)


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
    _warm_pair(warm)

    monkeypatch.setenv("WRASSE_BUYER_ADDRESS", BUYER)
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", PROVIDER)
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", "84532")
    # The committed persona, not the suite's stand-in. These stores are copies of the live
    # ones, so their provider is the real address and the persona has to be the file whose
    # hash they already committed to. A mismatch is refused, correctly, and that refusal is
    # what this line exists to avoid rather than to suppress.
    monkeypatch.setenv(
        "WRASSE_PROVIDER_PERSONA", str(REPO / "personas" / "provider-a.json")
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
    body = response.json()
    # The exact live numbers are pinned in `test_page.py` against the tracked documents. What
    # matters here is that a quote came out of the memories at all, without a key in sight.
    assert len(body["memories"]["buyer"]["receipts"]) == 1
    assert body["profiles"][0]["terms"]["provider_bond_bps"] > 500, "memory moved the bond"


def test_a_quote_makes_no_network_call_at_all(service):
    """The RPC URL points at a port nothing listens on, and the quote still answers.

    A quote is memory plus arithmetic. The only chain read on this path is the time
    observation, and supplying both times returns before a provider is constructed. If this
    ever hangs or fails, something started reaching for the chain.
    """

    client, _ = service
    body = client.get("/api/quote", params={"memory": "on"}).json()

    assert body["document"]["executability"]["executable"] is False
    assert body["document"]["executability"]["basis"] == "supplied-reference"
    assert body["document"]["executability"]["chain"] is None


def test_memory_off_and_on_are_the_same_engine_and_different_answers(service):
    """The page's central interaction, and the entry's whole argument in one comparison."""

    client, _ = service
    warm = client.get("/api/quote", params={"memory": "on"}).json()
    cold = client.get("/api/quote", params={"memory": "off"}).json()

    assert warm["engine_version"] == cold["engine_version"]
    assert warm["memory"] is True and cold["memory"] is False

    assert cold["memories"]["buyer"]["cold_start"] is True
    assert warm["memories"]["buyer"]["cold_start"] is False

    # The exact live numbers belong to `test_page.py`, against the two tracked documents. What
    # is under test here is that the two settings reach different stores at all.
    cold_bond = cold["document"]["buyer"]["profiles"]["urgent"]["terms"]["provider_bond_bps"]
    warm_bond = warm["document"]["buyer"]["profiles"]["urgent"]["terms"]["provider_bond_bps"]
    assert cold_bond == 500, "with nothing remembered, the operator's baseline stands"
    assert warm_bond > cold_bond, "and a remembered timeout raises the bond demanded"


def test_the_service_writes_nothing_to_the_memory_it_reads(service):
    """Checked by hashing the files, not by reading the code.

    The persona commitment is the one write on the quote path, and it is idempotent after the
    first open. A store that already holds it takes the verify branch. So a served quote must
    leave both databases byte-identical, which is what makes a read-only mount viable.
    """

    import hashlib

    client, warm = service

    def fingerprint():
        # Every file, not just the main database. SQLite writes through a write-ahead log, so
        # hashing `*.db` alone compares two files whose real content is still in `-wal` beside
        # them, and two different stores come out identical.
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(warm.glob("*memory.db*"))
        }

    # One request first, then the baseline. A freshly written store settles on its first open,
    # checkpointing its log into the main file, and that is the client library's housekeeping
    # rather than this service writing. The property under test is that a served quote leaves
    # the memory alone in steady state; baselining before the first open measures the library.
    assert client.get("/api/quote", params={"memory": "on"}).status_code == 200
    before = fingerprint()

    for _ in range(3):
        assert client.get("/api/quote", params={"memory": "on"}).status_code == 200

    after = fingerprint()
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
        "engine_version", "chain_id", "contract_address", "memory", "baseline",
        "limit_kinds", "limit_names", "persona", "memories", "profiles", "document",
    }
    # The document rides along untouched, so the projection can be checked against it here
    # rather than in a second request that might answer differently.
    assert set(body["document"]) == {
        "schema_version", "request_id", "chain_id", "contract_address", "engine_version",
        "engine", "executability", "buyer", "provider",
    }
    published = body["document"]["engine"]["negotiation_manifest"]
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

    built = tmp_path / "built"
    _warm_pair(built)
    source = tmp_path / "source"
    source.mkdir()
    for path in sorted(built.glob("*memory.db*")):
        # The whole store, sidecars included. A mount that carried only the main file is
        # exactly the failure the service now guards against, and copying that way here would
        # test the guard instead of the mount.
        copied = source / path.name
        shutil.copy(path, copied)
        copied.chmod(0o444)

    working = tmp_path / "working"
    monkeypatch.setenv("WRASSE_BUYER_ADDRESS", BUYER)
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", PROVIDER)
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", "84532")
    monkeypatch.setenv(
        "WRASSE_PROVIDER_PERSONA", str(REPO / "personas" / "provider-a.json")
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

    response = TestClient(module.app).get("/api/quote", params={"memory": "on"})
    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(body["memories"]["buyer"]["receipts"]) == 1, (
        "a read-only source still produced a quote from the memory it was copied from"
    )

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


def test_a_source_copied_without_its_write_ahead_log_does_not_serve_an_empty_memory(
    tmp_path, monkeypatch
):
    """The realistic mount mistake, and the one that looks like success.

    The service cannot recover rows a source never carried, so the contract is that it serves
    the memory or refuses, and never serves an empty one as though it were real.

    SQLite writes through a write-ahead log. A source whose log has not been checkpointed keeps
    its most recent rows in `<name>-wal` beside the database, and copying the `.db` alone loses
    them without error. The service then answers `memory=on` from what appears to be an empty
    store, which is indistinguishable from a system that remembers nothing.

    Found by CI, not here. The tests were copying the live memories, which are gitignored, so
    they passed on one laptop and failed on a fresh clone. Rebuilding them on stores they
    construct themselves surfaced this immediately, because a freshly built store has
    everything in its log and nothing checkpointed.
    """

    built = tmp_path / "built"
    _warm_pair(built)
    assert (built / "buyer-memory.db-wal").is_file(), (
        "this test is only meaningful while the fixture leaves rows in the log"
    )

    source = tmp_path / "source"
    source.mkdir()
    for role in ("buyer", "provider"):
        shutil.copy(built / f"{role}-memory.db", source / f"{role}-memory.db")

    monkeypatch.setenv("WRASSE_BUYER_ADDRESS", BUYER)
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", PROVIDER)
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", "84532")
    monkeypatch.setenv(
        "WRASSE_PROVIDER_PERSONA", str(REPO / "personas" / "provider-a.json")
    )

    from wrasse import service as module

    working = tmp_path / "working"
    monkeypatch.setattr(module, "_stores", {})
    monkeypatch.setattr(module, "SOURCE_DIR", str(source))
    monkeypatch.setitem(module._PATHS, module.WARM, {
        "buyer": working / "buyer-memory.db", "provider": working / "provider-memory.db",
    })
    monkeypatch.setitem(module._PATHS, module.COLD, {
        "buyer": tmp_path / "cold" / "buyer-memory.db",
        "provider": tmp_path / "cold" / "provider-memory.db",
    })

    with pytest.raises(RuntimeError, match="write-ahead log"):
        module.prepare_working_copies()

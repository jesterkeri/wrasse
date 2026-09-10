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
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web3 import Web3

from wrasse import service as service_module
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
    """Two quotes at once on a cold process, which is not two readers.

    This used to say that if it ever needed a lock, a write had crept in. The write was there
    all along and the docstring was wrong: the first open copies the source into the working
    paths, writes an identity record and commits a persona. Two requests arriving together both
    passed the check that decides to do that and both started writing, and SQLite answered the
    way it always does, with "database is locked".

    It surfaced as a CI failure on one branch and a pass on another from the same commit, which
    is what a race looks like from the outside. Serving is read-only; opening is not.
    """

    from concurrent.futures import ThreadPoolExecutor

    client, _ = service
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(client.get, "/api/quote", params={"memory": memory})
            for memory in ("on", "off")
        ]
        codes = [future.result().status_code for future in results]
    assert codes == [200, 200]

    # One pair per memory, not one per request. Two objects on one file would each hold their
    # own `fcntl` description, and those exclude each other rather than the caller.
    assert sorted(service_module._stores) == [service_module.COLD, service_module.WARM]


def test_the_first_open_does_its_writing_under_a_lock(service, monkeypatch):
    """Asserted directly, because two threads cannot prove it on a machine this fast.

    The race needs the loser to be slow enough to arrive during the winner's writes, which a
    CI runner managed and this laptop does not. Timing it here would pass whether the lock
    existed or not, which is the same as not testing it. What the lock has to guarantee is
    that the copy, the identity record and the persona commit happen with it held, so that is
    what this checks.
    """

    client, _ = service
    service_module._stores.clear()
    held: list[bool] = []

    real = service_module.prepare_working_copies
    monkeypatch.setattr(
        service_module, "prepare_working_copies",
        lambda: (held.append(service_module._open_lock.locked()), real())[1],
    )

    assert client.get("/api/quote", params={"memory": "on"}).status_code == 200

    assert held == [True], "the first open wrote without holding the lock that serialises it"


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
    # These are now properties of the deployment rather than of the code. This fixture sets no
    # execution variable, so this is the read-only deployment and all three agree.
    assert body["signs"] is False and body["holds_keys"] is False
    assert body["execution_enabled"] is False
    # Not `writes: false`. That was untrue on a cold deployment: opening a store that does not
    # exist writes its identity record, and the first quote writes the persona commitment when
    # it is absent. Both now happen at startup, and the claim says what is actually kept.
    #
    # Renamed to name the surface it is true of. Execution writes an outcome by definition, so
    # an unqualified claim would have become false the moment a deployment enabled it.
    assert body["quote_writes_receipts_or_outcomes"] is False
    assert "writes" not in body, "the claim that was too broad must not come back"
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


def test_a_cold_deployment_writes_its_metadata_before_it_serves(tmp_path, monkeypatch):
    """The claim the health endpoint makes, checked where it used to be false.

    A store that does not exist gets its identity record written on first open, and the first
    quote writes the persona commitment when it is absent. So a fresh deployment's first
    request wrote files while the module said "writes nothing". Both now happen at startup,
    which makes the read-only property true of every request rather than of every request
    after the first.
    """

    import hashlib

    for secret in ("WRASSE_KEYSTORE", "WRASSE_KEYSTORE_PASSWORD_FILE", "WRASSE_TX_DB"):
        monkeypatch.delenv(secret, raising=False)

    warm = tmp_path / "warm"
    _warm_pair(warm)
    cold = tmp_path / "cold"

    monkeypatch.setenv("WRASSE_BUYER_ADDRESS", BUYER)
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", PROVIDER)
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", "84532")
    monkeypatch.setenv(
        "WRASSE_PROVIDER_PERSONA", str(REPO / "personas" / "provider-a.json")
    )

    from wrasse import service as module

    monkeypatch.setattr(module, "_stores", {})
    monkeypatch.setitem(module._PATHS, module.WARM, {
        "buyer": warm / "buyer-memory.db", "provider": warm / "provider-memory.db",
    })
    monkeypatch.setitem(module._PATHS, module.COLD, {
        "buyer": cold / "buyer-memory.db", "provider": cold / "provider-memory.db",
    })

    client = TestClient(module.app)
    assert client.get("/api/quote", params={"memory": "off"}).status_code == 200

    def fingerprint(directory):
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.glob("*memory.db*"))
        }

    before = fingerprint(cold)
    assert before, "the cold pair exists after the first request"
    for _ in range(3):
        assert client.get("/api/quote", params={"memory": "off"}).status_code == 200
    assert fingerprint(cold) == before, "a cold quote wrote to the store it quoted from"


# ------------------------------------------------------------------------------------------
# The executing surface. Everything above holds for a deployment that signs nothing; these
# hold for the one that does, and the first of them is the boundary between the two.
# ------------------------------------------------------------------------------------------


def test_a_read_only_deployment_refuses_to_execute_and_says_why(service):
    """The routes exist in both deployments and only one of them will act.

    Returning 404 would be the easy alternative and it would be a worse answer: a judge given
    a link to the read-only deployment would conclude the feature does not exist rather than
    that this particular host does not hold keys.
    """

    client, _ = service
    for method, path, body in (
        ("post", "/api/session", None),
        ("post", "/api/execute", {"session_id": "x", "profile": "urgent"}),
        ("get", "/api/run/anything", None),
    ):
        response = getattr(client, method)(path, **({"json": body} if body else {}))
        assert response.status_code == 503
        assert "holds no keys" in response.json()["detail"]


@pytest.fixture
def executing(service, tmp_path, monkeypatch):
    """The same service with execution enabled and a runner that touches nothing.

    The runner is replaced rather than the command, because what these tests are about is the
    HTTP surface: which requests are admitted, what they are charged against, and what they
    return. The procedure itself is covered against its own fakes in `test_executor`.
    """

    from wrasse import executor, service as module
    from wrasse.sessions import Sessions

    client, warm = service
    monkeypatch.setattr(module, "EXECUTION", True)
    monkeypatch.setattr(module, "_STARTED", 0)
    monkeypatch.setattr(module, "_session_stores", {})

    executed: list[executor.Run] = []

    class Recording(executor.Runner):
        def __init__(self) -> None:
            pass

        def execute(self, run: executor.Run) -> None:
            executed.append(run)
            run.status = executor.SUCCEEDED

    # With the completion hook the production queue carries. Without it the fixture would be
    # testing a worker that cannot do the one thing the review moved into it.
    queue = executor.Queue(Recording(), after=module._run_finished)
    monkeypatch.setattr(module, "_QUEUE", queue)
    monkeypatch.setattr(
        module, "_SESSIONS",
        Sessions(module._PATHS[module.WARM], root=tmp_path / "sessions", limit=10),
    )
    yield client, executed, queue
    queue.stop()


def test_a_session_gets_its_own_copy_of_both_memories(executing):
    """And the copy is a copy: the source is not what the run will be pointed at."""

    client, _, _ = executing
    from wrasse import service as module

    body = client.post("/api/session").json()
    session = module._SESSIONS.get(body["session_id"])
    assert session is not None
    for role in ("buyer", "provider"):
        assert session.paths[role].is_file()
        assert session.paths[role] != module._PATHS[module.WARM][role]


def test_a_run_is_queued_against_the_session_that_asked_for_it(executing):
    """The run carries that session's paths, which is what every subprocess will inherit."""

    client, executed, _ = executing
    session_id = client.post("/api/session").json()["session_id"]
    body = client.post(
        "/api/execute", json={"session_id": session_id, "profile": "urgent"}
    ).json()

    deadline = time.time() + 5
    while not executed and time.time() < deadline:
        time.sleep(0.01)

    assert len(executed) == 1
    assert executed[0].session_id == session_id
    assert executed[0].paths["buyer"].parent.name == session_id
    assert client.get(f"/api/run/{body['run_id']}").json()["status"] == "succeeded"


def test_a_session_cannot_run_past_its_allowance(executing):
    """Both wallets are shared and faucet-funded, so this is what stops one visitor spending
    the demo. The refusal is a 429 rather than a 403 because it is a rate, not a permission."""

    from wrasse import sessions as session_module

    client, _, _ = executing
    session_id = client.post("/api/session").json()["session_id"]
    request = {"session_id": session_id, "profile": "urgent"}
    for _ in range(session_module.RUNS_PER_SESSION):
        assert client.post("/api/execute", json=request).status_code == 200
    refused = client.post("/api/execute", json=request)
    assert refused.status_code == 429
    assert "limit" in refused.json()["detail"]


def test_a_refused_run_is_not_charged_against_the_deployment_ceiling(executing):
    """A run that was never queued must not consume the global allowance.

    The per-session check happens after the global counter is taken, because the counter has
    to be taken under a lock and the session check can raise. Without the decrement, a visitor
    hammering a spent session would burn the whole deployment's budget without ever running
    anything, which is a denial of service with no transactions in it.
    """

    from wrasse import service as module, sessions as session_module

    client, _, _ = executing
    session_id = client.post("/api/session").json()["session_id"]
    request = {"session_id": session_id, "profile": "urgent"}
    for _ in range(session_module.RUNS_PER_SESSION):
        client.post("/api/execute", json=request)
    before = module._STARTED
    client.post("/api/execute", json=request)
    assert module._STARTED == before


def test_an_unknown_session_is_refused_before_anything_is_queued(executing):
    client, executed, _ = executing
    response = client.post(
        "/api/execute", json={"session_id": "0" * 32, "profile": "urgent"}
    )
    assert response.status_code == 404
    assert executed == []


def test_health_says_this_deployment_signs_when_it_does(executing):
    client, _, _ = executing
    body = client.get("/api/health").json()
    assert body["signs"] is True and body["holds_keys"] is True
    assert body["execution_enabled"] is True


# ------------------------------------------------------------------------------------------
# Simulation: what these two agents would settle on, given a history a visitor chose.
# ------------------------------------------------------------------------------------------


def test_no_history_settles_everything_at_the_baseline(service):
    """Two strangers. Nothing to hold against each other, so no term moves.

    This is the control the rest of the simulation is read against: every later difference has
    to be attributable to an outcome the visitor added, and that is only true if the empty
    history is genuinely inert.
    """

    client, _ = service
    body = client.post("/api/simulate", json={"history": []}).json()

    assert body["simulated"] is True
    assert body["memories"]["buyer"]["risk"] == "0.0000"
    assert body["memories"]["provider"]["risk"] == "0.0000"
    for profile in body["profiles"]:
        assert profile["agreed"]
        assert profile["terms"] == {
            "price_wei": 100_000_000_000_000, "provider_bond_bps": 500,
            "service_window": 600, "payout_delay": 1_800,
        }


def test_the_history_that_really_happened_reproduces_the_live_quote(service):
    """The strongest thing this simulator can be asked to prove.

    Given the outcomes these two memories actually hold, the simulation has to produce the same
    terms the live quote produces from the receipts themselves. If it did not, it would be
    predicting a system nobody is running, and the difference would be invisible until a judge
    put the two screens side by side.
    """

    client, _ = service
    live = client.post("/api/quote", json={"memory": True}).json()
    simulated = client.post(
        "/api/simulate", json={"history": ["timeout_claimed_without_delivery"]}
    ).json()

    def terms(body):
        return {p["id"]: (p["terms"] if p["agreed"] else p["failed_on"]) for p in body["profiles"]}

    assert terms(simulated) == terms(live)
    assert simulated["memories"]["buyer"]["risk"] == live["memories"]["buyer"]["risk"]


def test_an_outcome_neither_memory_can_read_is_refused_by_name(service):
    """Not scored as zero, which would say it was harmless.

    A dimension is learned once from an outcome that really settled. Until one does, the honest
    answer is that this history cannot be priced, and it has to name which outcome and why.
    Silently contributing nothing would make an unknown look like a neutral, and the whole
    argument here is that the engine says what it is doing.
    """

    client, _ = service
    response = client.post(
        "/api/simulate", json={"history": ["delivered_and_released_by_buyer"]}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "delivered_and_released_by_buyer" in detail["error"]
    assert "harmless" in detail["error"]
    assert "delivered_and_released_by_buyer" in detail["outcomes"]


def test_an_outcome_the_escrow_cannot_produce_is_refused(service):
    """The closed set is the contract's, so a simulation cannot explore a world it could not
    reach. Free text here would let the page invent an outcome and price it."""

    client, _ = service
    response = client.post("/api/simulate", json={"history": ["seller_was_rude"]})
    assert response.status_code == 422
    assert "not an outcome this escrow can produce" in response.json()["detail"]["error"]


def test_a_history_longer_than_the_bound_is_refused(service):
    client, _ = service
    response = client.post(
        "/api/simulate", json={"history": ["timeout_claimed_without_delivery"] * 13}
    )
    assert response.status_code == 422
    assert "at most" in response.json()["detail"]["error"]


def test_simulating_writes_nothing_to_either_memory(service):
    """A simulation that could deposit a receipt would make every later quote unfalsifiable.

    Checked by file hash rather than by reading the code, because the property is about what
    the whole request did and not about what one function intended.
    """

    import hashlib

    client, warm = service

    def fingerprint():
        digest = hashlib.sha256()
        for path in sorted(warm.iterdir()):
            if path.is_file():
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    client.post("/api/quote", json={"memory": True})  # settle any first-open writes
    before = fingerprint()
    for history in ([], ["timeout_claimed_without_delivery"], ["timeout_claimed_without_delivery"] * 3):
        assert client.post("/api/simulate", json={"history": history}).status_code == 200
    assert fingerprint() == before


def test_the_same_history_simulates_to_the_same_document_twice(service):
    """A simulated result a reader cannot reproduce is an assertion, not a demonstration.

    The identifiers behind hypothetical outcomes are derived from their position and type
    rather than minted randomly, which is what makes the policy hash stable across two
    identical requests.
    """

    client, _ = service
    body = {"history": ["timeout_claimed_without_delivery", "timeout_claimed_without_delivery"]}
    first = client.post("/api/simulate", json=body).json()
    second = client.post("/api/simulate", json=body).json()

    hashes = lambda d: [p.get("policy_hash") for p in d["profiles"]]  # noqa: E731
    assert hashes(first) == hashes(second)


# ------------------------------------------------------------------------------------------
# Refunds: once, at the end of a session, into whichever wallet is emptier.
# ------------------------------------------------------------------------------------------


def test_a_session_refunds_once_however_many_times_it_is_asked(executing):
    """Idempotent by session, and marked before the worker starts.

    `withdraw` collects everything owed at execution time, so a second call moves nothing, and
    still costs gas, and still looks to a reader like a second refund happened. A visitor who
    presses finish twice, or who presses it after the last run already triggered one, has to
    get the same refund back.
    """

    client, _, _ = executing
    session_id = client.post("/api/session").json()["session_id"]

    first = client.post("/api/finish", json={"session_id": session_id}).json()
    second = client.post("/api/finish", json={"session_id": session_id}).json()

    assert first["already_refunded"] is False
    assert second["already_refunded"] is True
    assert second["run_id"] == first["run_id"]
    assert second["refunded"] is True


def test_the_last_run_of_a_session_refunds_without_being_asked(executing):
    """A visitor who has spent their allowance has finished whether or not they press anything.

    Leaving the escrow holding their deposits until somebody remembers is how a demo runs out
    of money, which is the failure this whole mechanism exists to prevent.
    """

    from wrasse import sessions as session_module

    client, _, _ = executing
    session_id = client.post("/api/session").json()["session_id"]
    request = {"session_id": session_id, "profile": "urgent"}

    last = None
    for _ in range(session_module.RUNS_PER_SESSION):
        last = client.post("/api/execute", json=request).json()

    # Waits for the refund to appear, not merely for the run to succeed. The worker queues the
    # refund from its own thread after the run finishes, so the first poll can legitimately
    # arrive before it exists. Asserting on the first poll passed on this laptop and failed on
    # a slower CI runner, which is the difference between a test and a coincidence.
    deadline = time.time() + 5
    body = {}
    while time.time() < deadline:
        body = client.get(f"/api/run/{last['run_id']}").json()
        if body.get("status") == "succeeded" and body.get("refund"):
            break
        time.sleep(0.01)

    assert body["status"] == "succeeded"
    assert body["session"]["runs_left"] == 0
    assert body.get("refund"), "the worker never queued the final refund"

    # Queued by the worker, not by this request. The GET only reports it, so a visitor who
    # closed the tab after their last transaction still gets their escrow back.
    refund_id = body["refund"]["run_id"]
    deadline = time.time() + 5
    refund = {}
    while time.time() < deadline:
        refund = client.get(f"/api/run/{refund_id}").json()
        if refund.get("status") == "succeeded":
            break
        time.sleep(0.01)

    assert refund["status"] == "succeeded", refund.get("error")
    assert refund["kind"] == "refund"
    assert refund["session"]["refunded"] is True
    assert refund["session"]["finishing"] is False


def test_the_final_refund_does_not_need_anybody_to_be_watching(executing):
    """A browser is not a durable job worker.

    The trigger used to live in the GET that reports progress, so a visitor who watched their
    last transaction land and closed the tab left the escrow holding their deposits. Nothing
    polls in this test at all.
    """

    from wrasse import executor, sessions as session_module

    client, _, queue = executing
    session_id = client.post("/api/session").json()["session_id"]
    request = {"session_id": session_id, "profile": "urgent"}
    for _ in range(session_module.RUNS_PER_SESSION):
        client.post("/api/execute", json=request)

    deadline = time.time() + 5
    while time.time() < deadline:
        refunds = [r for r in queue._runs.values()
                   if r.session_id == session_id and r.kind == executor.REFUND]
        if refunds:
            break
        time.sleep(0.01)

    assert refunds, "the escrow was left full because nobody happened to poll"


def test_a_run_reports_which_number_it_is(executing):
    """`Run 2 of 5` is the difference between a queue and a sequence a visitor is building."""

    from wrasse import sessions as session_module

    client, _, _ = executing
    session_id = client.post("/api/session").json()["session_id"]
    request = {"session_id": session_id, "profile": "urgent"}

    first = client.post("/api/execute", json=request).json()
    second = client.post("/api/execute", json=request).json()

    assert first["run_number"] == 1
    assert second["run_number"] == 2
    assert first["runs_allowed"] == session_module.RUNS_PER_SESSION


def test_a_refund_goes_to_whichever_wallet_is_emptier(tmp_path):
    """The recipient is chosen, and the reason is arithmetic.

    A settlement moves the price from the buyer to the provider and the bond from the provider
    and back again, so the provider's credit is price plus bond. Sending it to the buyer every
    time leaves the provider short by a bond per run; sending it to the provider every time
    leaves the buyer short by a price. Either way one wallet drains and somebody goes looking
    for a faucet. Emptier-first balances the pair on its own.
    """

    from wrasse import executor

    os.environ["WRASSE_BUYER_ADDRESS"] = BUYER
    os.environ["WRASSE_PROVIDER_A_ADDRESS"] = PROVIDER
    seen = {}

    def command(argv, env, timeout):
        if argv[0] == "withdraw":
            seen["to"] = argv[argv.index("--to") + 1]
            return 0, json.dumps({"intent_id": "i-w", "tx_hash": "0xw", "status": "pending"}), ""
        if argv[0] == "tx-resolve":
            return 0, json.dumps([{"intent_id": "i-w", "status": "included_success"}]), ""
        raise AssertionError(argv[0])

    for poorer, richer in (("buyer", "provider"), ("provider", "buyer")):
        run = executor.Run(
            run_id="r", session_id="s", kind=executor.REFUND, profile="", baseline={},
            paths={"buyer": tmp_path / "b.db", "provider": tmp_path / "p.db"},
            workdir=tmp_path,
        )
        runner = executor.Runner(
            command=command,
            balance_reader=lambda p=poorer, r=richer: {p: 1, r: 10**18},
            # The escrow is holding the seller's price and stake, which is what a released
            # deal leaves behind. Injected rather than read, because what this test is about
            # is where the money goes, not what the chain says is owed.
            credit_reader=lambda: {"provider": 5 * 10**14, "buyer": 0},
            sleep=lambda _: None,
        )
        runner.execute(run)
        assert run.status == executor.SUCCEEDED, run.error
        assert run.refund_to == poorer
        assert seen["to"].lower() == os.environ[
            "WRASSE_BUYER_ADDRESS" if poorer == "buyer" else "WRASSE_PROVIDER_A_ADDRESS"
        ].lower()


def test_a_refund_that_failed_can_be_asked_for_again(executing, monkeypatch):
    """The flag used to be set before the work, which made a failed refund permanent.

    A withdrawal can revert or time out with credit still in the escrow. The session stayed
    marked refunded, a later press handed back the same failed run id, and the page reported
    success over money nobody could reach. Refunding is now two states: `finishing` while a
    withdrawal is in flight, and `refunded` only once one has succeeded.
    """

    from wrasse import executor

    client, _, _ = executing
    session_id = client.post("/api/session").json()["session_id"]

    first = client.post("/api/finish", json={"session_id": session_id}).json()
    assert first["already_refunded"] is False
    assert first["finishing"] is True
    assert first["refunded"] is False

    run = None
    deadline = time.time() + 5
    while time.time() < deadline:
        run = service_module._queue().get(first["run_id"])
        if run is not None and run.status in (executor.SUCCEEDED, executor.FAILED):
            break
        time.sleep(0.01)
    assert run is not None

    # Pinned rather than raced. The worker here is instant, so waiting for the in-flight window
    # would test the scheduler's timing instead of the branch.
    run.status = executor.RUNNING
    inflight = client.post("/api/finish", json={"session_id": session_id}).json()
    assert inflight["run_id"] == first["run_id"], "a second press must not queue a second one"
    assert inflight["already_refunded"] is False

    # Whatever it did, make it a failure and ask again. A retry must mint a new run.
    run.status = executor.FAILED
    run.error = "the withdrawal reverted"

    retry = client.post("/api/finish", json={"session_id": session_id}).json()
    assert retry["already_refunded"] is False
    assert retry["run_id"] != first["run_id"], (
        "a failed refund must be retryable, or the escrow keeps what it could not collect"
    )


def test_a_finished_session_is_refused_another_settlement(executing):
    """Its escrow is being collected, so a later run would leave its own deposit behind."""

    client, _, _ = executing
    session_id = client.post("/api/session").json()["session_id"]
    client.post("/api/finish", json={"session_id": session_id})

    refused = client.post(
        "/api/execute", json={"session_id": session_id, "profile": "urgent"}
    )

    assert refused.status_code == 429
    assert "finished" in refused.json()["detail"]


def test_a_session_is_not_opened_before_the_memories_it_copies(executing):
    """`/api/session` copies the working pair, which nothing had created on a fresh boot.

    The quote path was the only caller of `prepare_working_copies`, so a visitor who pressed
    the run button before quoting got a session copied from files that did not exist. It
    returned two empty stores and the page presented them as the seeded history.
    """

    client, _, _ = executing
    service_module._stores.clear()

    body = client.post("/api/session").json()
    session = service_module._session_registry().get(body["session_id"])

    assert session is not None
    for path in session.paths.values():
        assert path.is_file(), f"{path} was not copied"
        held = sqlite3.connect(path).execute(
            "select count(*) from entities where category='chain_event'"
        ).fetchone()[0]
        assert held > 0, "a session copied from an empty source is a cold start in disguise"


def test_the_worker_reclaims_before_it_settles_anything(executing, monkeypatch):
    """A restart leaves the ledger holding nonces for transactions nobody remembers.

    The queue is serialised, so submitting the reclaim when the queue is created puts it ahead
    of every visitor's settlement rather than merely near the front.
    """

    from wrasse import executor

    # Built through `_queue` rather than through the fixture, which injects a queue directly
    # and so never reaches the line under test. A queue that starts no thread, because what is
    # being asserted is what was submitted and in what order, not that a worker ran it.
    executing  # the service is configured; this test drives the constructor itself
    submitted: list[executor.Run] = []

    class Recording:
        def submit(self, run):
            submitted.append(run)

    monkeypatch.setattr(service_module, "_QUEUE", None)
    monkeypatch.setattr(service_module, "EXECUTION", True)
    monkeypatch.setattr(service_module.executor, "Queue", lambda *a, **k: Recording())

    service_module._queue()

    assert [run.kind for run in submitted] == [executor.RECLAIM], (
        "the worker will settle for a visitor before freeing the nonces a restart left held"
    )


def test_a_settlement_is_published_while_the_session_is_still_locked(executing):
    """Charging a run and publishing it used to be two steps with a gap between them.

    A refund arriving in that gap claimed the session, ran, found nothing to collect and
    succeeded. Only then did the already-charged settlement reach the queue, create a deal and
    credit value that no refund was scheduled to collect.

    The assertion is that publication happens under the lock a refund must take, which is the
    property; a second thread would only demonstrate one interleaving of it, and the lock is
    not reentrant so the two calls cannot be nested in one.
    """

    client, _, queue = executing
    session_id = client.post("/api/session").json()["session_id"]
    registry = service_module._session_registry()
    session = registry.get(session_id)
    held: list[bool] = []

    def publish():
        held.append(registry._lock.locked())

    registry.admit(session, publish)

    assert held == [True], "a refund could claim this session between charging and queueing"
    assert session.runs == 1


def test_a_settlement_that_cannot_be_queued_is_not_charged(executing):
    """The allowance is spent by a run that exists, not by one that failed to start."""

    client, _, _ = executing
    session_id = client.post("/api/session").json()["session_id"]
    registry = service_module._session_registry()
    session = registry.get(session_id)

    def refuses():
        raise OSError("no space left on device")

    with pytest.raises(OSError):
        registry.admit(session, refuses)

    assert session.runs == 0, "a run that never queued still cost the visitor one of five"


def _a_run(session):
    from wrasse import executor

    workdir = session.directory / "runs" / "probe"
    workdir.mkdir(parents=True, exist_ok=True)
    return executor.Run(
        run_id="probe", session_id=session.session_id, profile="urgent",
        baseline={"price_wei": 10**14, "provider_bond_bps": 500,
                  "service_window": 600, "payout_delay": 1800},
        paths=session.paths, workdir=workdir,
    )


def test_a_refund_that_cannot_be_queued_gives_the_session_back(executing, monkeypatch):
    """A claim without a job is a session that can never refund.

    `finishing` is set before the run directory exists and before anything is submitted. A
    full volume between those left the flag standing with no run id to report and no job to
    retry, so every later press returned nothing and the credits could not be collected.
    """

    client, _, queue = executing
    session_id = client.post("/api/session").json()["session_id"]
    session = service_module._session_registry().get(session_id)

    def refuses(run):
        raise OSError("no space left on device")

    monkeypatch.setattr(queue, "submit", refuses)

    with pytest.raises(OSError):
        service_module._refund(session)

    assert session.finishing is False, "the session was left claimed with nothing to retry"
    assert session.refund_run_id is None

    # And it can be asked again, which is the whole point of giving the claim back.
    monkeypatch.undo()
    again = service_module._refund(session)
    assert again["run_id"] is not None


def test_a_run_that_fails_with_a_deal_open_gets_a_refund_without_being_asked(
    executing, monkeypatch, tmp_path
):
    """This is the case recovery was built for, and the page used to show only an error.

    A visitor whose first run failed after creating a deal could not reach recovery at all:
    the automatic refund waited for the fifth run, and the button was hidden on failure. The
    deposit sat there until they performed four more settlements or an operator intervened.
    """

    from wrasse import executor, liabilities

    client, _, queue = executing
    session_id = client.post("/api/session").json()["session_id"]

    monkeypatch.setattr(
        liabilities, "open_deals",
        lambda sid=None, **kw: [4] if sid == session_id else [],
    )

    failed = executor.Run(
        run_id="failed", session_id=session_id, profile="urgent", baseline={},
        paths={}, workdir=tmp_path,
    )
    failed.status = executor.FAILED
    service_module._run_finished(failed)

    refunds = [r for r in queue._runs.values()
               if r.session_id == session_id and r.kind == executor.REFUND]
    assert refunds, "a failed run left its deposit with no way for the visitor to recover it"


def test_a_run_that_failed_after_moving_value_is_collected_even_with_an_empty_index(
    executing, monkeypatch, tmp_path
):
    """The liability is struck off at confirmation, one step before the memories are taught.

    So a run whose `reconcile` fails ends with the contract having assigned the credit, no open
    deal to notice it by, and nothing scheduled to collect it. The chain fact is permanent at
    that point and the memory write is allowed to fail; the money must not depend on the write.
    """

    from wrasse import executor, liabilities

    client, _, queue = executing
    session_id = client.post("/api/session").json()["session_id"]

    monkeypatch.setattr(liabilities, "open_deals", lambda sid=None, **kw: [])

    failed = executor.Run(
        run_id="reconcile-failed", session_id=session_id, profile="urgent", baseline={},
        paths={}, workdir=tmp_path,
    )
    failed.status = executor.FAILED
    failed.deal_id = 31
    failed.error = "reconcile could not reach the memory service"

    service_module._run_finished(failed)

    refunds = [r for r in queue._runs.values()
               if r.session_id == session_id and r.kind == executor.REFUND]
    assert refunds, "a confirmed deal whose memory write failed was left uncollected"


def test_a_successful_first_run_still_does_not_trigger_an_early_refund(
    executing, monkeypatch, tmp_path
):
    """The regression this project already shipped once, kept dead.

    Recovery triggers on a settlement that ends with a deal still open, and for a while nothing
    on the happy path struck its own deal off, so the first successful run finished the session
    and the second was refused. The new failed-with-a-deal-id trigger must not bring that back
    through the other door.
    """

    from wrasse import executor, liabilities

    client, _, queue = executing
    session_id = client.post("/api/session").json()["session_id"]

    monkeypatch.setattr(liabilities, "open_deals", lambda sid=None, **kw: [])

    done = executor.Run(
        run_id="clean", session_id=session_id, profile="urgent", baseline={},
        paths={}, workdir=tmp_path,
    )
    done.status = executor.SUCCEEDED
    done.deal_id = 32

    service_module._run_finished(done)

    refunds = [r for r in queue._runs.values()
               if r.session_id == session_id and r.kind == executor.REFUND]
    assert not refunds, "run one finished the session again"
    session = service_module._session_registry().get(session_id)
    assert session.finishing is False


def test_reconciling_an_older_refund_does_not_disturb_a_newer_one(executing):
    """Two callers reconciling the same failed refund raced over one session's flags.

    The first could clear a claim the second had already replaced, leaving a queued withdrawal
    that nothing tracked. Driven by naming the ids directly, because the property is that a
    stale view cannot write, not that one particular interleaving happens to be safe.
    """

    client, _, _ = executing
    session_id = client.post("/api/session").json()["session_id"]
    registry = service_module._session_registry()
    session = registry.get(session_id)

    assert registry.begin_finishing(session) is True
    session.refund_run_id = "r2"

    # A caller still holding the view that `r1` is current tries to release the claim.
    moved = registry.settle_refund(session, "r1", succeeded=False)

    assert moved is False
    assert session.finishing is True, "a stale caller released a newer refund's claim"
    assert session.refund_run_id == "r2"

    # And the caller that does name the current one still works.
    assert registry.settle_refund(session, "r2", succeeded=True) is True
    assert session.refunded is True


def test_the_deletion_proof_answers_all_four_cases_and_writes_nothing(service):
    """The eligibility test, served to a browser rather than run in a terminal.

    Sibyl's rule is that a project whose core function survives deleting the memory layer is a
    wrapper. The page has to be able to demonstrate that on demand, so this endpoint takes the
    memory away from the stores this deployment is serving and quotes again, four ways.

    Two properties, and the second is the one that would be discovered in production. The
    control has to produce terms while the three broken cases refuse, because a build that
    refuses everything proves nothing about memory. And the live stores have to come back
    byte-identical, because a proof button that damaged the memory it was proving would take
    the demo down the first time a judge pressed it twice.
    """

    import hashlib

    client, warm = service

    assert client.get("/api/quote", params={"memory": "on"}).status_code == 200
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(warm.glob("*memory.db*"))
    }

    response = client.post("/api/prove")
    assert response.status_code == 200, response.text
    body = response.json()

    outcomes = {case["name"]: case["outcome"] for case in body["cases"]}
    assert outcomes == {
        "both memories intact": "terms",
        "both memory files deleted": "refusal",
        "a memory that will not open": "refusal",
        "a receipt edited in place": "refusal",
    }, "the four cases did not answer the way the eligibility test needs them to"
    assert body["passed"] is True

    control = [case for case in body["cases"] if case["name"] == "both memories intact"][0]
    assert control["terms"], "the control has to show the terms memory produced"
    for case in body["cases"]:
        if case["outcome"] == "refusal":
            assert case["detail"], f"{case['name']} refused without saying why"

    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(warm.glob("*memory.db*"))
    }
    assert before == after, "the deletion proof wrote to the memory it was proving"


def test_the_deletion_proof_refuses_when_there_is_no_memory_to_delete(service, monkeypatch):
    """A deployment with no stores cannot answer the question, and must not pretend to.

    Four refusals from a machine that never had a memory would read as a pass, which is exactly
    the confusion the eligibility test exists to catch. It is answered with a 503 that says the
    test has no subject.
    """

    from wrasse import service as module

    client, warm = service
    monkeypatch.setitem(module._PATHS[module.WARM], "buyer", warm / "nothing-here.db")

    response = client.post("/api/prove")
    assert response.status_code == 503
    assert "no memory to take away" in response.json()["detail"]["error"]


def test_a_missing_warm_memory_stops_the_service_rather_than_being_invented(service, monkeypatch):
    """The eligibility hole, one layer above where it was fixed.

    `_open_stores` refuses a store file that is not there. `_initialise_metadata` used to hand
    it `allow_new=True` for both pairs, so the service created the warm pair on first open and
    served baseline terms from two empty stores, under a page that says the numbers come from
    two confirmed outcomes on Base. The cold pair is created on purpose and must stay creatable;
    the warm pair must not be, and the difference is the whole eligibility argument.
    """

    from wrasse import cli as cli_module
    from wrasse import service as module

    client, warm = service
    module._stores.clear()

    missing = warm / "gone"
    missing.mkdir()
    monkeypatch.setitem(
        module._PATHS, module.WARM,
        {"buyer": missing / "buyer-memory.db", "provider": missing / "provider-memory.db"},
    )

    with pytest.raises(cli_module.MemoryRequired):
        module._open(module.WARM)

    assert not list(missing.iterdir()), "a refused open still created the store it refused"


def test_the_cold_pair_is_still_created_when_it_is_not_there(service, monkeypatch):
    """The other half, so the fix above cannot be a blanket refusal.

    An empty store is present, has been asked, and holds nothing. That is an honest cold start
    and it is the control the whole comparison rests on, so it has to be creatable. A change
    that refused both pairs would pass the test above and take the page's left-hand column with
    it.
    """

    from wrasse import service as module

    client, warm = service
    module._stores.clear()

    fresh = warm / "cold-elsewhere"
    monkeypatch.setitem(
        module._PATHS, module.COLD,
        {"buyer": fresh / "buyer-memory.db", "provider": fresh / "provider-memory.db"},
    )
    fresh.mkdir()

    stores = module._open(module.COLD)
    assert set(stores) == {"buyer", "provider"}
    assert (fresh / "buyer-memory.db").is_file()


def test_one_session_opened_from_two_requests_opens_its_stores_once(executing):
    """The third first-open, which the fix for the other two left behind.

    `_open` was serialised and this cache was not, so two requests naming one session both
    passed `not in _session_stores` and both opened the same databases. Two open descriptions
    on one SQLite file inside one process do not exclude each other, they block, and the
    session belongs to a judge mid-demo.

    Driven through the open itself rather than by racing two threads, because one interleaving
    is not the property: the count of opens is.
    """

    import threading

    from wrasse import cli as cli_module
    from wrasse import service as module

    client, _, _ = executing

    response = client.post("/api/session")
    assert response.status_code == 200, response.text
    session = module._session_registry().get(response.json()["session_id"])
    assert session is not None

    module._session_stores.pop(session.session_id, None)

    started = threading.Event()
    opens = []
    real = cli_module._open_stores

    def slow(*args, **kwargs):
        opens.append(1)
        # Hold the open long enough that an unguarded second caller would be inside it too.
        started.set()
        time.sleep(0.3)
        return real(*args, **kwargs)

    module.cli._open_stores = slow
    try:
        first = threading.Thread(target=module._open_session, args=(session,))
        first.start()
        assert started.wait(5), "the first open never began"
        module._open_session(session)
        first.join(10)
    finally:
        module.cli._open_stores = real

    assert len(opens) == 1, f"the session's stores were opened {len(opens)} times, not once"


def test_the_deletion_proof_spends_one_budget_across_all_four_cases(monkeypatch):
    """`TIMEOUT` bounds the run, not each subprocess.

    It used to bound each one, so four cases could hold the endpoint's lock for four times the
    stated timeout between them while every later visitor waited behind it or timed out at the
    proxy.

    The property is that the budget handed to each case is what remains of one deadline, so it
    strictly decreases. Asserting only that a tiny timeout raises does not test this: a
    per-case budget of zero raises too, which is how the first version of this test survived
    having the fix reverted under it.
    """

    from wrasse import prove as proving

    budgets = []

    def record(paths, buyer, provider, budget):
        budgets.append(budget)
        time.sleep(0.05)
        return 1, "refused"

    monkeypatch.setattr(proving, "TIMEOUT", 30.0)
    monkeypatch.setattr(proving, "_quote", record)
    # `CASES` holds the break functions directly, so patching the module attributes would not
    # reach them. The mutations are not what this test is about; the budgets are.
    monkeypatch.setattr(proving, "CASES", tuple(
        (name, did, expected, lambda paths: None) for name, did, expected, _ in proving.CASES
    ))

    proving.prove({}, buyer="0x" + "1" * 40, provider="0x" + "2" * 40)

    assert len(budgets) == len(proving.CASES)
    assert budgets == sorted(budgets, reverse=True) and budgets[0] > budgets[-1], (
        f"each case was given its own budget rather than what remained of one: {budgets}"
    )
    assert budgets[0] <= 30.0, "the first case was given more than the whole run's budget"


def test_a_deletion_case_that_times_out_is_not_reported_as_a_refusal(monkeypatch):
    """Three of the four cases pass BY refusing, so this distinction is the whole test.

    A case that merely failed to answer in time would be indistinguishable from one that
    declined to answer, and the proof would report a pass it had not earned.
    """

    import subprocess

    from wrasse import prove as proving

    def never_answers(paths, buyer, provider, budget):
        raise subprocess.TimeoutExpired(cmd="wrasse policy", timeout=budget)

    monkeypatch.setattr(proving, "_quote", never_answers)
    monkeypatch.setattr(proving, "CASES", tuple(
        (name, did, expected, lambda paths: None) for name, did, expected, _ in proving.CASES
    ))

    with pytest.raises(RuntimeError) as raised:
        proving.prove({}, buyer="0x" + "1" * 40, provider="0x" + "2" * 40)

    message = str(raised.value)
    assert "did not answer" in message
    assert "is not reported as one" in message


def test_the_unreadable_case_leaves_a_file_no_sqlite_will_open(tmp_path):
    """CI caught this and the suite did not, on a machine whose SQLite disagreed with mine.

    The case used to write twenty-two bytes. A short non-empty file is ambiguous: the runner
    opened it as a fresh EMPTY database instead of refusing it, so the quote succeeded and the
    proof reported "a memory that will not open -> terms".

    That is the empty-versus-absent confusion this whole project exists to refuse, appearing
    inside the test that checks for it, on the one deliverable the entry is judged on.
    """

    import sqlite3

    from wrasse import prove as proving

    store = tmp_path / "a-store-of-its-own.db"
    connection = sqlite3.connect(store)
    connection.execute("create table entities (body text)")
    connection.commit()
    connection.close()

    proving._unreadable({"buyer": store})

    assert store.stat().st_size > 100, "a file shorter than the header is ambiguous to SQLite"
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(store).execute("select count(*) from sqlite_master").fetchone()


def test_a_corruption_that_did_not_corrupt_is_refused_rather_than_reported(tmp_path, monkeypatch):
    """The self-check, which is what would have turned the CI failure into a loud error.

    If the payload ever stops being unreadable on some future SQLite, this case must say so
    rather than quietly becoming a fourth way of asking an empty store for terms.
    """

    from wrasse import prove as proving

    store = tmp_path / "another-store-of-its-own.db"

    def leaves_it_readable(_data):
        connection = __import__("sqlite3").connect(store)
        connection.execute("create table entities (body text)")
        connection.commit()
        connection.close()

    monkeypatch.setattr(type(store), "write_bytes", lambda self, data: leaves_it_readable(data))

    with pytest.raises(RuntimeError) as raised:
        proving._unreadable({"buyer": store})
    assert "proves nothing" in str(raised.value)


def test_the_proof_copies_rows_that_are_still_in_the_write_ahead_log(tmp_path):
    """The copy has to be a snapshot, not three files grabbed while someone else writes.

    `shutil.copy` of a database, its `-wal` and its `-shm` is not consistent: the service holds
    these stores open and SQLite checkpoints on its own schedule, so a copy taken across that
    boundary can arrive without the rows it is supposed to carry. Every case then reasons about
    a store nobody ever served, intermittently, which is how the one test this entry is judged
    on came back wrong on CI and passed here three runs in a row.
    """

    import sqlite3

    from wrasse import prove as proving

    origin = tmp_path / "live-buyer.db"
    live = sqlite3.connect(origin)
    live.execute("pragma journal_mode=wal")
    live.execute("create table entities (category text, body text)")
    live.execute("insert into entities values ('chain_event', '{\"tx_hash\":\"0xabc\"}')")
    live.commit()
    # Deliberately still open and unchecked-pointed, which is the state the service leaves it in.

    try:
        (tmp_path / "snapshot").mkdir()
        copied = proving._copy({"buyer": origin}, tmp_path / "snapshot")

        rows = sqlite3.connect(copied["buyer"]).execute(
            "select body from entities where category = 'chain_event'"
        ).fetchall()
        assert len(rows) == 1, "the snapshot lost a row that was still in the write-ahead log"
        assert not copied["buyer"].with_name(copied["buyer"].name + "-wal").exists(), (
            "the snapshot carried a sidecar, which means it is three files again"
        )
    finally:
        live.close()

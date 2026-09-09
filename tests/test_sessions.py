"""Per-visitor memory copies, held to the two properties that make them worth having.

A session is only useful if it carries the whole memory and if it is genuinely private. Both
have failed here before in ways that looked like success: a copy missing its write-ahead log
reads as an empty store rather than as a broken one, and two paths that happen to agree prove
nothing about the code meant to keep them apart.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from wrasse import sessions
from wrasse.sessions import Sessions, copy_database


def _open_wal(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS receipts (body TEXT)")
    return connection


def _insert(connection: sqlite3.Connection, value: str) -> None:
    connection.execute("INSERT INTO receipts VALUES (?)", (value,))
    connection.commit()


def _write_leaving_the_log_uncheckpointed(path: Path, value: str) -> None:
    """A database whose most recent row is still in `-wal`, which is the normal state.

    The connection is deliberately left open. Closing the last connection checkpoints the log
    into the main file and deletes it, so a fixture that closes cannot produce the state this
    is trying to reproduce, and the first version of this test did exactly that and proved
    nothing. A live memory almost always has a log beside it, because something is holding it
    open.
    """

    connection = _open_wal(path)
    _insert(connection, value)
    _LEFT_OPEN.append(connection)


#: Connections held open for the length of a test, so their logs are not checkpointed away.
_LEFT_OPEN: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _close_held_connections():
    yield
    while _LEFT_OPEN:
        _LEFT_OPEN.pop().close()


def _rows(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute("SELECT body FROM receipts")]
    finally:
        connection.close()


def test_a_copy_carries_rows_that_are_still_in_the_write_ahead_log(tmp_path):
    """Copying the `.db` alone loses them, silently, and answers as an empty memory.

    This is the failure the service already hit once. It matters more than it sounds because
    an empty store and a broken mount are indistinguishable from the outside: both quote as a
    cold start, and only one of them is a true answer.
    """

    origin = tmp_path / "buyer-memory.db"
    _write_leaving_the_log_uncheckpointed(origin, "a receipt")
    assert (tmp_path / "buyer-memory.db-wal").is_file(), "the fixture must leave a log behind"

    destination = tmp_path / "copy" / "buyer-memory.db"
    copy_database(origin, destination)
    assert _rows(destination) == ["a receipt"]


def test_a_copy_is_writable_even_when_its_source_is_not(tmp_path):
    """`shutil.copy` carries the source's mode, and the source is deliberately read-only.

    A deployment mounts the memories read-only so the artifact cannot be mutated. Without the
    permission reset the copy inherits that and cannot be opened at all, which is exactly the
    failure the copy exists to avoid.
    """

    origin = tmp_path / "provider-memory.db"
    _write_leaving_the_log_uncheckpointed(origin, "a receipt")
    origin.chmod(0o444)

    destination = tmp_path / "copy" / "provider-memory.db"
    copy_database(origin, destination)
    _write_leaving_the_log_uncheckpointed(destination, "another receipt")
    assert _rows(destination) == ["a receipt", "another receipt"]


@pytest.fixture
def registry(tmp_path):
    source = {}
    for role in ("buyer", "provider"):
        path = tmp_path / "source" / f"{role}-memory.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_leaving_the_log_uncheckpointed(path, f"{role} receipt")
        source[role] = path
    return Sessions(source, root=tmp_path / "sessions", limit=2, runs_per_session=2)


def test_two_sessions_do_not_share_a_memory(registry):
    """The whole reason a session exists: what one visitor teaches, the next does not inherit.

    Every visitor is entitled to the same starting point, so the before they are shown is the
    before the write-up describes. A shared pair would drift somewhere nobody chose by the
    tenth visitor.
    """

    first, second = registry.create(), registry.create()
    assert first.paths["buyer"] != second.paths["buyer"]

    _write_leaving_the_log_uncheckpointed(first.paths["buyer"], "taught by the first visitor")
    assert _rows(second.paths["buyer"]) == ["buyer receipt"]


def test_a_session_may_not_run_past_its_allowance(registry):
    """Both wallets are shared and faucet-funded, so one visitor cannot spend the demo."""

    session = registry.create()
    registry.spend(session)
    registry.spend(session)
    with pytest.raises(PermissionError, match="limit"):
        registry.spend(session)
    assert session.runs == 2


def test_the_oldest_session_is_evicted_and_its_files_are_deleted(registry):
    """Disk is bounded rather than visitors. An evicted visitor is given a new session."""

    first = registry.create()
    registry.create()
    registry.create()

    assert registry.get(first.session_id) is None
    assert not first.directory.exists()


def test_an_identifier_this_process_never_issued_does_not_resolve(registry):
    """Lookup, never a path join. A visitor-supplied string that reaches the filesystem is a
    traversal bug however carefully it is escaped; one that can only match something already
    minted has no such shape."""

    assert registry.get("../../etc") is None
    assert registry.get("0" * 32) is None


# ------------------------------------------------------------------------------------------
# What a session must refuse to do, rather than do quietly.
# ------------------------------------------------------------------------------------------


def test_a_session_refuses_to_copy_a_source_that_is_not_there(tmp_path):
    """Silently skipping produced the exact failure every other check here exists to prevent.

    A missing source used to `continue`, and `create` still returned a session, so the visitor
    got two empty stores presented as the seeded history. That is a cold start wearing the
    clothes of a warm one, and nothing downstream can tell them apart. It is reachable on a
    fresh deployment, where the working copies do not exist until something has opened them.
    """

    source = {"buyer": tmp_path / "absent" / "buyer-memory.db"}
    registry = sessions.Sessions(source, root=tmp_path / "sessions")

    with pytest.raises(FileNotFoundError) as raised:
        registry.create()

    assert "nothing to copy" in str(raised.value)


def test_a_sidecar_the_source_lacks_does_not_survive_at_the_destination(tmp_path):
    """A write-ahead log belonging to a different database is worse than none at all."""

    origin = tmp_path / "origin.db"
    sqlite3.connect(origin).close()
    destination = tmp_path / "copy" / "origin.db"
    destination.parent.mkdir(parents=True)
    stale = destination.with_name(destination.name + "-wal")
    stale.write_bytes(b"a log from some other database")

    sessions.copy_database(origin, destination)

    assert not stale.exists()


def test_eviction_leaves_a_session_that_is_still_running(tmp_path):
    """A public endpoint reaches this with session creation alone.

    One stranger past the limit deletes the oldest directory, and if that session has a run in
    flight the worker loses the databases it is halfway through teaching. The escrow keeps the
    deposit and no refund can ever name the paths again.
    """

    origin = tmp_path / "buyer-memory.db"
    sqlite3.connect(origin).close()
    busy_ids: set[str] = set()
    registry = sessions.Sessions(
        {"buyer": origin}, root=tmp_path / "sessions", limit=2,
        busy=lambda session: session.session_id in busy_ids,
    )

    working = registry.create()
    busy_ids.add(working.session_id)
    idle = registry.create()          # over the limit; the busy one must survive
    registry.create()                 # and so must the survivor's directory

    assert registry.get(working.session_id) is not None
    assert working.directory.is_dir()
    assert registry.get(idle.session_id) is None


def test_creation_is_refused_when_every_retained_session_is_still_busy(tmp_path):
    """Capacity is an admission rule, not only a deletion rule.

    Eviction skips busy sessions, which is right: deleting files under a running worker is
    worse than exceeding a disk bound. But a public endpoint could then create sessions faster
    than the single worker drains them, every one would be busy, and the bound stopped binding
    at the moment it mattered. Refusing is the other half of it.
    """

    origin = tmp_path / "buyer-memory.db"
    sqlite3.connect(origin).close()
    busy_ids: set[str] = set()
    registry = sessions.Sessions(
        {"buyer": origin}, root=tmp_path / "sessions", limit=2,
        busy=lambda session: session.session_id in busy_ids,
    )

    for _ in range(2):
        busy_ids.add(registry.create().session_id)

    with pytest.raises(RuntimeError) as raised:
        registry.create()
    assert "work in flight" in str(raised.value)

    # And it clears on its own, which is the whole reason it is a refusal rather than an error.
    busy_ids.clear()
    assert registry.create() is not None


def test_an_idle_session_is_evicted_once_it_stops_being_busy(tmp_path):
    """Kept, not exempt. A busy session that stayed forever would defeat the limit entirely."""

    origin = tmp_path / "buyer-memory.db"
    sqlite3.connect(origin).close()
    busy_ids: set[str] = set()
    registry = sessions.Sessions(
        {"buyer": origin}, root=tmp_path / "sessions", limit=2,
        busy=lambda session: session.session_id in busy_ids,
    )

    first = registry.create()
    busy_ids.add(first.session_id)
    registry.create()
    assert registry.get(first.session_id) is not None

    busy_ids.clear()
    registry.create()

    assert registry.get(first.session_id) is None


def test_a_finished_session_cannot_start_another_settlement(tmp_path):
    """Its escrow has been collected, so a later run would leave its deposit behind."""

    origin = tmp_path / "buyer-memory.db"
    sqlite3.connect(origin).close()
    registry = sessions.Sessions({"buyer": origin}, root=tmp_path / "sessions")
    session = registry.create()

    session.finishing = True
    with pytest.raises(PermissionError) as raised:
        registry.spend(session)
    assert "finished" in str(raised.value)

    session.finishing = False
    session.refunded = True
    with pytest.raises(PermissionError):
        registry.spend(session)


def test_a_session_being_copied_counts_against_the_limit(tmp_path):
    """The copy is slow and happens outside the lock, which is right and was also the hole.

    Several concurrent creates all passed the admission check before any of them added a
    session, so a public burst went straight past the stated bound. Driven through the copy
    itself, so the second create happens exactly inside the window rather than by timing.
    """

    origin = tmp_path / "buyer-memory.db"
    sqlite3.connect(origin).close()
    registry = sessions.Sessions(
        {"buyer": origin}, root=tmp_path / "sessions", limit=1, busy=lambda session: False,
    )

    refused: list[str] = []
    real_copy = sessions.copy_database

    def copying(source, destination):
        if not refused:
            try:
                registry.create()
                refused.append("allowed")
            except RuntimeError as error:
                refused.append(str(error))
        return real_copy(source, destination)

    sessions.copy_database = copying
    try:
        registry.create()
    finally:
        sessions.copy_database = real_copy

    assert refused and refused[0] != "allowed", (
        "two creates both passed the cap before either of them landed"
    )
    assert "work in flight" in refused[0]


def test_a_copy_that_fails_gives_its_reservation_back(tmp_path):
    """A reservation that leaked would shrink the deployment's capacity on every failure."""

    origin = tmp_path / "buyer-memory.db"
    sqlite3.connect(origin).close()
    registry = sessions.Sessions(
        {"buyer": origin}, root=tmp_path / "sessions", limit=1, busy=lambda session: False,
    )

    real_copy = sessions.copy_database

    def refusing(source, destination):
        raise OSError("no space left on device")

    sessions.copy_database = refusing
    try:
        with pytest.raises(OSError):
            registry.create()
    finally:
        sessions.copy_database = real_copy

    assert registry._pending == 0
    assert registry.create() is not None, "the failed create cost a slot forever"

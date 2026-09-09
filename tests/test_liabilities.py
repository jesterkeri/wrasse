"""The durable index of open deals, against a real SQLite file rather than a fake.

Every other test of this subsystem injects `FakeLiabilities`, which is right for testing the
run procedure's ordering and wrong for testing the file itself. The migration in particular
cannot be exercised any other way: it is a schema transition on a database written by a
previous deployment, and the thing at risk is a real row naming a real deal that the escrow is
still holding value for.
"""

from __future__ import annotations

import sqlite3

import pytest

from wrasse import liabilities

V1_SCHEMA = """
create table open_deals (
    deal_id     integer primary key,
    session_id  text not null,
    opened_at   text not null,
    detail      text not null default '{}'
)
"""


@pytest.fixture
def older_database(tmp_path):
    """A database in the shape the previous deployment wrote, holding one open deal."""

    path = tmp_path / "liabilities.db"
    connection = sqlite3.connect(path)
    connection.execute(V1_SCHEMA)
    connection.execute(
        "insert into open_deals(deal_id, session_id, opened_at, detail) values (?, ?, ?, ?)",
        (41, "a session that no longer exists", "2026-09-09T00:00:00+00:00",
         '{"create_tx": "0xabc"}'),
    )
    connection.commit()
    connection.close()
    return path


def test_an_older_index_keeps_the_deals_it_was_holding(older_database):
    """The first version of this dropped the table, which deleted the route to real money.

    "Migration is a reset" is this project's rule for memory, and it is the wrong rule here: a
    memory store rebuilds from receipts and this file rebuilds from nothing. It is the only
    list of deals the escrow still holds value for.
    """

    assert liabilities.open_deals(path=older_database) == [41]


def test_the_migrated_row_keeps_the_exact_deal_id_recovery_needs(older_database):
    """The deal id is the field recovery acts on, so it is the field that has to survive."""

    rows = liabilities.rows(path=older_database)

    assert len(rows) == 1
    assert rows[0]["deal_id"] == 41
    assert rows[0]["session_id"] == "a session that no longer exists"
    assert rows[0]["intent_id"] == "migrated:41"
    assert rows[0]["opened_at"] == "2026-09-09T00:00:00+00:00"


def test_a_migrated_row_can_be_closed_like_any_other(older_database):
    """Recovery has to be able to finish with it, not merely read it."""

    liabilities.close(41, path=older_database)

    assert liabilities.open_deals(path=older_database) == []


def test_migrating_twice_is_the_same_as_migrating_once(older_database):
    """Every open of this file runs the transition, so it has to be idempotent."""

    liabilities.rows(path=older_database)
    liabilities.rows(path=older_database)

    assert liabilities.open_deals(path=older_database) == [41]
    with sqlite3.connect(older_database) as connection:
        tables = {
            row[0] for row in
            connection.execute("select name from sqlite_master where type='table'")
        }
    assert "open_deals_v1" not in tables, "the old table was left behind"


def test_the_lifecycle_a_creation_actually_walks(tmp_path):
    """Opened before signing, given its hash, then its id, then forgotten."""

    path = tmp_path / "liabilities.db"
    liabilities.open_intent("quote-1:urgent", "s1", path=path)
    assert liabilities.unresolved(path=path) == []
    assert liabilities.open_deals(path=path) == []

    liabilities.attach("quote-1:urgent", tx_hash="0xfeed", path=path)
    assert [row["intent_id"] for row in liabilities.unresolved(path=path)] == ["quote-1:urgent"]

    liabilities.attach("quote-1:urgent", deal_id=9, path=path)
    assert liabilities.unresolved(path=path) == []
    assert liabilities.open_deals(path=path) == [9]

    liabilities.close(9, path=path)
    assert liabilities.rows(path=path) == []


def test_a_row_that_has_a_deal_cannot_be_discarded(tmp_path):
    """`discard` is only for a send that never reached a node. A deal means it did."""

    path = tmp_path / "liabilities.db"
    liabilities.open_intent("i", "s", path=path)
    liabilities.attach("i", tx_hash="0xfeed", deal_id=5, path=path)

    liabilities.discard("i", path=path)

    assert liabilities.open_deals(path=path) == [5], "a live deposit was discarded"


def test_a_migration_interrupted_halfway_is_finished_on_the_next_open(tmp_path):
    """The exact state a killed process used to leave, and it used to be invisible.

    The previous version committed each statement separately, so a process killed after the
    rename left the rows in a table nothing reads and a new empty table that looked migrated.
    Recovery returned nothing while the escrow still held the deal. Both halves are fixed: the
    migration is one transaction, and a leftover old table is finished rather than ignored.
    """

    path = tmp_path / "liabilities.db"
    connection = sqlite3.connect(path)
    connection.execute(V1_SCHEMA.replace("open_deals", "open_deals_v1"))
    connection.execute(
        "insert into open_deals_v1(deal_id, session_id, opened_at, detail) values (?,?,?,?)",
        (41, "a session that died", "2026-09-09T00:00:00+00:00", "{}"),
    )
    connection.execute(
        """create table open_deals (intent_id text primary key, session_id text not null,
           opened_at text not null, tx_hash text, deal_id integer,
           detail text not null default '{}')"""
    )
    connection.commit()
    connection.close()

    assert liabilities.open_deals(path=path) == [41]
    with sqlite3.connect(path) as check:
        tables = {row[0] for row in
                  check.execute("select name from sqlite_master where type='table'")}
    assert "open_deals_v1" not in tables


def test_the_migration_is_one_transaction(older_database, monkeypatch):
    """A failure anywhere inside it must leave the database exactly as it was.

    Python's sqlite3 commits DDL independently by default, which is what made the four
    statements four commits. The connection now owns its own transaction.
    """

    # The copy is made to fail from inside SQLite rather than by patching its driver, which
    # refuses to be patched: a destination column that is `not null` with no default and is
    # not one of the columns the copy names.
    monkeypatch.setattr(liabilities, "_SCHEMA", """
        create table if not exists open_deals (
            intent_id   text primary key,
            session_id  text not null,
            opened_at   text not null,
            tx_hash     text,
            deal_id     integer,
            detail      text not null default '{}',
            unfillable  text not null
        )
    """)
    # The copy refuses directly now. It used to be `insert or ignore`, which swallowed the
    # constraint and left the row count as the only thing that could notice; a null deal id
    # defeated that count too, which is how a live row could be dropped.
    with pytest.raises(sqlite3.IntegrityError):
        liabilities.rows(path=older_database)
    monkeypatch.undo()

    # The old table is still the one the next open finds, with its row intact.
    with sqlite3.connect(older_database) as check:
        columns = {row[1] for row in check.execute("pragma table_info(open_deals)")}
        assert "intent_id" not in columns, "a rolled-back migration left the new shape behind"
    assert liabilities.open_deals(path=older_database) == [41]


def test_a_second_opener_does_not_migrate_a_table_that_is_already_current(older_database):
    """The race that could delete a live row, driven across the decision boundary.

    Two processes opening a v1 index together could both decide a migration was needed. The
    first would do it and start serving; the second would then rename the first's *current*
    table as though it were old, and a liability written for a signed creation but not yet
    given its deal id would go with it. The deposit would be on chain with nothing pointing at
    it. The decision is made under the lock now, so the second opener re-reads and finds
    nothing to do.
    """

    # The first process migrates and then writes exactly the row that used to be lost: an
    # intent that has been signed and whose deal id is not known yet.
    assert liabilities.open_deals(path=older_database) == [41]
    liabilities.open_intent("quote-9:urgent", "a live session", path=older_database)
    liabilities.attach("quote-9:urgent", tx_hash="0xsigned", path=older_database)

    # The second process, still holding the view that a migration is required, opens the file.
    liabilities.rows(path=older_database)

    held = {row["intent_id"]: row for row in liabilities.rows(path=older_database)}
    assert "quote-9:urgent" in held, "a signed creation with no deal id yet was deleted"
    assert held["quote-9:urgent"]["tx_hash"] == "0xsigned"
    assert held["migrated:41"]["deal_id"] == 41


def test_a_current_table_is_never_renamed_even_when_an_old_one_is_present(tmp_path):
    """A leftover old table must not make the current one look old.

    This is the same race seen from the other side: the recovery path for a half-finished
    migration must copy out of the old table without touching the rows the new one already
    holds.
    """

    path = tmp_path / "liabilities.db"
    connection = sqlite3.connect(path)
    connection.execute(V1_SCHEMA.replace("open_deals", "open_deals_v1"))
    connection.execute(
        "insert into open_deals_v1 values (7, 'gone', '2026-09-09T00:00:00+00:00', '{}')"
    )
    connection.execute(
        """create table open_deals (intent_id text primary key, session_id text not null,
           opened_at text not null, tx_hash text, deal_id integer,
           detail text not null default '{}')"""
    )
    connection.execute(
        "insert into open_deals values ('live:1', 's', '2026-09-09T00:00:00+00:00',"
        " '0xsigned', null, '{}')"
    )
    connection.commit()
    connection.close()

    held = {row["intent_id"]: row for row in liabilities.rows(path=path)}

    assert set(held) == {"live:1", "migrated:7"}
    assert held["live:1"]["deal_id"] is None
    assert held["live:1"]["tx_hash"] == "0xsigned"


def test_a_stale_preflight_decision_is_overruled_inside_the_lock(older_database, monkeypatch):
    """The interleaving itself, forced rather than approximated.

    The two tests above reach the right outcome from a database that looks like the aftermath.
    Neither makes a caller arrive at the lock still believing a migration is needed, which is
    the actual race: the cheap look happens outside the lock and can be stale by the time the
    lock is held. Codex made that point about them and it is correct, so this holds the belief
    fixed at "yes" and checks the locked re-read overrules it.
    """

    # One opener migrates and writes a signed creation whose deal id is not known yet.
    assert liabilities.open_deals(path=older_database) == [41]
    liabilities.open_intent("quote-9:urgent", "a live session", path=older_database)
    liabilities.attach("quote-9:urgent", tx_hash="0xsigned", path=older_database)

    # The next one arrives at the lock still convinced there is work to do. There is not, and
    # acting on that belief is what renamed a current table and dropped the row above.
    monkeypatch.setattr(liabilities, "_needs_attention", lambda connection: True)
    liabilities.rows(path=older_database)
    monkeypatch.undo()

    held = {row["intent_id"]: row for row in liabilities.rows(path=older_database)}
    assert set(held) == {"migrated:41", "quote-9:urgent"}
    assert held["quote-9:urgent"]["tx_hash"] == "0xsigned"
    assert held["quote-9:urgent"]["deal_id"] is None

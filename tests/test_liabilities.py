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

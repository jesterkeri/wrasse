"""What the escrow is holding on this deployment's behalf, written down where a restart can find it.

The run objects, the session registry and the queue all live in process memory, and a restart
discards every one of them. The transaction ledger survives, but it records transactions rather
than deals: it can say that a `createDeal` was included and cannot say which deal id that
produced, because the id exists only in a log the ledger does not parse.

So a deal opened by a run that was interrupted had no index that could find it again. This file
is that index.

**The row is written before the transaction is signed, not after the deal id is known.** That
ordering is the whole point and it was wrong once. Writing after the id looks safe, because the
id is what the recovery needs, but the id only becomes knowable after the value has already
moved: `createDeal` is included, the buyer's ETH is in an Offered deal, and the receipt read
that turns that into an id is a separate call that can fail, or be interrupted, or be killed
with the process. A crash in that window left a deposit on chain with nothing durable pointing
at it. So a row is created when the intent is, carries the transaction hash as soon as there is
one, and gains the deal id when the receipt gives it up. A row with a hash and no id is exactly
what boot recovery has to resolve, and it can, because a hash is enough to re-read the receipt.

It is deliberately not the ledger and deliberately not a memory store. The ledger's job is
nonces and it must not grow a second responsibility. A memory store holds what an agent believes
about a counterparty, and an operational liability is not a belief about anybody.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

#: Beside the transaction ledger by default, because the two answer the same kind of question
#: about the same two wallets and an operator looking for one will look for the other.
LIABILITY_DB = Path(os.getenv("WRASSE_LIABILITY_DB", ".wrasse/liabilities.db"))

_SCHEMA = """
create table if not exists open_deals (
    intent_id   text primary key,
    session_id  text not null,
    opened_at   text not null,
    tx_hash     text,
    deal_id     integer,
    detail      text not null default '{}'
)
"""

_lock = threading.Lock()

#: The table an interrupted migration leaves behind. Recognised on every open, because a
#: half-finished upgrade must be finishable rather than merely unlikely.
_OLD_TABLE = "open_deals_v1"


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in
        connection.execute("select name from sqlite_master where type='table'").fetchall()
    }


@contextmanager
def _connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    # `isolation_level=None` hands transaction control to this code. Python's default mode
    # commits DDL independently of the statements around it, which turned the migration below
    # into four separate commits: a process killed after the rename left the rows in a table
    # nothing reads and the new table empty, which is the same lost deposit the migration was
    # written to prevent, reached by interruption instead of by design.
    destination = Path(path or LIABILITY_DB)
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(destination, timeout=30, isolation_level=None)
    try:
        connection.execute("pragma journal_mode=wal")
        _migrate(connection)
        connection.execute(_SCHEMA)
        yield connection
    finally:
        connection.close()


def _needs_attention(connection: sqlite3.Connection) -> bool:
    """A cheap unlocked look, used only to decide whether the locked look is worth taking.

    Safe to be wrong in one direction and only one. A false "yes" costs a lock and a re-read. A
    false "no" would be a missed migration, and cannot happen: this answers no only when the
    current shape is present and no old table exists, and the only thing that creates an old
    table is a migration, which only runs when the shape is not current.
    """

    tables = _tables(connection)
    if _OLD_TABLE in tables:
        return True
    columns = {
        row[1] for row in connection.execute("pragma table_info(open_deals)").fetchall()
    }
    return bool(columns) and "intent_id" not in columns


def _migrate(connection: sqlite3.Connection) -> None:
    """Bring an older index up to this shape, all of it or none of it.

    **The decision is made inside the transaction, not before it.** Deciding first and locking
    second was a race with a real cost: two processes could both conclude a migration was
    needed, the first would do it and start serving, and the second would then rename the
    first's *current* table as though it were old. A liability the first process had just
    written for a signed creation, still carrying no deal id because the receipt had not come
    back yet, would be dropped with it, and the deposit on chain would have nothing pointing at
    it. Codex reproduced that interleaving; this is the fix for it.

    The copy is an exact mapping and its proof is a count of rows, not of deal ids. The previous
    version used `insert or ignore` and compared deal ids, and both halves failed silently on a
    null id: the insert ignored it because the computed key was null, and `null not in (...)` is
    unknown rather than true, so the check passed over the row it was meant to catch.
    """

    if not _needs_attention(connection):
        return

    connection.execute("begin immediate")
    try:
        # Re-read under the lock. Another process may have finished the whole migration between
        # the cheap look above and this line, in which case there is nothing to do and doing it
        # anyway is what destroyed a row.
        tables = _tables(connection)
        columns = {
            row[1] for row in connection.execute("pragma table_info(open_deals)").fetchall()
        }
        stale = _OLD_TABLE in tables
        older = bool(columns) and "intent_id" not in columns
        if not (stale or older):
            connection.execute("rollback")
            return

        if older:
            connection.execute(f"alter table open_deals rename to {_OLD_TABLE}")
        connection.execute(_SCHEMA)

        source = connection.execute(
            f"select deal_id, session_id, opened_at, detail from {_OLD_TABLE}"
        ).fetchall()
        for deal_id, session_id, opened_at, detail in source:
            if deal_id is None:
                # Unnameable, and the only way one gets here is the race above. Refusing keeps
                # the table, and the next open migrates it once the shape is settled.
                raise RuntimeError(
                    f"{_OLD_TABLE} holds a row with no deal id, which this shape cannot name. "
                    "Refusing to drop the only record of whatever it points at."
                )
            connection.execute(
                "insert or replace into open_deals"
                "(intent_id, session_id, opened_at, tx_hash, deal_id, detail) "
                "values (?, ?, ?, null, ?, ?)",
                (f"migrated:{int(deal_id)}", session_id, opened_at, int(deal_id),
                 detail if detail is not None else "{}"),
            )

        moved = connection.execute(
            "select count(*) from open_deals where intent_id like 'migrated:%'"
        ).fetchone()[0]
        if moved != len(source):
            raise RuntimeError(
                f"{len(source)} rows were in {_OLD_TABLE} and {moved} arrived; refusing to "
                "drop the only record of the deals they name"
            )
        connection.execute(f"drop table {_OLD_TABLE}")
        connection.execute("commit")
    except Exception:
        connection.execute("rollback")
        raise


def open_intent(intent_id: str, session_id: str, *, detail: dict | None = None,
                path: Path | None = None) -> None:
    """Write down that this deployment is about to try to open a deal.

    Called before the transaction is signed. A row here with nothing else on it costs one
    delete if the send is refused, and is the only thing that can find a deposit if the send
    succeeds and everything after it does not.
    """

    with _lock, _connect(path) as connection:
        connection.execute(
            "insert or ignore into open_deals(intent_id, session_id, opened_at, detail) "
            "values (?, ?, ?, ?)",
            (str(intent_id), session_id, datetime.now(UTC).isoformat(),
             json.dumps(detail or {}, sort_keys=True)),
        )


def attach(intent_id: str, *, tx_hash: str | None = None, deal_id: int | None = None,
           path: Path | None = None) -> None:
    """Record what has become known about an intent, without ever clearing what already is."""

    with _lock, _connect(path) as connection:
        if tx_hash is not None:
            connection.execute(
                "update open_deals set tx_hash = ? where intent_id = ?",
                (str(tx_hash), str(intent_id)),
            )
        if deal_id is not None:
            connection.execute(
                "update open_deals set deal_id = ? where intent_id = ?",
                (int(deal_id), str(intent_id)),
            )


def discard(intent_id: str, *, path: Path | None = None) -> None:
    """Forget an intent that never reached a node, so nothing is escrowed behind it.

    Only safe for a send the ledger recorded as unbroadcast. Anything that reached a mempool
    keeps its row until the chain says what became of it.
    """

    with _lock, _connect(path) as connection:
        connection.execute(
            "delete from open_deals where intent_id = ? and deal_id is null", (str(intent_id),)
        )


def close(deal_id: int, *, path: Path | None = None) -> None:
    """Forget a deal that has reached a terminal state and had its value credited."""

    with _lock, _connect(path) as connection:
        connection.execute("delete from open_deals where deal_id = ?", (int(deal_id),))


def open_deals(session_id: str | None = None, *, path: Path | None = None) -> list[int]:
    """Every deal whose id is known and which is still recorded as open, oldest first."""

    return [int(row["deal_id"]) for row in rows(session_id, path=path)
            if row["deal_id"] is not None]


def unresolved(session_id: str | None = None, *, path: Path | None = None) -> list[dict]:
    """Intents that reached a node and whose deal id was never learned.

    This is the crash window made visible. A row here means a transaction may have created a
    deal that nothing in this process has ever named, and its hash is enough to find out.
    """

    return [row for row in rows(session_id, path=path)
            if row["deal_id"] is None and row["tx_hash"]]


def rows(session_id: str | None = None, *, path: Path | None = None) -> list[dict]:
    """Every recorded intent, oldest first.

    Without a session id this is the whole deployment, which is what a restart needs: the
    sessions that opened these deals no longer exist, and the money does.
    """

    with _lock, _connect(path) as connection:
        connection.row_factory = sqlite3.Row
        if session_id is None:
            found = connection.execute(
                "select * from open_deals order by opened_at, intent_id"
            ).fetchall()
        else:
            found = connection.execute(
                "select * from open_deals where session_id = ? order by opened_at, intent_id",
                (session_id,),
            ).fetchall()
    return [dict(row) for row in found]

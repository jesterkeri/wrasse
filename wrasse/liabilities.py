"""What the escrow is holding on this deployment's behalf, written down where a restart can find it.

The run objects, the session registry and the queue all live in process memory, and a restart
discards every one of them. The transaction ledger survives, but it records transactions rather
than deals: it can say that a `createDeal` was included and cannot say which deal id that
produced, because the id exists only in a log the ledger does not parse.

So a deal opened by a run that was interrupted had no index that could find it again. The refund
assembled its recovery list from whatever `Run` objects the current process happened to hold,
which after a restart is none, and the money stayed in a deal nobody would ever close. This file
is that index: one line per deal, written the moment the id is known, removed when the deal is
closed.

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
from pathlib import Path

#: Beside the transaction ledger by default, because the two answer the same kind of question
#: about the same two wallets and an operator looking for one will look for the other.
LIABILITY_DB = Path(os.getenv("WRASSE_LIABILITY_DB", ".wrasse/liabilities.db"))

_SCHEMA = """
create table if not exists open_deals (
    deal_id     integer primary key,
    session_id  text not null,
    opened_at   text not null,
    detail      text not null default '{}'
)
"""

_lock = threading.Lock()


@contextmanager
def _connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    destination = Path(path or LIABILITY_DB)
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(destination, timeout=30)
    try:
        connection.execute("pragma journal_mode=wal")
        connection.execute(_SCHEMA)
        yield connection
        connection.commit()
    finally:
        connection.close()


def record(deal_id: int, session_id: str, *, detail: dict | None = None,
           path: Path | None = None) -> None:
    """Write a deal down as open. Called as soon as the id is known, before anything else.

    Idempotent by deal id, because a retry that re-reads the same receipt must not produce a
    second row and a row that already exists is the truth this is trying to preserve.
    """

    from datetime import UTC, datetime

    with _lock, _connect(path) as connection:
        connection.execute(
            "insert or ignore into open_deals(deal_id, session_id, opened_at, detail) "
            "values (?, ?, ?, ?)",
            (int(deal_id), session_id, datetime.now(UTC).isoformat(),
             json.dumps(detail or {}, sort_keys=True)),
        )


def close(deal_id: int, *, path: Path | None = None) -> None:
    """Forget a deal that has reached a terminal state and had its value credited."""

    with _lock, _connect(path) as connection:
        connection.execute("delete from open_deals where deal_id = ?", (int(deal_id),))


def open_deals(session_id: str | None = None, *, path: Path | None = None) -> list[int]:
    """Every deal still recorded as open, oldest first.

    Without a session id this is the whole deployment, which is what a restart needs: the
    sessions that opened these deals no longer exist, and the money does.
    """

    with _lock, _connect(path) as connection:
        if session_id is None:
            rows = connection.execute(
                "select deal_id from open_deals order by deal_id"
            ).fetchall()
        else:
            rows = connection.execute(
                "select deal_id from open_deals where session_id = ? order by deal_id",
                (session_id,),
            ).fetchall()
    return [int(row[0]) for row in rows]

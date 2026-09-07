"""A private pair of memories per visitor, so one judge's run is not another judge's history.

**Why a copy rather than one shared pair.** The demonstration is that a memory changes what an
agent will agree to. That needs a *before*, and a before that every visitor sees identically.
One shared pair would mean the second judge starts from a store the first judge already taught,
so the terms they are shown as the starting point are not the terms the write-up describes, and
by the tenth visitor the opening position has drifted somewhere nobody chose.

The alternative was tempting and is worth recording as rejected: a single accumulating memory
is a *better story* about memory, and it is the story this project is actually making. It is
also unrepeatable, and a demo a judge cannot replay is one they cannot check.

**What a session does not isolate.** The two wallets, the escrow contract and the transaction
ledger are global and stay that way. The ledger is the thing that prevents a nonce gap on the
shared wallets, so copying it per session would recreate the exact defect it exists to prevent.
A session isolates what each side *remembers*, which is the only thing a visitor is allowed to
change.
"""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

#: Where session copies live. A volume in a deployment; a temporary directory in the tests.
SESSION_ROOT = Path(os.getenv("WRASSE_SESSION_ROOT", ".wrasse/sessions"))

#: How many sessions are kept before the oldest is deleted. Each pair is under a megabyte, so
#: this is a bound on disk rather than a bound on visitors; a returning visitor whose session
#: has been evicted is given a new one.
SESSION_LIMIT = int(os.getenv("WRASSE_SESSION_LIMIT", "200"))

#: How many settlements one session may execute. Both wallets are shared and faucet-funded, so
#: this is what stops one visitor from spending the demo.
RUNS_PER_SESSION = int(os.getenv("WRASSE_RUNS_PER_SESSION", "3"))


def copy_database(origin: Path, destination: Path) -> None:
    """Copy a SQLite database and everything committed to it.

    Every sidecar, not just the main file. SQLite writes through a write-ahead log, so a source
    whose log has not been checkpointed keeps its most recent rows in `<name>-wal`. Copying the
    `.db` alone loses them silently and the copy answers as an empty memory, which is
    indistinguishable from a cold start and is the more dangerous of the two because it looks
    like a working system that remembers nothing.

    The permission reset is the second half of the same lesson. `shutil.copy` carries the
    source's mode, and a source mounted read-only produces a copy that cannot be opened at all.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        sidecar = origin.with_name(origin.name + suffix)
        if not sidecar.is_file():
            continue
        target = destination.with_name(destination.name + suffix)
        shutil.copy(sidecar, target)
        target.chmod(0o600)


@dataclass
class Session:
    """One visitor's private pair, and what they have spent."""

    session_id: str
    directory: Path
    created_at: str
    runs: int = 0
    paths: dict[str, Path] = field(default_factory=dict)

    def view(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "runs_used": self.runs,
            "runs_allowed": RUNS_PER_SESSION,
        }


class Sessions:
    """Mint, find and evict sessions. Every method is safe to call from any request thread."""

    def __init__(
        self,
        source: dict[str, Path],
        *,
        root: Path | None = None,
        limit: int | None = None,
        runs_per_session: int | None = None,
    ) -> None:
        self._source = source
        self._root = root or SESSION_ROOT
        self._limit = limit if limit is not None else SESSION_LIMIT
        self._runs = runs_per_session if runs_per_session is not None else RUNS_PER_SESSION
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._order: list[str] = []

    def create(self) -> Session:
        """A fresh pair, copied from the warm source, with an identifier this process minted.

        The identifier is minted here and every later request is looked up in this table rather
        than joined onto a path. A visitor-supplied string that reaches the filesystem is a
        directory-traversal bug however carefully it is escaped, and a lookup that can only
        succeed for something already issued has no such shape.
        """

        session_id = uuid.uuid4().hex
        directory = self._root / session_id
        paths = {}
        for role, origin in self._source.items():
            destination = directory / f"{role}-memory.db"
            copy_database(origin, destination)
            paths[role] = destination
        session = Session(
            session_id=session_id,
            directory=directory,
            created_at=datetime.now(UTC).isoformat(),
            paths=paths,
        )
        with self._lock:
            self._sessions[session_id] = session
            self._order.append(session_id)
            self._evict()
        return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def spend(self, session: Session) -> None:
        """Record a run against a session, or refuse when it has used its allowance."""

        with self._lock:
            if session.runs >= self._runs:
                raise PermissionError(
                    f"this session has run {session.runs} settlements, which is its limit. "
                    "Both wallets are shared and faucet-funded. Start a new session to run more."
                )
            session.runs += 1

    def _evict(self) -> None:
        """Delete the oldest sessions past the limit. Called with the lock held."""

        while len(self._order) > self._limit:
            oldest = self._order.pop(0)
            session = self._sessions.pop(oldest, None)
            if session is None:
                continue
            shutil.rmtree(session.directory, ignore_errors=True)

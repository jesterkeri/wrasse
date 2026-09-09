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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

#: Where session copies live. A volume in a deployment; a temporary directory in the tests.
SESSION_ROOT = Path(os.getenv("WRASSE_SESSION_ROOT", ".wrasse/sessions"))

#: How many sessions are kept before the oldest is deleted. Each pair is under a megabyte, so
#: this is a bound on disk rather than a bound on visitors; a returning visitor whose session
#: has been evicted is given a new one.
SESSION_LIMIT = int(os.getenv("WRASSE_SESSION_LIMIT", "200"))

#: How many settlements one session may execute. The point of allowing several is that each
#: one teaches both memories, so a visitor can watch terms move across a history they built
#: rather than inferring it from one deal.
RUNS_PER_SESSION = int(os.getenv("WRASSE_RUNS_PER_SESSION", "5"))


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

    if not origin.is_file():
        # Refused rather than skipped. A missing source used to `continue` here and the copy
        # still returned a session, so the visitor got two empty stores presented as the
        # seeded history: a cold start wearing the clothes of a warm one, which is the failure
        # every other check in this file exists to prevent. It happens on a fresh deployment,
        # where the working copies do not exist until the first quote has created them.
        raise FileNotFoundError(
            f"{origin} does not exist, so there is nothing to copy. A session copied from a "
            "source that is not there would answer as a memory holding nothing, which is "
            "indistinguishable from a genuine cold start and is the more dangerous of the two."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        sidecar = origin.with_name(origin.name + suffix)
        target = destination.with_name(destination.name + suffix)
        if not sidecar.is_file():
            # A sidecar the source does not have must not survive at the destination. Nothing
            # reaches this on a fresh directory, but a session root reused across a reseed
            # would otherwise keep a write-ahead log belonging to a different database.
            target.unlink(missing_ok=True)
            continue
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
    #: Set when the escrow has actually been emptied back into the wallets, never before. A
    #: session refunds at the end rather than after each run, because the credits sitting in
    #: the escrow are the least interesting thing about a run and collecting them between runs
    #: would put two transactions nobody asked for in the middle of the story.
    #:
    #: `finishing` is the separate flag that stops a second press queueing a second withdrawal
    #: while the first is still in flight. Collapsing the two into one was the defect: a refund
    #: that failed left the session marked refunded, so it could never be retried and the money
    #: it did not collect stayed in the escrow behind a page reporting success.
    refunded: bool = False
    finishing: bool = False
    refund_run_id: str | None = None

    def view(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "runs_used": self.runs,
            "runs_allowed": RUNS_PER_SESSION,
            "runs_left": max(0, RUNS_PER_SESSION - self.runs),
            "refunded": self.refunded,
            "finishing": self.finishing,
            "refund_run_id": self.refund_run_id,
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
        busy: Callable[["Session"], bool] | None = None,
        on_evict: Callable[[str], None] | None = None,
    ) -> None:
        self._source = source
        self._root = root or SESSION_ROOT
        self._limit = limit if limit is not None else SESSION_LIMIT
        self._runs = runs_per_session if runs_per_session is not None else RUNS_PER_SESSION
        #: Asked before deleting anything. Injected rather than imported, because the queue is
        #: the thing that knows a run is in flight and this module must not depend on it.
        self._busy = busy
        self._on_evict = on_evict
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

    def begin_finishing(self, session: Session) -> bool:
        """Claim the right to refund this session, under the same lock that admits runs.

        Returns whether this caller is the one that claimed it. Sharing the lock with `spend`
        is the point: without it a settlement could be admitted between a refund deciding there
        was nothing left to collect and that refund running, and the deal it then opened would
        have nothing scheduled to close it.
        """

        with self._lock:
            if session.finishing or session.refunded:
                return False
            session.finishing = True
            return True

    def abandon_finishing(self, session: Session) -> None:
        """Give the claim back, for a refund that failed and may be retried."""

        with self._lock:
            session.finishing = False
            session.refund_run_id = None

    def spend(self, session: Session) -> None:
        """Record a run against a session, or refuse when it has used its allowance."""

        with self._lock:
            # The allowance first, because spending it is what triggers the refund, so a
            # visitor who used all five would otherwise be told they had finished the session
            # when what they did was reach its limit. Both are refusals; only one is the
            # reason.
            if session.runs >= self._runs:
                raise PermissionError(
                    f"this session has run {session.runs} settlements, which is its limit. "
                    "Both wallets are shared and faucet-funded. Start a new session to run more."
                )
            # A finished session cannot start another settlement. Its refund has already
            # collected, or is collecting, whatever the escrow was holding, so a later run
            # would credit the escrow again with nothing scheduled to empty it.
            if session.finishing or session.refunded:
                raise PermissionError(
                    "this session has been finished and its escrow collected. A settlement "
                    "started now would leave its deposit behind. Start a new session."
                )
            session.runs += 1

    def _evict(self) -> None:
        """Delete the oldest sessions past the limit, skipping any that are still busy.

        Called with the lock held.

        A session is busy while a run of its own is queued or executing, and while it is owed a
        refund it has not collected. Deleting one of those takes the memory databases out from
        under a worker mid-run and leaves the escrow holding a deposit whose session paths no
        longer exist. A public endpoint reaches this with session creation alone, so it is not
        a rare interleaving: it is one stranger past the limit.

        A busy session keeps its place at the front of the queue rather than being skipped
        permanently, so it is reconsidered on the next creation and evicted once it is idle.
        """

        keep: list[str] = []
        while len(self._order) > self._limit:
            oldest = self._order.pop(0)
            session = self._sessions.get(oldest)
            if session is None:
                continue
            if self._busy is not None and self._busy(session):
                keep.append(oldest)
                if len(keep) + len(self._order) <= self._limit:
                    break
                continue
            self._sessions.pop(oldest, None)
            if self._on_evict is not None:
                # Whatever else owns resources for this session has to let go at the same
                # moment its files do. A cached pair of open stores outlives its own database
                # otherwise, and holds a file description on a path that no longer exists.
                self._on_evict(oldest)
            shutil.rmtree(session.directory, ignore_errors=True)
        self._order = keep + self._order

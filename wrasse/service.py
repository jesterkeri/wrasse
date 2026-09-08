"""The hosted service: a quote anyone can read, and a settlement anyone can perform.

**The rule the quoting surface keeps.** Quoting holds no keys, signs nothing, and writes no
receipt, no learned dimension and no outcome. Everything in that half follows from it, and
every constraint below has a reason attached so none of them get traded away at two in the
morning.

The claim used to read "writes nothing", and an adversarial pass showed that is false on a
cold deployment. Opening a store that does not exist writes its identity record, and the first
quote writes the provider's persona commitment when it is absent, so a fresh `memory=off` pair
creates a database, a lock file and two metadata entities. None of that touches a term or a
receipt, and stating it as "writes nothing" was still a guarantee the code does not keep.

So the metadata is written once at startup, before anything is served, and the health endpoint
says what is actually true rather than what is convenient.

**The quoting surface still keeps that rule, and a second surface does not.** This module used
to argue that the service should never sign, on three grounds: concurrent visitors serialise
behind one unresolved transaction per wallet and would need a queue, a receipt lost to a closed
tab holds that wallet and ends the demo for everyone after the first, and a host that signs is
a host holding a key. Two of those are now answered and one is accepted, and the argument is
recorded in `executor` rather than deleted, because a reversal with no reason attached is how a
constraint gets traded away twice.

The short of it: a demo a judge cannot perform is a demo a judge does not believe, and that is
the requirement this build exists to meet. So execution lives behind `WRASSE_ENABLE_EXECUTION`,
it is off by default, and `/api/health` reports which of the two deployments this one is rather
than making a claim that is true only sometimes.

**Why quoting still needs no worker, no queue and no locks.** Those were consequences of
writing, and quoting does not write: `_bilateral_quote` performs no ingest, no commit, no
insert and no status change; it reads both stores under their locks and returns. Two visitors
asking for a quote at the same moment are two readers, which is what the stores already permit.
Per-session copies are the one exception, and they exist for a different reason: a visitor who
executes teaches a memory, and every visitor is entitled to the same starting point.

**Why it makes no network calls.** A quote is memory plus arithmetic. The only RPC on the
`policy` path is the chain-time observation, and supplying `reference_timestamp` and
`accept_by` returns before a provider is ever constructed. The document says so itself: such a
quote is labelled `executable: false`, which is honest rather than weaker. A live quote stops
being executable minutes after it is written and no hosted answer could keep that claim.

**Why the stores are opened once, at startup, rather than per request.** Every setting in
`cli` is read from the environment at call time, deliberately, so `load_dotenv` can reach it.
That is right for a command and wrong here: `os.environ` is shared by every in-flight request
and two would interleave. Both pairs are opened at import and passed explicitly.

**Read-only mounts, and what is actually achievable.** The intent was to mount both databases
read-only so the guarantee came from the operating system rather than only from the code path.
That does not work, and the reason is worth writing down rather than discovering at deploy
time. The Sibyl SDK opens SQLite read-write and sets WAL, and `store._file_lock` creates its
lock file with `open(path, "w")`. A genuinely read-only file fails to open at all, with
"cannot open the buyer memory at ...".

So the guarantee is one step back and still real: mount the *source* read-only, and let the
service copy it into a writable working directory at startup with `WRASSE_MEMORY_SOURCE_DIR`.
The deployed artifact cannot be mutated by anything, the working copy is ephemeral and is
recreated on every restart, and `test_the_service_writes_nothing_to_the_memory_it_reads` shows
by file hash that nothing writes to it anyway. What is lost is the kernel enforcing a property
the tests already enforce; what is kept is that no visitor can change what the next one sees.
"""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from sibyl_memory_client import MemoryClient

from . import cli, executor, sessions, simulate as simulation
from .evidence import CHAIN_EVENT_CATEGORY
from .page import page_view
from .store import persona_digest
from .negotiation import BASELINE_BOUNDS

#: The two memory settings the page toggles between. `on` is what the two agents actually
#: remember; `off` is the same engine against empty stores, which is the control that makes the
#: difference attributable to memory rather than to anything else.
WARM, COLD = "on", "off"

#: Where each pair lives. The cold pair is created empty on first open and stays empty: nothing
#: on this path ingests, so there is nothing to fill it.
_PATHS = {
    WARM: {
        "buyer": Path(os.getenv("WRASSE_BUYER_MEMORY_PATH", ".wrasse/buyer-memory.db")),
        "provider": Path(os.getenv("WRASSE_PROVIDER_MEMORY_PATH", ".wrasse/provider-memory.db")),
    },
    COLD: {
        "buyer": Path(os.getenv("WRASSE_COLD_BUYER_MEMORY_PATH", ".wrasse/cold/buyer-memory.db")),
        "provider": Path(
            os.getenv("WRASSE_COLD_PROVIDER_MEMORY_PATH", ".wrasse/cold/provider-memory.db")
        ),
    },
}

#: A read-only source, copied once into a writable working directory at startup. Unset in
#: development, where the databases beside the repository are the working copies already.
SOURCE_DIR = os.getenv("WRASSE_MEMORY_SOURCE_DIR")

#: Whether this deployment signs. Off by default, so the read-only deployment stays a real
#: option and its health answer stays true. A deployment with this on holds two Base Sepolia
#: keys and says so; one with it off refuses every execute route with the reason.
EXECUTION = os.getenv("WRASSE_ENABLE_EXECUTION") == "1"

#: A global ceiling on settlements per process lifetime, under the per-session one. Both
#: wallets are faucet-funded and the page is public, so a griefer with a fresh session for
#: every run is cheaper than the wallets are.
RUN_CEILING = int(os.getenv("WRASSE_TOTAL_RUN_CEILING", "400"))


def prepare_working_copies() -> None:
    """Copy the read-only source into the working paths, once, before anything opens them.

    Deliberately not a fallback: if the source is configured and a file is missing, that is a
    broken deployment and it should fail loudly rather than serve a cold start that looks like
    a successful one. A quote against empty memories is a legitimate answer to `memory=off` and
    a catastrophic one to `memory=on`, and nothing downstream could tell them apart.
    """

    if SOURCE_DIR is None:
        return
    source = Path(SOURCE_DIR)
    for role, path in _PATHS[WARM].items():
        origin = source / f"{role}-memory.db"
        if not origin.is_file():
            raise RuntimeError(
                f"{origin} is missing. The source directory is configured, so serving without "
                "it would answer `memory=on` from an empty store, which reads as a system that "
                "remembers nothing rather than as a broken mount."
            )
        path.parent.mkdir(parents=True, exist_ok=True)

        # Every sidecar, not just the main file. SQLite writes through a write-ahead log, so a
        # source whose log has not been checkpointed keeps its most recent rows in
        # `<name>-wal`. Copying the `.db` alone loses them silently and the service answers
        # from what looks like an empty memory: the same indistinguishable failure the missing
        # check above exists to prevent, arriving through a door that check cannot see.
        for suffix in ("", "-wal", "-shm"):
            sidecar = origin.with_name(origin.name + suffix)
            if not sidecar.is_file():
                continue
            destination = path.with_name(path.name + suffix)
            shutil.copy(sidecar, destination)
            # `shutil.copy` carries the source's permission bits and the source is deliberately
            # read-only. Without this the working copy inherits that and cannot be opened
            # either, which is the exact failure the copy exists to avoid.
            destination.chmod(0o600)

    # And then check the copy actually carried a memory, rather than trusting that it did.
    #
    # A source is configured only when someone means to supply the two memories, so an empty
    # result is a broken mount and never a cold start. Distinguishing them matters because they
    # are identical from the outside: both answer `memory=on` with no receipts, and only one is
    # fixed by restarting. The likeliest cause is a mount that carried `<name>.db` without
    # `<name>.db-wal`, which loses every row still in the log and reports nothing.
    for role, path in _PATHS[WARM].items():
        held = MemoryClient.local(path).list_entities(CHAIN_EVENT_CATEGORY, limit=1)
        if not held:
            raise RuntimeError(
                f"{path} holds no receipts after copying from {source}. A configured source "
                "means the memories were meant to be supplied, so this is a broken mount "
                "rather than a cold start. The usual cause is copying the database without "
                "its write-ahead log, which loses every row still in it and says nothing."
            )


app = FastAPI(
    title="wrasse quote",
    description="Two independently held memories, two positions, one deterministic settlement.",
)

_stores: dict[str, dict[str, Any]] = {}


def _open(memory: str) -> dict[str, Any]:
    """Open one pair once and keep it. Opening per request would reread the identity record on
    every call and, worse, would make the store objects short-lived enough that two requests
    could hold two objects for one file. The file lock is per open description, so that
    deadlocks rather than excluding."""

    if memory not in _stores:
        if not _stores:
            prepare_working_copies()
            _initialise_metadata()
        _stores[memory] = cli._open_stores(
            buyer=cli._required_env("WRASSE_BUYER_ADDRESS"),
            provider=cli._required_env("WRASSE_PROVIDER_A_ADDRESS"),
            paths=_PATHS[memory],
        )
    return _stores[memory]


#: The page, served from the same origin as the API so it needs no CORS and a share link
#: carries both. Absent in development and in the tests, where the service is exercised
#: directly; present in a deployment, where the whole point is that one URL is the demo.
WEB_ROOT = Path(os.getenv("WRASSE_WEB_ROOT", "web"))


def _initialise_metadata() -> None:
    """Write the identity record and persona commitment once, before anything is served.

    A store that does not exist yet gets both written on its first open, and the first quote
    writes the persona commitment when it is absent. Doing it here rather than inside a request
    means the serving path really is read-only, so "writes no receipt, dimension or outcome" is
    a property of every request rather than of every request after the first.

    The cold pair is the one this matters to. It is created empty on purpose and stays empty:
    nothing on this path ingests, so there is nothing to fill it.
    """

    digest, persona = persona_digest(cli._persona_path())
    for paths in _PATHS.values():
        stores = cli._open_stores(
            buyer=cli._required_env("WRASSE_BUYER_ADDRESS"),
            provider=cli._required_env("WRASSE_PROVIDER_A_ADDRESS"),
            paths=paths,
        )
        stores["provider"].commit_persona(name=persona["name"], digest=digest)


@app.get("/api/health")
def health() -> dict:
    """Enough to tell a deploy from a corpse, and nothing that touches a wallet."""

    return {
        "ok": True,
        "engine_version": cli.ENGINE_VERSION,
        "chain_id": cli._chain_id(),
        "contract_address": cli._escrow_address(),
        # Whether this deployment signs is a property of the deployment, not of the code, so it
        # is read rather than asserted. A single hard-coded `false` here was true of the
        # read-only build and would be a lie the moment anyone set the variable.
        "signs": EXECUTION,
        "holds_keys": EXECUTION,
        "execution_enabled": EXECUTION,
        # Not `writes: false`, which was untrue on a cold deployment. Store identity and the
        # persona commitment are written when a store is first opened, and both happen at
        # startup rather than while serving.
        #
        # On the quoting path this is still exactly true. Execution writes an outcome by
        # definition, and only ever into the session copy belonging to the visitor who asked
        # for it.
        "quote_writes_receipts_or_outcomes": False,
        "queue_depth": _queue().depth() if EXECUTION else 0,
    }


# `def`, not `async def`, and deliberately. Every call underneath is synchronous blocking I/O,
# including an `fcntl` lock. An `async def` handler would hold the event loop across it and
# freeze every other request in the process.
@app.get("/api/quote")
def quote(
    memory: str = Query(WARM, pattern="^(on|off)$"),
    price_wei: int = Query(100_000_000_000_000),
    provider_bond_bps: int = Query(500),
    service_window: int = Query(600),
    payout_delay: int = Query(1_800),
    reference_timestamp: int = Query(1_788_666_320),
    accept_by: int = Query(1_788_666_920),
    session_id: str | None = Query(None),
) -> JSONResponse:
    """One quote, as the document the command produces, not a reshaping of it.

    `quote_document` is the same function `wrasse policy` calls, so the hosted answer and the
    terminal answer are the same bytes from the same code rather than two assemblies that agree
    today. A layer that renamed or recomputed a field would be one more place the displayed
    terms could drift from the produced ones, which is the whole thing this project is about.

    Both times are supplied rather than observed, so this makes no network call at all and a
    link reproduces its own result.
    """

    return _quote(
        memory=memory, price_wei=price_wei, provider_bond_bps=provider_bond_bps,
        service_window=service_window, payout_delay=payout_delay,
        reference_timestamp=reference_timestamp, accept_by=accept_by,
        session_id=session_id,
    )


def _quote(
    *,
    memory: str,
    price_wei: int,
    provider_bond_bps: int,
    service_window: int,
    payout_delay: int,
    reference_timestamp: int = 1_788_666_320,
    accept_by: int = 1_788_666_920,
    session_id: str | None = None,
) -> JSONResponse:
    """One implementation, two request shapes. Neither reshapes the document.

    A session quotes against its own warm pair, which is the whole point of having one: after a
    run has taught those two stores, the same request returns terms that have moved. Without a
    session the shared pair answers, and it is the same pair for everybody and stays untaught.

    The cold pair is shared either way. Nothing on any path writes to it, so there is nothing a
    visitor could do to it that another visitor would see.
    """

    if session_id is not None and memory == WARM:
        stores = _open_session(_session_or_404(session_id))
    else:
        stores = _open(memory)

    basis = cli.TimeBasis(reference_timestamp, accept_by, None, 0)
    try:
        document = cli.quote_document(
            buyer=cli._required_env("WRASSE_BUYER_ADDRESS"),
            provider=cli._required_env("WRASSE_PROVIDER_A_ADDRESS"),
            base_price_wei=price_wei,
            base_bond_bps=provider_bond_bps,
            service_window=service_window,
            payout_delay=payout_delay,
            basis=basis,
            executability=cli._executability(_SuppliedTime(), basis),
            inclusion_margin=0,
            stores=stores,
        )
    except RuntimeError as error:
        # The baseline domain is refused by the writer before anything is produced. The page
        # prints the bound under the field, so it is returned rather than only described.
        raise HTTPException(
            status_code=422,
            detail={
                "error": str(error),
                "baseline_bounds": {
                    field: list(bounds) for field, bounds in BASELINE_BOUNDS.items()
                },
            },
        ) from error

    return JSONResponse(page_view(document, memory=memory == WARM))


class Baseline(BaseModel):
    """The four numbers an operator chooses before either memory is consulted.

    Named exactly as the page sends them and as the document prints them back, because a
    rename here is a place the displayed baseline and the quoted one can differ.
    """

    price_wei: int = Field(default=100_000_000_000_000)
    provider_bond_bps: int = Field(default=500)
    service_window: int = Field(default=600)
    payout_delay: int = Field(default=1_800)


class QuoteRequest(BaseModel):
    """What the page posts. `memory` is a boolean there, `on`/`off` in the query form.

    Both spellings reach the same code. The page posts, because it sends four numbers and a
    flag; a share link uses the query form, because a link has to carry its own state.
    """

    baseline: Baseline = Field(default_factory=Baseline)
    memory: bool = True
    session_id: str | None = None


@app.post("/api/quote")
def quote_post(request: QuoteRequest = Body(default_factory=QuoteRequest)) -> JSONResponse:
    """The shape the page sends. Same function underneath as the query form."""

    return _quote(
        memory=WARM if request.memory else COLD,
        price_wei=request.baseline.price_wei,
        provider_bond_bps=request.baseline.provider_bond_bps,
        service_window=request.baseline.service_window,
        payout_delay=request.baseline.payout_delay,
        session_id=request.session_id,
    )


# ----------------------------------------------------------------------------------------
# The second surface: performing the settlement rather than reading it.
# ----------------------------------------------------------------------------------------
#
# Everything below signs. It is reachable only when `WRASSE_ENABLE_EXECUTION` is set, it runs
# every chain step in a subprocess with its own environment, and it serialises every run behind
# one worker because the two wallets are shared. The reasoning for all three lives in
# `executor`, next to the code it constrains.

_QUEUE: executor.Queue | None = None
_SESSIONS: sessions.Sessions | None = None
_STARTED = 0
_START_LOCK = threading.Lock()

#: One store pair per session, opened once. Opening per request would put two open file
#: descriptions on one database inside one process, and `fcntl` locks belong to a description
#: rather than to a process, so the second would block on the first instead of excluding
#: another process. That is a deadlock, not a mutual exclusion.
_session_stores: dict[str, dict[str, Any]] = {}


def _queue() -> executor.Queue:
    """The worker, started on first use rather than at import.

    At import it would start a thread in every test process and in every tool that merely reads
    this module, which is how a background thread ends up outliving the thing that wanted it.
    """

    global _QUEUE
    if _QUEUE is None:
        _QUEUE = executor.Queue()
    return _QUEUE


def _session_registry() -> sessions.Sessions:
    global _SESSIONS
    if _SESSIONS is None:
        _SESSIONS = sessions.Sessions(_PATHS[WARM])
    return _SESSIONS


def _require_execution() -> None:
    if not EXECUTION:
        raise HTTPException(
            status_code=503,
            detail=(
                "this deployment is the read-only one: it holds no keys and signs nothing. "
                "The settlement it quotes is performed by a deployment with execution enabled."
            ),
        )


def _open_session(session: sessions.Session) -> dict[str, Any]:
    """This session's own pair, opened once and kept.

    A run teaches these stores from a subprocess, and this object sees that write because
    SQLite hands a reader the latest committed snapshot at the start of each read. What it must
    not do is open a second description on the same file while the first is alive.
    """

    if session.session_id not in _session_stores:
        _session_stores[session.session_id] = cli._open_stores(
            buyer=cli._required_env("WRASSE_BUYER_ADDRESS"),
            provider=cli._required_env("WRASSE_PROVIDER_A_ADDRESS"),
            paths=session.paths,
        )
    return _session_stores[session.session_id]


def _session_or_404(session_id: str) -> sessions.Session:
    session = _session_registry().get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "no such session. Sessions are held in memory and the oldest are evicted, so a "
                "link from a previous deployment will not resolve. Start a new one."
            ),
        )
    return session


@app.post("/api/session")
def open_session() -> dict:
    """A private copy of both memories, so this visitor's run is theirs alone."""

    _require_execution()
    session = _session_registry().create()
    return {**session.view(), "queue_depth": _queue().depth()}


class ExecuteRequest(BaseModel):
    """Which profile to settle, against which baseline, in whose session."""

    session_id: str
    profile: str = Field(pattern="^(urgent|budget|sensitive)$")
    baseline: Baseline = Field(default_factory=Baseline)


@app.post("/api/execute")
def execute(request: ExecuteRequest) -> dict:
    """Queue one real settlement on Base Sepolia. Returns immediately with something to poll.

    The response is a run id and nothing else that matters, because the interesting part takes
    two minutes and an HTTP request that waits that long is a request that times out in a
    proxy nobody controls.
    """

    global _STARTED
    _require_execution()
    session = _session_or_404(request.session_id)

    with _START_LOCK:
        if _STARTED >= RUN_CEILING:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"this deployment has run its {RUN_CEILING} settlements. Both wallets are "
                    "faucet-funded and shared; the quote surface is unaffected."
                ),
            )
        _STARTED += 1

    try:
        _session_registry().spend(session)
    except PermissionError as error:
        with _START_LOCK:
            _STARTED -= 1
        raise HTTPException(status_code=429, detail=str(error)) from error

    run_id = uuid.uuid4().hex
    workdir = session.directory / "runs" / run_id
    workdir.mkdir(parents=True, exist_ok=True)
    run = executor.Run(
        run_id=run_id,
        session_id=session.session_id,
        profile=request.profile,
        baseline=request.baseline.model_dump(),
        paths=session.paths,
        workdir=workdir,
    )
    _queue().submit(run)
    return {"run_id": run_id, "queue_position": _queue().position(run_id)}


@app.get("/api/run/{run_id}")
def run_status(run_id: str) -> dict:
    """Per-step progress, transaction hashes and a link to each one on the explorer."""

    _require_execution()
    run = _queue().get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    return run.view(position=_queue().position(run_id))


class SimulateRequest(BaseModel):
    """A baseline and a history the visitor chose, rather than one that happened."""

    baseline: Baseline = Field(default_factory=Baseline)
    #: Outcomes in the order they occurred. An empty list is two strangers, which is the
    #: control the rest of the simulation is read against.
    history: list[str] = Field(default_factory=list)


@app.post("/api/simulate")
def simulate(request: SimulateRequest) -> JSONResponse:
    """What these two agents would settle on, given a history you set.

    Real engine, real settlement rule, real persona, real ontology, hypothetical outcomes. It
    is the same document writer the live quote uses, so the two cannot drift: a simulation that
    predicted something the live path would not produce would be worse than no simulation.

    Stateless on purpose. The history arrives with every request, so clearing it is a client
    deleting a list rather than a server forgetting something, and two visitors simulating at
    once cannot see each other's history.
    """

    stores = _open(WARM)
    try:
        quote = simulation.simulated_quote(
            stores=stores,
            buyer=cli._required_env("WRASSE_BUYER_ADDRESS"),
            provider=cli._required_env("WRASSE_PROVIDER_A_ADDRESS"),
            outcomes=request.history,
            base_price_wei=request.baseline.price_wei,
            base_bond_bps=request.baseline.provider_bond_bps,
            base_service_window=request.baseline.service_window,
            base_payout_delay=request.baseline.payout_delay,
            persona=cli._provider_persona(stores["provider"]),
            quote_class=cli.BilateralQuote,
        )
    except simulation.SimulationRefused as error:
        raise HTTPException(
            status_code=422,
            detail={"error": str(error), "outcomes": list(simulation.OUTCOMES)},
        ) from error

    basis = cli.TimeBasis(1_788_666_320, 1_788_666_920, None, 0)
    try:
        document = cli.quote_document(
            buyer=cli._required_env("WRASSE_BUYER_ADDRESS"),
            provider=cli._required_env("WRASSE_PROVIDER_A_ADDRESS"),
            base_price_wei=request.baseline.price_wei,
            base_bond_bps=request.baseline.provider_bond_bps,
            service_window=request.baseline.service_window,
            payout_delay=request.baseline.payout_delay,
            basis=basis,
            executability=cli._executability(_SuppliedTime(), basis),
            inclusion_margin=0,
            stores=stores,
            quote=quote,
        )
    except RuntimeError as error:
        raise HTTPException(
            status_code=422,
            detail={
                "error": str(error),
                "baseline_bounds": {
                    field: list(bounds) for field, bounds in BASELINE_BOUNDS.items()
                },
            },
        ) from error

    body = page_view(document, memory=bool(request.history))
    # Said in the response as well as in the module, because this is the boundary a reader
    # crosses. A document with a policy hash and no marking is one somebody will eventually
    # try to sign, and the hash over a history that never happened is a real hash of a
    # fiction.
    body["simulated"] = True
    body["history"] = list(request.history)
    body["outcomes_available"] = list(simulation.OUTCOMES)
    return JSONResponse(body)


class _SuppliedTime:
    """`_executability` reads one field off the argparse namespace. This is that field."""

    inclusion_margin = 0


# Mounted last, so every `/api/...` route above wins and the page only catches what is left.
# A missing directory is not an error: the service is useful without a page, and the tests
# exercise it that way.
if WEB_ROOT.is_dir():

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")

    app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")

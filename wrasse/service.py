"""The hosted quote service: one route, no keys, nothing written.

**The rule this module exists to keep.** The service holds no keys, signs nothing and writes
nothing. Everything else here follows from it, and every constraint below has a reason attached
so none of them get traded away at two in the morning.

An earlier design had this service driving transactions so a judge could execute a deal from
the page. That reinherits three problems the read-only split deletes outright. The ledger
permits one unresolved transaction per wallet, so concurrent visitors serialise behind each
other and need a queue. A receipt lost to a closed tab holds that wallet, which ends the demo
for every judge after the first. And a host that signs is a host holding a key.

So settlement stays on the operator's machine, driven live. What is hosted is the part that
carries the argument: two memories, two positions, and a deterministic rule that resolves them
or refuses. That part reads and returns.

**Why this needs no worker, no queue, no locks and no per-session copies.** All four were
consequences of writing. `_bilateral_quote` performs no ingest, no commit, no insert and no
status change; it reads both stores under their locks and returns. Two visitors asking for a
quote at the same moment are two readers, which is what the stores already permit.

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
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import cli
from .page import page_view
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
        shutil.copy(origin, path)
        # `shutil.copy` carries the source's permission bits, and the source is deliberately
        # read-only. Without this the working copy inherits that and the store cannot open it
        # either, which is the exact failure the copy exists to avoid.
        path.chmod(0o600)


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


@app.get("/api/health")
def health() -> dict:
    """Enough to tell a deploy from a corpse, and nothing that touches a wallet."""

    return {
        "ok": True,
        "engine_version": cli.ENGINE_VERSION,
        "chain_id": cli._chain_id(),
        "contract_address": cli._escrow_address(),
        "signs": False,
        "writes": False,
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
) -> JSONResponse:
    """One implementation, two request shapes. Neither reshapes the document."""

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
            stores=_open(memory),
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


@app.post("/api/quote")
def quote_post(request: QuoteRequest = Body(default_factory=QuoteRequest)) -> JSONResponse:
    """The shape the page sends. Same function underneath as the query form."""

    return _quote(
        memory=WARM if request.memory else COLD,
        price_wei=request.baseline.price_wei,
        provider_bond_bps=request.baseline.provider_bond_bps,
        service_window=request.baseline.service_window,
        payout_delay=request.baseline.payout_delay,
    )


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

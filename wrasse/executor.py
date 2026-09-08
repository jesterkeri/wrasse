"""Driving one real settlement from the page, on the operator's two wallets.

**Why this exists at all.** The quote service reads two memories and returns a settlement, and
a reader who has not seen the chain has no reason to believe any of it happened. The demo this
project is judged on is not a page describing a deal; it is a stranger pressing a button and
watching four transactions land on Base Sepolia and both memories change afterwards. So the
host has to be able to sign.

**What that costs, stated rather than hidden.** `service` was written read-only and its
docstring argued the split was worth keeping. Two of the three reasons it gave are now answered
and one is accepted:

  * *Concurrent visitors serialise behind one unresolved transaction per wallet.* Answered by
    construction: one worker, one run at a time, and a queue position reported to everyone
    waiting. Serialising is the correct behaviour for two shared wallets, not a defect to
    engineer around.
  * *A receipt lost to a closed tab holds that wallet forever.* This was true and is now fixed.
    Three separate gates read a deal action's absent deadline as an expired one; all three are
    corrected, an unbroadcast send releases its nonce, and the run resolves after every step
    rather than depending on a human. A dropped transaction now costs a retry, not a wallet.
  * *A host that signs is a host holding a key.* Accepted, and irreducible. The mitigation is
    that both keys are Base Sepolia keys holding faucet ETH, the price ceiling below bounds
    what any single run can move, and the health endpoint says plainly that this deployment
    signs. A demo that cannot sign cannot be performed, and the user's requirement is that a
    judge performs it.

**Why each step is a subprocess rather than a function call.** Every setting in `cli` is read
from the environment at call time, which is right for a command and unsafe inside a server:
`os.environ` is process-global, so two runs pointed at two different session memories would
interleave and one judge's outcome would be written into another's store. A subprocess gets its
own environment by definition. It also gets its own file-lock ownership, which matters because
`fcntl` locks belong to an open file description and a thread cannot exclude its own process.
The cost is a process spawn per step, which is invisible beside a ten second inclusion wait.

The second reason is more important than the first. The chain path is the part of this build
that has been run live, reviewed seven times and fixed today; the commands are its tested
surface. Reimplementing them behind an HTTP handler would fork the thing least able to afford a
fork.

**Why the run is a procedure and not a list of commands.** The deal id does not exist until the
creation is mined, so the argument list for every step after the first is not knowable when the
run is queued. What the page needs is a stable list of *labels* to render, which is declared
once in `STEPS` and filled in as the run walks it.

**Why waiting for confirmation is its own visible step.** Freeing a wallet needs inclusion,
about ten seconds. Writing an outcome into memory needs confirmation at the safe head, which on
Base Sepolia trails the tip by roughly 66 seconds. Folding that into the release step would
show a judge a spinner for a minute and a half with no explanation, and the usual conclusion
about an unexplained spinner is that the thing is broken.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

#: Run states. `queued` and `running` are the only non-terminal ones.
QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"
#: A fifth, and not a failure. Two memories can leave no overlap on a term, and the honest
#: answer is that no deal exists rather than a deal quoted anyway. It is a demo beat.
REFUSED = "refused"

#: Step states, reported per entry of `STEPS`.
PENDING, DONE, SKIPPED = "pending", "done", "skipped"

#: The steps a judge sees, in order, with the label each one is rendered under. Declared here
#: rather than assembled during the run so the page can draw the whole list before anything has
#: happened, which is what makes a queued run legible instead of blank.
STEPS: tuple[tuple[str, str], ...] = (
    ("quote", "Settle the terms from both memories"),
    ("create", "Buyer opens the deal and escrows the price"),
    ("accept", "Provider accepts and posts the bond"),
    ("deliver", "Provider marks the work delivered"),
    ("release", "Buyer releases the payment"),
    ("confirm", "Base confirms the outcome at the safe head"),
    ("teach", "Both memories record what happened"),
)

#: What a refund does, as one visible step. Its own list because a refund is not a
#: settlement: nothing is negotiated, nothing is remembered, and the only thing it produces is
#: a transaction returning what the escrow is holding.
REFUND_STEPS: tuple[tuple[str, str], ...] = (
    ("refund", "Return what the escrow is holding to the emptier wallet"),
)

#: The two kinds of work the queue carries.
SETTLEMENT, REFUND = "settlement", "refund"

#: Statuses that free a wallet. Inclusion consumes the nonce, which is the only question the
#: next step is asking; permanence is a different question and only the teach step asks it.
_INCLUDED = frozenset({"included_success", "confirmed_success"})
#: The only status a receipt may have before it is allowed to become memory.
_CONFIRMED = frozenset({"confirmed_success"})
#: What the ledger calls a send that never reached a node.
UNBROADCAST_STATUS = "unbroadcast"

#: Reaching any of these means the run is over and the outcome is not the one it wanted.
_DEAD = frozenset(
    {"included_reverted", "confirmed_reverted", "stuck", "unbroadcast",
     "nonce_consumed_or_replaced"}
)

#: Seconds between `tx-resolve` polls. Base produces a block every two seconds, so anything
#: shorter is a request that cannot have new information in it.
POLL_SECONDS = float(os.getenv("WRASSE_POLL_SECONDS", "4"))
#: How long one send may take to reach inclusion before the run gives up and says so.
INCLUSION_TIMEOUT = float(os.getenv("WRASSE_INCLUSION_TIMEOUT", "180"))
#: How long the confirmation wait may take. The safe head is about 66 seconds behind, and a
#: slow stretch of blocks widens that, so this is deliberately several times the expectation.
CONFIRMATION_TIMEOUT = float(os.getenv("WRASSE_CONFIRMATION_TIMEOUT", "420"))
#: A single command's own wall-clock bound, so a hung RPC cannot hold the worker forever.
COMMAND_TIMEOUT = float(os.getenv("WRASSE_COMMAND_TIMEOUT", "120"))

#: The most any one run may move. Both wallets are faucet-funded and the page is public, so
#: this is the difference between a demo that survives async judging and one that empties a
#: wallet on its ninth visitor. It bounds the settled price, not the baseline, because the
#: settled price is what actually leaves the buyer.
#:
#: Sized against the demo's own default rather than picked round. The page opens at a baseline
#: of 1e14 wei, and the urgent profile settles that at 1.18e14, so a lower ceiling would refuse
#: the demo's own front page and every judge would meet an error instead of a deal. The first
#: value here did exactly that. It is a guard against a visitor typing a large baseline, not a
#: substitute for funding the wallets, and nothing about it makes a nearly-empty wallet safe.
MAX_PRICE_WEI = int(os.getenv("WRASSE_DEMO_MAX_PRICE_WEI", str(2 * 10**14)))

#: What one whole run costs in gas, generously. Seven transactions at Base Sepolia's fees come
#: to a small fraction of this; it is deliberately loose because its only job is to catch a
#: wallet that cannot finish what it is about to start.
GAS_ALLOWANCE_WEI = int(os.getenv("WRASSE_DEMO_GAS_ALLOWANCE_WEI", str(2 * 10**13)))

#: Seconds of acceptance time a hosted run quotes. Long enough that a queued run does not
#: expire while it waits, short enough that an abandoned deal can be cancelled the same day.
ACCEPT_WINDOW = int(os.getenv("WRASSE_DEMO_ACCEPT_WINDOW", "1800"))

_EXPLORER = os.getenv("WRASSE_EXPLORER", "https://sepolia.basescan.org/tx/")


class ExecutionError(RuntimeError):
    """A run cannot continue, with a reason a judge can read."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Step:
    """One line of the progress the page draws."""

    name: str
    label: str
    status: str = PENDING
    tx_hash: str | None = None
    detail: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "status": self.status,
            "tx_hash": self.tx_hash,
            "explorer_url": f"{_EXPLORER}{self.tx_hash}" if self.tx_hash else None,
            "detail": self.detail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class Run:
    """One judge's settlement, from the quote to the two memories that learned from it."""

    run_id: str
    session_id: str
    profile: str
    baseline: dict[str, int]
    paths: dict[str, Path]
    workdir: Path
    kind: str = SETTLEMENT
    status: str = QUEUED
    error: str | None = None
    deal_id: int | None = None
    refund_to: str | None = None
    refund_wei: int | None = None
    settled: dict[str, Any] | None = None
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None
    steps: list[Step] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.steps:
            shape = REFUND_STEPS if self.kind == REFUND else STEPS
            self.steps = [Step(name, label) for name, label in shape]

    def step(self, name: str) -> Step:
        for entry in self.steps:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def view(self, *, position: int | None = None) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "profile": self.profile,
            "refund_to": self.refund_to,
            "refund_wei": self.refund_wei,
            "status": self.status,
            "error": self.error,
            "deal_id": self.deal_id,
            "settled": self.settled,
            "queue_position": position,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "steps": [entry.view() for entry in self.steps],
        }


def _default_command(
    argv: Sequence[str], env: Mapping[str, str], timeout: float
) -> tuple[int, str, str]:
    """Run one `wrasse` command in its own process.

    Invoked as `-m wrasse.cli` rather than by the console-script name so it cannot depend on
    what happens to be on `PATH` in a container, and so it runs under exactly the interpreter
    this service is running under.
    """

    completed = subprocess.run(  # noqa: S603 - argv is built here, never from a request
        [sys.executable, "-m", "wrasse.cli", *argv],
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _default_balance_reader() -> dict[str, int]:
    from . import cli

    web3 = cli._web3()
    return {
        role: int(web3.eth.get_balance(cli._required_env(name)))
        for role, name in (
            ("buyer", "WRASSE_BUYER_ADDRESS"),
            ("provider", "WRASSE_PROVIDER_A_ADDRESS"),
        )
    }


def _default_deal_id_reader(tx_hash: str) -> int:
    from . import cli
    from .escrow import deal_id_from_receipt

    return deal_id_from_receipt(cli._web3(), cli._required_env("WRASSE_ESCROW_ADDRESS"), tx_hash)


class Runner:
    """Walks one run to the end, or stops it with a reason a judge can read.

    Every collaborator that touches the world is injected, so the whole procedure can be
    exercised without a chain, a wallet or a subprocess. That is not test convenience: the
    ordering rules here are the part most likely to be wrong, and they are unreachable in a
    test that needs Base Sepolia to answer first.
    """

    def __init__(
        self,
        *,
        command: Callable[..., tuple[int, str, str]] | None = None,
        deal_id_reader: Callable[[str], int] | None = None,
        balance_reader: Callable[[], dict[str, int]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._command = command or _default_command
        self._deal_id = deal_id_reader or _default_deal_id_reader
        self._balances = balance_reader or _default_balance_reader
        self._sleep = sleep
        self._clock = clock

    # -- the world ---------------------------------------------------------------------

    def _env(self, run: Run) -> dict[str, str]:
        """This run's environment, and the reason the memory paths are set here.

        `reconcile` writes the outcome into whatever stores the environment names, so pointing
        those two variables at this session's copies is the whole of session isolation on the
        write side. Setting them in the parent process instead would put one judge's outcome
        into another judge's memory, which is the failure this service most needs not to have.
        """

        env = dict(os.environ)
        env["WRASSE_ALLOW_BROADCAST"] = "1"
        env["WRASSE_BUYER_MEMORY_PATH"] = str(run.paths["buyer"])
        env["WRASSE_PROVIDER_MEMORY_PATH"] = str(run.paths["provider"])
        return env

    def _cli(self, run: Run, argv: Sequence[str]) -> Any:
        """One command, its exit code checked and its JSON returned."""

        try:
            code, out, err = self._command(argv, self._env(run), COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise ExecutionError(f"`{argv[0]}` did not finish within {COMMAND_TIMEOUT:.0f}s") from error
        if code != 0:
            raise ExecutionError(_reason(argv, out, err))
        try:
            import json

            return json.loads(out)
        except ValueError as error:
            raise ExecutionError(f"`{argv[0]}` produced no readable result") from error

    # -- resolution --------------------------------------------------------------------

    def _resolve_until(
        self, run: Run, intent_id: str, accept: Iterable[str], timeout: float
    ) -> dict[str, Any]:
        """Poll `tx-resolve` until this intent reaches a status the run can build on.

        `tx-resolve` is the only writer to the ledger and it walks every unresolved row, not
        just this one, which is what keeps the two wallets free without a separate janitor.
        The status this waits for is the caller's choice because inclusion and confirmation are
        different questions: a wallet is freed by the first and a memory may only be written
        from the second.
        """

        accept = frozenset(accept)
        deadline = self._clock() + timeout
        last = "no row yet"
        while True:
            rows = self._cli(run, ["tx-resolve"])
            row = next(
                (entry for entry in rows if entry.get("intent_id") == intent_id), None
            )
            if row is not None:
                status = row.get("status")
                last = f"{status}: {row.get('detail') or row.get('action') or ''}".strip(": ")
                if status in accept:
                    return row
                if status in _DEAD:
                    raise ExecutionError(f"the transaction ended as {status}. {last}")
            if self._clock() >= deadline:
                raise ExecutionError(
                    f"gave up after {timeout:.0f}s waiting for the transaction. Last seen: {last}"
                )
            self._sleep(POLL_SECONDS)

    # -- the procedure -----------------------------------------------------------------

    def execute(self, run: Run) -> None:
        """Quote, settle on chain, and teach both memories what happened."""

        run.status = RUNNING
        if run.kind == REFUND:
            self._run_refund(run)
            return
        try:
            self._quote(run)
            self._create(run)
            self._accept(run)
            self._deliver(run)
            release = self._release(run)
            self._confirm(run, release)
            self._teach(run, release)
        except _Refused:
            # Every later step is marked skipped rather than pending, so the page does not
            # draw a run that looks like it is still going to do something.
            self._skip_remaining(run)
            run.status = REFUSED
            run.finished_at = _now()
            return
        except ExecutionError as error:
            self._fail(run, str(error))
            return
        except Exception as error:  # noqa: BLE001 - a worker that dies takes every later run
            self._fail(run, f"unexpected failure: {error}")
            return
        run.status = SUCCEEDED
        run.finished_at = _now()

    def _run_refund(self, run: Run) -> None:
        """Collect what the escrow is holding and send it to whichever wallet holds less.

        The recipient is chosen rather than fixed, and the reason is arithmetic. A settlement
        moves the price from the buyer to the provider and the bond from the provider and back
        again, so the provider's credit is price plus bond. Sending that to the buyer every
        time leaves the provider short by a bond per run; sending it to the provider every time
        leaves the buyer short by a price. Either way one wallet drains and somebody goes
        looking for a faucet.

        Sending it to whichever wallet is currently emptier balances the pair on its own. The
        two of them together lose only gas, which on Base Sepolia is a rounding error, so the
        demo funds itself for as long as anyone wants to use it.

        `withdraw` collects the caller's entire credit at execution time. The amount is not
        calldata and cannot be, so what is reported here is what the chain moved rather than
        what was intended.
        """

        step = self._begin(run, "refund")
        try:
            balances = self._balances()
            run.refund_to = min(balances, key=lambda role: balances[role])
            destination = self._address(run.refund_to)
            sent = self._cli(run, ["withdraw", "--role", "provider", "--to", destination])
            if sent.get("status") == UNBROADCAST_STATUS:
                raise ExecutionError(sent.get("error", "the refund was refused before sending"))
            step.tx_hash = sent.get("tx_hash")
            self._resolve_until(run, sent["intent_id"], _INCLUDED, INCLUSION_TIMEOUT)
            after = self._balances()
            run.refund_wei = after[run.refund_to] - balances[run.refund_to]
            self._end(step, detail=(
                f"returned to the {run.refund_to} wallet, which was the emptier of the two"
            ))
        except ExecutionError as error:
            self._fail(run, str(error))
            return
        except Exception as error:  # noqa: BLE001
            self._fail(run, f"unexpected failure: {error}")
            return
        run.status = SUCCEEDED
        run.finished_at = _now()

    def _address(self, role: str) -> str:
        from . import cli

        return cli._required_env(
            "WRASSE_BUYER_ADDRESS" if role == "buyer" else "WRASSE_PROVIDER_A_ADDRESS"
        )

    def _skip_remaining(self, run: Run) -> None:
        for step in run.steps:
            if step.status == PENDING:
                step.status = SKIPPED

    def _fail(self, run: Run, reason: str) -> None:
        run.status = FAILED
        run.error = reason
        run.finished_at = _now()
        for step in run.steps:
            if step.status == RUNNING:
                step.status = FAILED
                step.detail = reason
                step.finished_at = _now()

    def _begin(self, run: Run, name: str) -> Step:
        step = run.step(name)
        step.status = RUNNING
        step.started_at = _now()
        return step

    def _end(self, step: Step, *, tx_hash: str | None = None, detail: str | None = None) -> None:
        step.status = DONE
        step.tx_hash = tx_hash or step.tx_hash
        step.detail = detail or step.detail
        step.finished_at = _now()

    def _quote(self, run: Run) -> None:
        """Produce the live, executable document this run will sign against.

        Live, and therefore quoted with an acceptance window rather than a supplied timestamp.
        The read-only quote endpoint deliberately supplies both times so a link reproduces its
        own answer; a document that is going to be signed cannot do that, because the deadline
        it commits to has to be one Base will still accept when the transaction is mined.
        """

        from . import cli

        step = self._begin(run, "quote")
        document_path = run.workdir / "policy.json"
        self._cli(run, [
            "policy", cli._required_env("WRASSE_PROVIDER_A_ADDRESS"),
            "--buyer", cli._required_env("WRASSE_BUYER_ADDRESS"),
            "--accept-window", str(ACCEPT_WINDOW),
            "--base-price-wei", str(run.baseline["price_wei"]),
            "--base-bond-bps", str(run.baseline["provider_bond_bps"]),
            "--service-window", str(run.baseline["service_window"]),
            "--payout-delay", str(run.baseline["payout_delay"]),
            "--output", str(document_path),
        ])

        import json

        document = json.loads(document_path.read_text(encoding="utf-8"))
        profiles = document["buyer"]["profiles"]
        if run.profile not in profiles:
            raise ExecutionError(f"{run.profile} is not a profile this engine produces")
        settlement = profiles[run.profile]["settlement"]
        if not settlement.get("agreed"):
            # Not a failure. Two memories that leave no overlap are a real answer, and saying
            # so is the point of publishing the limits: the run stops because no deal exists,
            # not because anything went wrong.
            run.settled = {"agreed": False, **{
                key: settlement[key] for key in ("failed_on", "gap") if key in settlement
            }}
            self._end(step, detail=(
                f"no overlap on {settlement.get('failed_on')}: the two memories leave a gap of "
                f"{settlement.get('gap')}. There is nothing to sign."
            ))
            raise _Refused()

        terms = profiles[run.profile]["terms"]
        if terms["price_wei"] > MAX_PRICE_WEI:
            raise ExecutionError(
                f"the settled price of {terms['price_wei']} wei is above this deployment's "
                f"{MAX_PRICE_WEI} wei ceiling. Both wallets are faucet-funded and shared, so a "
                "single run is bounded. Quote a smaller baseline price and run it again."
            )
        self._require_funded(terms)
        run.settled = {"agreed": True, **terms}
        self._end(step, detail=(
            f"bond {terms['provider_bond_bps']} bps, window {terms['service_window']}s, "
            f"price {terms['price_wei']} wei, payout delay {terms['payout_delay']}s"
        ))

    def _require_funded(self, terms: dict[str, Any]) -> None:
        """Refuse a run neither wallet can finish, before the first transaction rather than
        during the third.

        `chain.require_affordable` already guards each individual send, which is the right
        place for it and the wrong time for this. By then the deal exists: the buyer's price is
        in escrow, the provider cannot accept, and the visitor is looking at a half-finished
        lifecycle and an error about gas. Checking here costs one RPC read and turns that into
        a sentence saying the demo wallet needs topping up.

        The provider's side is the bond, which is a fraction of the price, plus its own gas.
        """

        balances = self._balances()
        bond = terms["price_wei"] * terms["provider_bond_bps"] // 10_000
        for role, needs in (
            ("buyer", terms["price_wei"] + GAS_ALLOWANCE_WEI),
            ("provider", bond + GAS_ALLOWANCE_WEI),
        ):
            if balances.get(role, 0) < needs:
                raise ExecutionError(
                    f"the {role} wallet holds {balances.get(role, 0)} wei and this run needs "
                    f"about {needs}. Both wallets are faucet-funded and shared; the demo needs "
                    "topping up before it can settle another deal. Quoting is unaffected."
                )

    def _create(self, run: Run) -> dict[str, Any]:
        step = self._begin(run, "create")
        sent = self._cli(run, [
            "create-deal",
            "--policy", str(run.workdir / "policy.json"),
            "--profile", run.profile,
            "--accept-window", str(ACCEPT_WINDOW),
            "--base-price-wei", str(run.baseline["price_wei"]),
            "--base-bond-bps", str(run.baseline["provider_bond_bps"]),
            "--service-window", str(run.baseline["service_window"]),
            "--payout-delay", str(run.baseline["payout_delay"]),
        ])
        step.tx_hash = sent.get("tx_hash")
        self._resolve_until(run, sent["intent_id"], _INCLUDED, INCLUSION_TIMEOUT)

        # The id exists nowhere until the log does, so every later step waits on this read
        # rather than on the wallet being free.
        run.deal_id = self._deal_id(sent["tx_hash"])
        self._end(step, detail=f"deal {run.deal_id} is open")
        return sent

    def _action(self, run: Run, name: str, command: str, detail: str) -> dict[str, Any]:
        step = self._begin(run, name)
        sent = self._cli(run, [command, "--deal-id", str(run.deal_id)])
        step.tx_hash = sent.get("tx_hash")
        self._resolve_until(run, sent["intent_id"], _INCLUDED, INCLUSION_TIMEOUT)
        self._end(step, detail=detail)
        return sent

    def _accept(self, run: Run) -> dict[str, Any]:
        return self._action(run, "accept", "accept-deal", "the bond is posted")

    def _deliver(self, run: Run) -> dict[str, Any]:
        return self._action(run, "deliver", "mark-delivered", "delivery is recorded on chain")

    def _release(self, run: Run) -> dict[str, Any]:
        return self._action(run, "release", "release-deal", "the payment is credited")

    def _confirm(self, run: Run, release: dict[str, Any]) -> None:
        """Wait for the release receipt to reach the safe head.

        Its own step because it is the only slow one, and an unexplained ninety seconds reads
        as a hang. Inclusion already freed both wallets; what this waits for is the different
        and stronger claim that the outcome is permanent enough to become memory.
        """

        step = self._begin(run, "confirm")
        step.tx_hash = release.get("tx_hash")
        self._resolve_until(run, release["intent_id"], _CONFIRMED, CONFIRMATION_TIMEOUT)
        self._end(step, detail="confirmed at the safe head; this outcome may become memory")

    def _teach(self, run: Run, release: dict[str, Any]) -> None:
        step = self._begin(run, "teach")
        step.tx_hash = release.get("tx_hash")
        self._cli(run, ["reconcile", "--tx", release["tx_hash"]])
        self._end(step, detail="both memories now hold this outcome")


class _Refused(Exception):
    """The two memories left no overlap. Carried as control flow, reported as an outcome."""


def _reason(argv: Sequence[str], out: str, err: str) -> str:
    """The most useful line a failed command produced, named by the command that produced it."""

    for stream in (err, out):
        lines = [line.strip() for line in stream.splitlines() if line.strip()]
        if lines:
            return f"`{argv[0]}` failed: {lines[-1]}"
    return f"`{argv[0]}` failed with no output"


class Queue:
    """One worker, one run at a time, and a position for everyone waiting.

    Serialising is not a concession here, it is the correct model. Both wallets are shared, the
    ledger permits one unresolved transaction per wallet, and that rule is what stops a nonce
    gap. A pool of workers would spend its life contending for the same two nonces and would
    reintroduce the exact class of failure the ledger exists to prevent.

    What that costs is honest and worth reporting rather than hiding: a run takes a couple of
    minutes, so the third visitor in the queue waits a few. `position` exists so the page can
    say so instead of showing a dead button.
    """

    def __init__(self, runner: Runner | None = None, *, history: int = 200) -> None:
        self._runner = runner or Runner()
        self._condition = threading.Condition()
        self._pending: deque[str] = deque()
        self._runs: dict[str, Run] = {}
        self._order: deque[str] = deque()
        self._history = history
        self._active: str | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._work, name="wrasse-executor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    # -- submission --------------------------------------------------------------------

    def submit(self, run: Run) -> Run:
        with self._condition:
            self._runs[run.run_id] = run
            self._order.append(run.run_id)
            self._pending.append(run.run_id)
            self._trim()
            self._condition.notify()
        self.start()
        return run

    def get(self, run_id: str) -> Run | None:
        with self._condition:
            return self._runs.get(run_id)

    def position(self, run_id: str) -> int | None:
        """How many runs are in front of this one, or None once it is no longer waiting.

        Zero means it is next and nothing is in front of it; a run already executing reports
        None, because it is not waiting for anything.
        """

        with self._condition:
            try:
                return self._pending.index(run_id)
            except ValueError:
                return None

    def depth(self) -> int:
        with self._condition:
            return len(self._pending) + (1 if self._active else 0)

    def runs_for(self, session_id: str) -> list[Run]:
        with self._condition:
            return [run for run in self._runs.values() if run.session_id == session_id]

    def _trim(self) -> None:
        """Forget the oldest finished runs. Called with the lock held."""

        while len(self._order) > self._history:
            oldest = self._order[0]
            run = self._runs.get(oldest)
            if run is not None and run.status in (QUEUED, RUNNING):
                return
            self._order.popleft()
            self._runs.pop(oldest, None)

    # -- the worker --------------------------------------------------------------------

    def _work(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                run_id = self._pending.popleft()
                self._active = run_id
                run = self._runs[run_id]
            try:
                self._runner.execute(run)
            finally:
                with self._condition:
                    self._active = None

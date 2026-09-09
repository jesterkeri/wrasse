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

#: The three outcomes the escrow can actually produce, and the only three a run may aim at.
#: Named for what happens rather than for the call that ends it, because the visitor is picking
#: a story and not a function.
RELEASED, TIMEOUT, DELAYED = "released", "timeout", "delayed"

#: The steps a run walks, per outcome, with the label each is rendered under. Declared up front
#: so the page can draw the whole list before anything has happened, which is what makes a
#: queued run legible instead of blank.
#:
#: The two failures cost real time and the contract is why. `claimTimeout` requires the
#: service window to have passed and `claimPayment` requires the payout delay to have passed,
#: both measured in block time, so a history containing either one has to be waited for rather
#: than asserted. Set those two durations short and a failure costs a couple of minutes; set
#: them long and it costs exactly as long as you asked for.
SHAPES: dict[str, tuple[tuple[str, str], ...]] = {
    RELEASED: (
        ("quote", "Settle the terms from both memories"),
        ("create", "Buyer opens the deal and escrows the price"),
        ("accept", "Provider accepts and posts the bond"),
        ("deliver", "Provider marks the work delivered"),
        ("release", "Buyer releases the payment"),
        ("confirm", "Base confirms the outcome at the safe head"),
        ("teach", "Both memories record what happened"),
    ),
    TIMEOUT: (
        ("quote", "Settle the terms from both memories"),
        ("create", "Buyer opens the deal and escrows the price"),
        ("accept", "Provider accepts and posts the bond"),
        ("wait", "Nothing is delivered. Waiting out the window the seller agreed to"),
        ("claim", "Buyer claims the deal back after the deadline"),
        ("confirm", "Base confirms the outcome at the safe head"),
        ("teach", "Both memories record what happened"),
    ),
    DELAYED: (
        ("quote", "Settle the terms from both memories"),
        ("create", "Buyer opens the deal and escrows the price"),
        ("accept", "Provider accepts and posts the bond"),
        ("deliver", "Provider marks the work delivered"),
        ("wait", "The buyer does not release. Waiting out the agreed payment delay"),
        ("claim", "Seller claims its payment once the delay has passed"),
        ("confirm", "Base confirms the outcome at the safe head"),
        ("teach", "Both memories record what happened"),
    ),
}

#: Kept for callers that still ask for the shape of an ordinary settlement.
STEPS: tuple[tuple[str, str], ...] = SHAPES[RELEASED]

#: What a refund does, as one visible step. Its own list because a refund is not a
#: settlement: nothing is negotiated, nothing is remembered, and the only thing it produces is
#: a transaction returning what the escrow is holding.
REFUND_STEPS: tuple[tuple[str, str], ...] = (
    ("refund", "Return what the escrow is holding to the emptier wallet"),
)

#: What a reclaim does, which is one thing.
RECLAIM_STEPS: tuple[tuple[str, str], ...] = (
    ("reclaim", "Resolve every transaction left unresolved by the previous process"),
)

#: The three kinds of work the queue carries.
SETTLEMENT, REFUND, RECLAIM = "settlement", "refund", "reclaim"

#: Statuses that free a wallet. Inclusion consumes the nonce, which is the only question the
#: next step is asking; permanence is a different question and only the teach step asks it.
_INCLUDED = frozenset({"included_success", "confirmed_success"})
#: The only status a receipt may have before it is allowed to become memory.
_CONFIRMED = frozenset({"confirmed_success"})
#: What the ledger calls a send that never reached a node.
UNBROADCAST_STATUS = "unbroadcast"

#: Statuses in which a row is no longer holding its wallet's nonce. Terminal ones have finished
#: and `included_*` have consumed the nonce, which is the only question the next send asks. Any
#: other status means the wallet is still blocked, whatever the resolver's exit code said.
#: Statuses in which a creation produced no deal, whatever its transaction hash says. The hash
#: is written when the bytes are signed, so it is present long before anything is sent and stays
#: present when nothing ever was.
_NO_DEAL_STATUSES = frozenset({
    "unbroadcast", "included_reverted", "confirmed_reverted", "nonce_consumed_or_replaced",
})
#: And the only two in which one did.
_MADE_DEAL_STATUSES = frozenset({"included_success", "confirmed_success"})

_FREED_STATUSES = frozenset({
    "included_success", "included_reverted",
    "confirmed_success", "confirmed_reverted",
    "nonce_consumed_or_replaced", "unbroadcast",
})

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
#: How long a run may sit waiting out a deadline the contract enforces. Its own constant, and
#: the separation is the whole point: a confirmation wait is bounded by how far behind the safe
#: head runs, which is a property of Base, while this one is bounded by a number the visitor
#: typed. Sharing `CONFIRMATION_TIMEOUT` made every ending that waits fail at the default
#: baseline, because the settled payout delay is 900 seconds and that budget was 420.
WAIT_TIMEOUT = float(os.getenv("WRASSE_WAIT_TIMEOUT", "1200"))
#: What the wait costs beyond the deadline itself: four blocks of margin, and the acceptance or
#: delivery that has to be included before the clock the contract reads even starts.
_WAIT_MARGIN = 60
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
    #: Which of the three the run is aiming at. A choice, not a description: the run performs
    #: it rather than asserting it, so the receipt at the end is the outcome.
    outcome: str = RELEASED
    status: str = QUEUED
    error: str | None = None
    deal_id: int | None = None
    #: The identity the transaction ledger will file this run's creation under, derived from
    #: the quote rather than read back from a reply. A liability written before signing has to
    #: be keyed on something a later process can look up, and the reply is the thing a timeout
    #: takes away.
    intent_id: str | None = None
    #: Deals this run is responsible for driving to a terminal state before it collects. Only
    #: a refund run carries any: it is filled from every settlement the session performed, so a
    #: run that failed halfway does not leave its deposit behind a deal nobody will finish.
    recover: list[int] = field(default_factory=list)
    refund_to: str | None = None
    refund_wei: int | None = None
    settled: dict[str, Any] | None = None
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None
    steps: list[Step] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.steps:
            shape = {REFUND: REFUND_STEPS, RECLAIM: RECLAIM_STEPS}.get(
                self.kind, SHAPES.get(self.outcome, ())
            ) or SHAPES[self.outcome]
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
            "outcome": self.outcome,
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


def _eth(wei: int) -> str:
    """Wei to an exact ETH string, by moving the decimal point rather than dividing.

    The same reasoning as on the page: a float cannot hold eighteen significant figures, so
    0.1 ETH becomes 99999999999999998 wei and the line would be quoting a number nobody sent.
    """

    digits = str(int(wei)).rjust(19, "0")
    whole, frac = digits[:-18], digits[-18:].rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def _percent(bps: int) -> str:
    """Basis points as a percentage, without a trailing zero nobody asked for."""

    value = int(bps) / 100
    return f"{value:g}%"


def _minutes(seconds: int) -> str:
    """Minutes once there are whole minutes to show, seconds below that."""

    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} seconds"
    minutes = seconds / 60
    return f"{minutes:g} minutes"


def _default_credit_reader() -> dict[str, int]:
    """What the escrow is holding for each wallet, as opposed to what each wallet holds.

    A settlement never sends: every terminal path credits the recipient and `withdraw` is the
    only call that moves value out. So the escrow's own ledger is the thing a refund has to
    read, and a wallet balance says nothing about it.
    """

    from . import cli
    from .escrow import contract

    escrow = contract(cli._web3(), cli._required_env("WRASSE_ESCROW_ADDRESS"))
    return {
        role: int(escrow.functions.withdrawable(cli._required_env(name)).call())
        for role, name in (
            ("buyer", "WRASSE_BUYER_ADDRESS"),
            ("provider", "WRASSE_PROVIDER_A_ADDRESS"),
        )
    }


def _default_deal_reader(deal_id: int) -> dict[str, Any]:
    from . import cli
    from .escrow import read_deal

    return read_deal(cli._web3(), cli._required_env("WRASSE_ESCROW_ADDRESS"), deal_id)


def _default_chain_now() -> int | None:
    """Block time, never local time.

    A deadline is enforced by the block that mines the transaction, so a wait measured against
    this machine's clock would be measuring the wrong thing. Two seconds of drift is the
    difference between claiming a timeout and having it reverted.
    """

    from . import cli

    return cli._chain_now(cli._web3())


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
        credit_reader: Callable[[], dict[str, int]] | None = None,
        #: Where open deals are written down so a restart can find them. Injected like every
        #: other collaborator that touches the world, so the procedure can be exercised without
        #: one and a test can hand it a scratch file rather than the deployment's own.
        liabilities: Any | None = None,
        deal_reader: Callable[[int], dict[str, Any]] | None = None,
        chain_now: Callable[[], int | None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._command = command or _default_command
        self._deal_id = deal_id_reader or _default_deal_id_reader
        self._balances = balance_reader or _default_balance_reader
        self._credits = credit_reader or _default_credit_reader
        if liabilities is None:
            from . import liabilities as _liabilities

            liabilities = _liabilities
        self._liabilities = liabilities
        self._deal = deal_reader or _default_deal_reader
        self._chain_now = chain_now or _default_chain_now
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
        if run.kind == RECLAIM:
            self._run_reclaim(run)
            return
        if run.kind == REFUND:
            self._run_refund(run)
            return
        try:
            self._quote(run)
            self._create(run)
            self._accept(run)
            if run.outcome == TIMEOUT:
                # Nothing is delivered. This is the buyer's grievance, and the only way to
                # produce it is to let the window the seller agreed to actually run out.
                self._wait_for(run, "deadline", "the delivery window")
                ending = self._claim(run, "claim-timeout", "the buyer took the deal back")
            elif run.outcome == DELAYED:
                self._deliver(run)
                # Delivered, and the buyer does not release. The seller has to wait out the
                # delay it agreed to before the contract will pay it.
                self._wait_for(run, "payout_available_at", "the payment delay")
                ending = self._claim(run, "claim-payment", "the seller took its payment")
            else:
                self._deliver(run)
                ending = self._release(run)
            self._confirm(run, ending)
            # Terminal on chain and permanent, so the escrow has assigned its value and
            # `withdraw` can see it. Forgetting it here is what stops a finished deal reading
            # as a stranded one: the worker offers recovery whenever a run ends with a deal
            # still listed, and a happy path that never struck its own deal off made every
            # first run look like a failure with money left behind.
            self._forget_liability(run.deal_id)
            self._teach(run, ending)
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

    def _run_reclaim(self, run: Run) -> None:
        """Free both wallets before this process serves anybody.

        The ledger is the only durable thing here, and it survives a restart while the queue,
        the sessions and the runs do not. A process that died with a transaction sent leaves a
        row holding a nonce, and the ledger permits one unresolved transaction per wallet, so
        the first visitor after a restart would meet a wallet that refuses to sign for a
        transaction they know nothing about.

        Resolving by query rather than resending: the chain is asked what happened to each row
        and the answer is recorded. A row that genuinely mined frees its nonce here. A row
        whose bytes never reached a node is a different problem and stays where it is, which is
        what the operator escape exists for.

        This does not restore the runs themselves. A visitor whose settlement was in flight
        across a restart loses their run id and starts again; what they must not lose is the
        deposit, and the refund's own recovery closes any deal that run left open.
        """

        step = self._begin(run, "reclaim")
        try:
            rows = self._cli(run, ["tx-resolve"])

            # Every row's status, not the command's exit code. `tx-resolve` exits zero having
            # reported that a transaction is absent and needs a deliberate rebroadcast, which
            # leaves the row and its nonce exactly where they were. Counting that as reclaimed
            # was the defect: the wallet stayed blocked and the next visitor met it.
            held = [
                row for row in rows
                if str(row.get("status")) not in _FREED_STATUSES
            ]
            if held:
                raise ExecutionError(
                    "these transactions still hold a wallet and this service cannot free them "
                    "on its own: "
                    + "; ".join(
                        f"{row.get('intent_id')} is {row.get('status')}"
                        f" ({row.get('action') or 'no action'})" for row in held
                    )
                    + ". An operator has to decide whether to resend identical bytes; until "
                    "then no settlement can be signed, because the nonce is not free."
                )

            # A restart inherits deals whose runs no longer exist. This is the only place that
            # can find them, and finding them is what makes the recovery claim true rather
            # than aspirational.
            self._name_unresolved(run, step)
            inherited = self._open_liabilities(None)
            if inherited:
                self._recover_deals(run, inherited)
                self._collect(run, step)
        except ExecutionError as error:
            self._fail(run, str(error))
            return
        except Exception as error:  # noqa: BLE001
            self._fail(run, f"unexpected failure: {error}")
            return

        moved = [row for row in rows if row.get("action") not in (None, "none")]
        self._end(step, detail=(
            f"{len(moved)} of {len(rows)} transactions were still open and have been resolved"
            + (f"; {len(inherited)} deals left open by a previous process were closed"
               if inherited else "")
            if moved or inherited else "nothing was left unresolved"
        ))
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
            # Read now, not when this run was queued. A settlement queued behind this one has
            # no deal id yet, and a snapshot taken at submission would miss it; the durable
            # index has it the moment the id exists.
            self._recover_deals(run, self._open_liabilities(run.session_id or None))
            owed = self._collect(run, step)
            if not owed:
                self._end(step, detail="the escrow was holding nothing for either wallet")
            else:
                self._end(step, detail=(
                    f"collected what the escrow held for the {' and the '.join(owed)}, into "
                    f"whichever wallet was emptier at the time"
                ))
        except ExecutionError as error:
            self._fail(run, str(error))
            return
        except Exception as error:  # noqa: BLE001
            self._fail(run, f"unexpected failure: {error}")
            return
        run.status = SUCCEEDED
        run.finished_at = _now()

    #: How a deal that never reached an ending is driven to one, by the state the contract has
    #: it in. Each is the only action that state permits, and each ends by crediting somebody.
    #: A terminal state is absent, because there is nothing left to do to it.
    _RECOVERY: dict[str, tuple[str, str | None]] = {
        "Offered": ("cancel-unaccepted", "accept_by"),
        "Accepted": ("claim-timeout", "deadline"),
        # The buyer may release at any time, so nothing has to expire first. Releasing rather
        # than waiting out the payout delay also ends it in the state the seller would prefer,
        # which is the fair reading of a run this service abandoned rather than the visitor.
        "Delivered": ("release-deal", None),
    }

    def _recover_deals(self, run: Run, deal_ids: Sequence[int]) -> None:
        """Drive every deal this session opened to a terminal state, before collecting.

        A settlement can fail after `createDeal` has already escrowed the price: the acceptance
        reverts, an RPC times out, the worker is restarted. The deal is then a live liability
        holding a deposit, and `withdraw` cannot see it, because `withdraw` collects credits and
        an unfinished deal has assigned none. Nothing in the service used to close that gap, so
        the money sat there until a human ran a lifecycle command by hand.

        Recovery belongs here rather than in the failing run because the same procedure covers
        the run that failed, the run that was interrupted, and the visitor who simply left. A
        deal already terminal is skipped, so this costs one read per deal in the ordinary case.
        """

        for deal_id in deal_ids:
            deal = self._deal(deal_id)
            action = self._RECOVERY.get(str(deal["state"]))
            if action is None:
                # Terminal, so its value is already credited and `withdraw` can see it. Drop it
                # from the durable index rather than reading it again on every later refund.
                self._forget_liability(deal_id)
                continue
            command, expires = action
            step = Step("recover", f"Close deal {deal_id}, which is still holding a deposit")
            step.status = RUNNING
            step.started_at = _now()
            run.steps.insert(len(run.steps) - 1, step)
            if expires is not None:
                self._hold_until(
                    step, int(deal[expires]) + 4, f"the deadline on deal {deal_id}"
                )
            sent = self._cli(run, [command, "--deal-id", str(deal_id)])
            step.tx_hash = sent.get("tx_hash")
            self._resolve_until(run, sent["intent_id"], _INCLUDED, INCLUSION_TIMEOUT)
            self._forget_liability(deal_id)
            self._end(step, detail=(
                f"deal {deal_id} was {deal['state']} and holding a deposit; it is now closed "
                "and what it held has been credited"
            ))

    def _name_unresolved(self, run: Run, step: Step) -> None:
        """Decide what became of every creation whose deal id was never learned.

        One loop, and it branches on what the ledger says rather than on whether a hash is
        present. A hash is present from the moment the bytes are signed, so it is not evidence
        that anything was sent: a creation the node rejected before the mempool is marked
        `unbroadcast` and keeps its hash, and one that mined and reverted has a hash and a
        receipt and no deal. Treating a hash as a probable deal made both of those into a row
        that could never be resolved, and the next boot refused to serve because of it. One
        ordinary rejected creation would have closed the demo until an operator repaired a
        record for a deal that never existed.

        Nothing here raises. A status this cannot act on yet leaves its row for the next boot,
        which is the safe direction: the row is the thing that remembers. The reclaim still
        fails closed when the ledger itself cannot be read, because that is `_cli` raising.
        """

        for row in [r for r in self._liabilities.rows(None) if r["deal_id"] is None]:
            intent = str(row["intent_id"])
            found = self._ledger_row(run, intent)
            if found is None:
                # The ledger never took a nonce for it. The row is written before signing and
                # the ledger row is committed before broadcast, so this is proof that nothing
                # was signed and nothing can be escrowed behind it.
                self._liabilities.discard(intent)
                continue

            status = str(found.get("status") or "")
            if status in _NO_DEAL_STATUSES:
                # Signed and refused, or mined and reverted. Either way the escrow holds
                # nothing for it and the ledger governs its nonce.
                self._liabilities.discard(intent)
                continue
            if status not in _MADE_DEAL_STATUSES or not found.get("tx_hash"):
                # Still in flight, or signed and not yet sent. Nothing to resolve today.
                continue

            self._liabilities.attach(intent, tx_hash=str(found["tx_hash"]))
            try:
                deal_id = self._deal_id(str(found["tx_hash"]))
            except Exception:  # noqa: BLE001 - a receipt that cannot be read right now
                # It succeeded, so a deal exists; this run simply could not read it back. The
                # row keeps its hash and the next boot tries again.
                continue
            self._liabilities.attach(intent, deal_id=deal_id)
            step.detail = f"deal {deal_id} was left unnamed by a previous process"

    def _ledger_row(self, run: Run, intent_id: str) -> dict[str, Any] | None:
        """What the transaction ledger holds for one intent, or nothing if it holds none."""

        rows = self._cli(run, ["tx-status", "--intent", intent_id])
        for row in rows if isinstance(rows, list) else []:
            if str(row.get("intent_id")) == intent_id:
                return row
        return None

    def _collect(self, run: Run, step: Step) -> list[str]:
        """Empty the escrow into the two wallets, and say which sides it collected for.

        Both sides, because which side holds a credit depends on how the runs ended. A released
        deal credits the seller its price and stake back; a timeout credits the buyer the same
        total. Collecting only the seller left every timeout a visitor produced sitting in the
        escrow, and `withdraw` reverts on a zero credit, so a session made only of timeouts
        refunded nothing and reported a failure over money it never looked at.
        """

        before = self._balances()
        credits = self._credits()
        owed = [role for role in ("provider", "buyer") if credits.get(role, 0) > 0]
        if not owed:
            run.refund_to = min(before, key=lambda role: before[role])
            run.refund_wei = 0
            return []

        hashes = []
        for role in owed:
            # Recomputed per collection rather than chosen once. The first withdrawal changes
            # which wallet is emptier, and the point of choosing at all is to keep the pair
            # level.
            standing = self._balances()
            run.refund_to = min(standing, key=lambda name: standing[name])
            destination = self._address(run.refund_to)
            sent = self._cli(run, ["withdraw", "--role", role, "--to", destination])
            if sent.get("status") == UNBROADCAST_STATUS:
                raise ExecutionError(sent.get("error", "the refund was refused before sending"))
            hashes.append(sent.get("tx_hash"))
            self._resolve_until(run, sent["intent_id"], _INCLUDED, INCLUSION_TIMEOUT)
        step.tx_hash = hashes[-1]
        after = self._balances()
        run.refund_wei = sum(max(0, after[role] - before[role]) for role in after)
        return owed

    def _address(self, role: str) -> str:
        from . import cli

        return cli._required_env(
            "WRASSE_BUYER_ADDRESS" if role == "buyer" else "WRASSE_PROVIDER_A_ADDRESS"
        )

    def _wait_for(self, run: Run, field: str, what: str) -> None:
        """Hold until the chain's own clock passes a deadline the contract will check.

        Read from the deal rather than computed from the terms. The window runs from the block
        that mined the acceptance, not from when this process sent it, and a wait derived from
        the wrong start is a transaction that reverts for a reason nobody can see afterwards.

        The margin is a block, because the transaction is mined after it is sent.
        """

        step = self._begin(run, "wait")
        deal = self._deal(run.deal_id)
        self._hold_until(step, int(deal[field]) + 4, what)
        self._end(step, detail=f"{what} has passed; the claim is now allowed")

    def _hold_until(self, step: Step, until: int, what: str) -> None:
        """Poll chain time until it passes `until`, reporting what is left as it goes.

        Split out of `_wait_for` because recovery waits on a deadline read from a deal it was
        handed rather than from the run's own, and two copies of a polling loop is two places
        for the budget to drift apart.
        """

        deadline = self._clock() + WAIT_TIMEOUT
        while True:
            now = self._chain_now()
            if now is not None and now >= until:
                return
            if self._clock() >= deadline:
                raise ExecutionError(f"gave up waiting for {what} to pass")
            step.detail = (
                f"{max(0, until - now)}s of {what} left" if now is not None
                else "the chain's clock is unreadable"
            )
            self._sleep(POLL_SECONDS)

    def _claim(self, run: Run, command: str, detail: str) -> dict[str, Any]:
        step = self._begin(run, "claim")
        sent = self._cli(run, [command, "--deal-id", str(run.deal_id)])
        step.tx_hash = sent.get("tx_hash")
        self._resolve_until(run, sent["intent_id"], _INCLUDED, INCLUSION_TIMEOUT)
        self._end(step, detail=detail)
        return sent

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
        # The identity the CLI will use for this creation, computed here rather than read back
        # from its reply. That is the whole point: a reply is exactly what a timeout loses, and
        # a liability keyed on something only the reply carries cannot be looked up afterwards.
        # `intent_id` is `{request_id}:{profile}` and both halves are already in this file.
        run.intent_id = f"{document['request_id']}:{run.profile}"
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
        self._require_waitable(run, terms)
        run.settled = {"agreed": True, **terms}
        self._end(step, detail=(
            f"stake {_percent(terms['provider_bond_bps'])} of the price, "
            f"deliver within {_minutes(terms['service_window'])}, "
            f"price {_eth(terms['price_wei'])} ETH, "
            f"paid {_minutes(terms['payout_delay'])} after delivery"
        ))

    #: Which settled term each ending has to outlast, and which baseline number moves it. An
    #: ending that waits for nothing is absent, which is why `released` is not here.
    _WAITS_ON: dict[str, tuple[str, str, str]] = {
        TIMEOUT: ("service_window", "the delivery window", "service window"),
        DELAYED: ("payout_delay", "the payment delay", "payout delay"),
    }

    def _require_waitable(self, run: Run, terms: dict[str, Any]) -> None:
        """Refuse an ending this run cannot sit out, before the deal exists.

        Two of the three endings are produced by letting a deadline the contract enforces
        actually pass, so the run has to be alive for the whole of it. The settled number is
        known here, one step before anything is signed, and comparing it against the budget
        here is the difference between a sentence naming the number to lower and a visitor
        watching a progress bar for twenty minutes before being told the same thing with their
        deposit already locked inside an accepted deal.

        Checked against the settled term rather than the baseline, because the settlement is
        what the contract will enforce: a baseline delay of 800 seconds settles at 900 when the
        buyer's floor binds, and it is the 900 this run has to outlast.
        """

        waits_on = self._WAITS_ON.get(run.outcome)
        if waits_on is None:
            return
        term, what, knob = waits_on
        needed = int(terms[term])
        if needed + _WAIT_MARGIN <= WAIT_TIMEOUT:
            return
        raise ExecutionError(
            f"this ending is produced by letting {what} run out, and the two memories settled "
            f"it at {needed}s. A single run may wait {WAIT_TIMEOUT:.0f}s. Lower the {knob} and "
            "run it again, or choose the ending where the work is delivered and paid for, "
            "which waits for nothing."
        )

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
        # Written before the transaction is signed, not after the deal id is known. Writing it
        # after looks safe, because the id is what recovery needs, but the id only becomes
        # knowable once the value has already moved: creation is included, the buyer's ETH is
        # in an Offered deal, and the receipt read that turns that into an id is a separate
        # call that can fail or be killed with the process. A crash there left a deposit on
        # chain with nothing durable pointing at it.
        self._liabilities.open_intent(run.intent_id or run.run_id, run.session_id)
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
        if sent.get("status") == UNBROADCAST_STATUS:
            # Nothing reached a node, so nothing is escrowed and the row would be a liability
            # that does not exist. This is the only case in which a row is dropped without the
            # chain having said what became of it.
            self._liabilities.discard(run.intent_id or run.run_id)
            raise ExecutionError(sent.get("error", "the deal was refused before sending"))

        step.tx_hash = sent.get("tx_hash")
        self._liabilities.attach(run.intent_id or run.run_id, tx_hash=sent.get("tx_hash"))
        self._resolve_until(run, sent["intent_id"], _INCLUDED, INCLUSION_TIMEOUT)

        # The id exists nowhere until the log does, so every later step waits on this read
        # rather than on the wallet being free.
        run.deal_id = self._deal_id(sent["tx_hash"])
        self._liabilities.attach(run.intent_id or run.run_id, deal_id=run.deal_id)
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

    def _forget_liability(self, deal_id: int) -> None:
        self._liabilities.close(deal_id)

    def _open_liabilities(self, session_id: str | None) -> list[int]:
        return self._liabilities.open_deals(session_id)


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

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        history: int = 200,
        after: Callable[[Run], None] | None = None,
    ) -> None:
        self._runner = runner or Runner()
        #: Called with every run the moment it finishes, whatever it finished as. This is where
        #: a session's automatic final refund is queued, because the thing that knows a run is
        #: over is the worker rather than the next HTTP request that happens to arrive.
        self._after = after
        #: Set when the startup reclaim failed, and it closes the queue to settlements. Not to
        #: refunds: a refund collects and closes, which is exactly what a service in this state
        #: should still be able to do.
        self._closed: str | None = None
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

    def busy_with(self, session_id: str) -> bool:
        """Whether this session has a run that has not finished.

        Asked before a session's files may be deleted. Queued counts as well as running: a run
        waiting its turn still holds the paths it was built with, and deleting them first turns
        a wait into a failure the visitor cannot explain.
        """

        with self._condition:
            return any(
                run.session_id == session_id and run.status in (QUEUED, RUNNING)
                for run in self._runs.values()
            )

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
                if run.kind == SETTLEMENT and self._closed is not None:
                    # The reclaim failed, so at least one wallet is still holding a nonce for a
                    # transaction nobody here remembers. Signing anyway would meet the ledger's
                    # refusal three transactions into a visitor's run instead of before it.
                    self._refuse(run, self._closed)
                else:
                    self._runner.execute(run)
                    if run.kind == RECLAIM and run.status != SUCCEEDED:
                        self._closed = run.error or "the startup reclaim did not succeed"
            finally:
                with self._condition:
                    self._active = None
                if self._after is not None:
                    # Cleanup belongs to whatever finished the work, not to whoever happens to
                    # poll next. A browser is not a durable job worker: a visitor who closes
                    # the tab after their last run would otherwise leave the escrow full.
                    try:
                        self._after(run)
                    except Exception:  # noqa: BLE001 - a callback must not kill the worker
                        pass

    def _refuse(self, run: Run, reason: str) -> None:
        run.status = FAILED
        run.error = (
            f"this deployment cannot sign until its wallets are free. {reason}"
        )
        for step in run.steps:
            if step.status == PENDING:
                step.status = SKIPPED
        run.finished_at = _now()

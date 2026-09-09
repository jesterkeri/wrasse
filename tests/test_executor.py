"""The run procedure, held to the orderings that make it safe rather than to its happy path.

Every collaborator that touches the world is injected, so what is under test here is the
sequence: what is waited for before what, what is refused before anything is signed, and which
environment each step runs in. None of that is reachable in a test that needs Base Sepolia to
answer first, and all of it is the part most likely to be wrong.

The document these tests quote against is the tracked sample, not a hand-written stub. It has a
profile that agrees and a profile that refuses, which are the two outcomes the procedure has to
tell apart, and its numbers are the real ones from the two receipts on chain.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from wrasse import executor
from wrasse.executor import Run, Runner

REPO = Path(__file__).resolve().parent.parent
SAMPLE = REPO / "docs" / "examples" / "policy.schema3.json"

#: The intent id each command reports, so a test can say which transaction it means.
INTENTS = {
    "create-deal": "i-create",
    "accept-deal": "i-accept",
    "mark-delivered": "i-deliver",
    "release-deal": "i-release",
    "claim-timeout": "i-claim",
    "claim-payment": "i-claim",
}


class FakeChain:
    """Stands in for the `wrasse` command line, and remembers everything it was asked.

    Statuses advance one step per `tx-resolve`, which is what a caller polling a real chain
    sees. A test that wants a transaction to stall forever gives it a progression that ends
    before the status the run is waiting for.
    """

    def __init__(self, *, progress: dict[str, list[str]] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.progress = progress or {}
        self._cursor: dict[str, int] = {}

    def commands(self) -> list[str]:
        return [argv[0] for argv, _ in self.calls]

    def env_for(self, command: str) -> dict[str, str]:
        for argv, env in self.calls:
            if argv[0] == command:
                return env
        raise AssertionError(f"{command} was never run")

    def argv_for(self, command: str) -> list[str]:
        for argv, _ in self.calls:
            if argv[0] == command:
                return argv
        raise AssertionError(f"{command} was never run")

    def _steps(self, intent: str) -> list[str]:
        return self.progress.get(intent, ["pending", "included_success", "confirmed_success"])

    def __call__(self, argv, env, timeout):
        argv = list(argv)
        self.calls.append((argv, dict(env)))
        command = argv[0]

        if command == "policy":
            destination = Path(argv[argv.index("--output") + 1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SAMPLE, destination)
            return 0, json.dumps({"output": str(destination)}), ""

        if command in INTENTS:
            intent = INTENTS[command]
            self._cursor.setdefault(intent, 0)
            return 0, json.dumps(
                {"intent_id": intent, "tx_hash": f"0x{intent}", "status": "pending"}
            ), ""

        if command == "tx-resolve":
            rows = []
            for intent, cursor in self._cursor.items():
                steps = self._steps(intent)
                self._cursor[intent] = min(cursor + 1, len(steps) - 1)
                rows.append({"intent_id": intent, "status": steps[self._cursor[intent]],
                             "action": "resolved"})
            return 0, json.dumps(rows), ""

        if command == "reconcile":
            return 0, json.dumps({"ingested": True}), ""

        raise AssertionError(f"unexpected command {command}")


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("WRASSE_BUYER_ADDRESS", "0x30C95B7eb3E08F83992E803Be2A5AB0E0af93d22")
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", "0x0b920573ADf657f45Fecd9f7e48e66B5535A90C0")
    # The tracked sample settles urgent at 1.18e14 wei, which is above the shipped ceiling on
    # purpose: the ceiling is sized for a faucet-funded wallet running hundreds of times.
    monkeypatch.setattr(executor, "MAX_PRICE_WEI", 10**15)
    monkeypatch.setattr(executor, "POLL_SECONDS", 0)
    return tmp_path


def make_run(tmp_path: Path, profile: str = "urgent") -> Run:
    """One run, whose session stores are deliberately *not* where the ambient ones are.

    The autouse `isolated_state` fixture points `WRASSE_BUYER_MEMORY_PATH` at
    `tmp_path/buyer-memory.db` so no test can touch the repository's real memories. A session
    built at that same path would inherit exactly the value the executor is supposed to be
    overriding, and the isolation test would pass with the override deleted. It did: the
    mutation survived, and this subdirectory is the fix. Two paths that happen to agree prove
    nothing about the code that sets one of them.
    """

    session_dir = tmp_path / "session"
    workdir = session_dir / "run"
    workdir.mkdir(parents=True, exist_ok=True)
    return Run(
        run_id="r1",
        session_id="s1",
        profile=profile,
        baseline={"price_wei": 100_000_000_000_000, "provider_bond_bps": 500,
                  "service_window": 600, "payout_delay": 1_800},
        paths={"buyer": session_dir / "buyer-memory.db",
               "provider": session_dir / "provider-memory.db"},
        workdir=workdir,
    )


def bounded_clock():
    """A clock that reaches every timeout in a handful of ticks.

    Found by mutation rather than by design. With the real clock, deleting the dead-status
    early stop does not make these tests fail, it makes them *hang*: the run falls through to
    the timeout path and polls for the full three minutes. A test whose failure mode is a
    three minute spin is one nobody will run twice, and in CI it reads as an infrastructure
    problem rather than as the regression it is.
    """

    ticks = iter(range(0, 10_000_000, 60))
    return lambda: next(ticks)


#: Both wallets, funded well past anything these tests settle. A test that had to think about
#: gas would be testing arithmetic it does not own.
RICH = {"buyer": 10**18, "provider": 10**18}


def runner(chain: FakeChain, balances: dict[str, int] | None = None, **kwargs) -> Runner:
    kwargs.setdefault("liabilities", FakeLiabilities())
    return Runner(
        command=chain, deal_id_reader=lambda tx_hash: 7,
        balance_reader=lambda: balances if balances is not None else RICH,
        sleep=lambda _: None, **kwargs,
    )


def test_a_whole_run_walks_the_lifecycle_in_order(environment):
    """Create, accept, deliver, release, and only then teach.

    The order is the contract's, not a preference: `acceptDeal` needs the deal Offered and
    `release` needs it Delivered. A procedure that sent them in any other order would be
    refused on chain, which is the safe failure, and would still have wasted a judge's run.
    """

    chain = FakeChain()
    run = make_run(environment)
    runner(chain).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    lifecycle = [name for name in chain.commands() if name != "tx-resolve"]
    assert lifecycle == [
        "policy", "create-deal", "accept-deal", "mark-delivered", "release-deal", "reconcile",
    ]
    assert [step.status for step in run.steps] == ["done"] * len(executor.STEPS)
    assert run.deal_id == 7
    assert run.settled == {
        "agreed": True, "payout_delay": 900, "price_wei": 118_000_000_000_000,
        "provider_bond_bps": 2480, "service_window": 300,
    }


def test_every_step_runs_against_this_session_s_own_memories(environment):
    """The session's two paths reach every subprocess, `reconcile` above all.

    `reconcile` writes the outcome into whatever stores the environment names. If the parent's
    variables leaked through, one visitor's settlement would be written into another visitor's
    memory, and the next quote that visitor asked for would be answered from a history they
    never created. That is the single worst thing this service could do quietly.
    """

    chain = FakeChain()
    run = make_run(environment)
    runner(chain).execute(run)

    for command in ("policy", "create-deal", "reconcile", "tx-resolve"):
        env = chain.env_for(command)
        assert env["WRASSE_BUYER_MEMORY_PATH"] == str(run.paths["buyer"])
        assert env["WRASSE_PROVIDER_MEMORY_PATH"] == str(run.paths["provider"])


def test_broadcasting_is_opted_into_for_every_send(environment):
    """Without this every send dies at the opt-in guard, having already signed nothing.

    The guard is deliberate and the service is the caller that is genuinely allowed past it.
    Asserting it here rather than only observing that the run succeeded is the difference
    between testing the environment and testing the fake.
    """

    chain = FakeChain()
    runner(chain).execute(make_run(environment))
    assert chain.env_for("create-deal")["WRASSE_ALLOW_BROADCAST"] == "1"


def test_a_refusal_is_an_outcome_and_not_a_failure(environment):
    """Two memories that leave no overlap end the run without signing anything.

    `budget` refuses on price in the tracked sample, by 850 basis points. Reporting that as a
    failure would be wrong twice: it would suggest the system broke, and it would hide the beat
    that the refusal exists to show. Nothing is signed, and the remaining steps say `skipped`
    rather than `pending`, so the page does not draw a run still about to do something.
    """

    chain = FakeChain()
    run = make_run(environment, profile="budget")
    runner(chain).execute(run)

    assert run.status == executor.REFUSED
    assert run.error is None
    assert chain.commands() == ["policy"]
    assert run.settled == {"agreed": False, "failed_on": "price_bps", "gap": 850}
    assert run.step("quote").status == "done"
    assert {step.status for step in run.steps if step.name != "quote"} == {"skipped"}


def test_a_price_over_the_ceiling_is_refused_before_anything_is_signed(environment, monkeypatch):
    """The ceiling has to bind before the first transaction, not after the money has moved."""

    monkeypatch.setattr(executor, "MAX_PRICE_WEI", 5 * 10**12)
    chain = FakeChain()
    run = make_run(environment)
    runner(chain).execute(run)

    assert run.status == executor.FAILED
    assert "ceiling" in run.error
    assert chain.commands() == ["policy"]


def test_the_outcome_is_taught_only_from_a_confirmed_receipt(environment):
    """Inclusion frees a wallet; only confirmation may become memory.

    The release transaction here reaches `included_success` and stays there, which is exactly
    what a receipt looks like before the safe head has caught up. The run must not reconcile
    from it. Accepting inclusion here would write a fact the chain has not yet agreed to keep,
    and a reorg would leave a memory nothing on chain supports.
    """

    chain = FakeChain(progress={"i-release": ["pending", "included_success"]})
    run = make_run(environment)
    runner(chain, clock=bounded_clock()).execute(run)

    assert run.status == executor.FAILED
    assert "gave up" in run.error
    assert "reconcile" not in chain.commands()
    assert run.step("release").status == "done"
    assert run.step("confirm").status == executor.FAILED


def test_a_transaction_that_ends_badly_stops_the_run_instead_of_polling(environment):
    """A reverted or stuck transaction is an answer, not an absence.

    Polling on until the timeout would turn a two second refusal into a three minute one, and
    would report it as "gave up waiting", which names the wrong cause and sends whoever reads
    it looking at the network instead of at the deal.
    """

    chain = FakeChain(progress={"i-accept": ["pending", "included_reverted"]})
    run = make_run(environment)
    runner(chain, clock=bounded_clock()).execute(run)

    assert run.status == executor.FAILED
    # `"included_reverted" in run.error` was the first assertion here and it could not fail.
    # The timeout message quotes the last status it saw, so deleting the early stop still
    # produced an error containing the word: the run polled for three minutes and then said
    # "gave up ... Last seen: included_reverted". Both the ending and the absence of a timeout
    # have to be named, and the poll count is what actually separates them.
    assert "ended as included_reverted" in run.error
    assert "gave up" not in run.error
    assert chain.commands().count("tx-resolve") <= 6
    assert "mark-delivered" not in chain.commands()


def test_a_failed_run_leaves_no_step_saying_it_is_still_running(environment):
    """A step stuck at `running` after the run is over is a spinner that never stops."""

    chain = FakeChain(progress={"i-accept": ["pending", "stuck"]})
    run = make_run(environment)
    runner(chain, clock=bounded_clock()).execute(run)

    assert run.status == executor.FAILED
    assert executor.RUNNING not in {step.status for step in run.steps}
    assert run.step("accept").status == executor.FAILED
    assert run.step("accept").finished_at is not None


def test_a_wallet_that_cannot_finish_the_run_stops_it_before_the_first_transaction(environment):
    """The check that matters is the one before the deal exists.

    `chain.require_affordable` guards each send, correctly, and by the time it fires on the
    provider's acceptance the buyer's price is already in escrow and the visitor is looking at
    a half-finished lifecycle. One RPC read here turns that into a sentence.
    """

    chain = FakeChain()
    run = make_run(environment)
    runner(chain, balances={"buyer": 1, "provider": 10**18}).execute(run)

    assert run.status == executor.FAILED
    assert "topping up" in run.error
    assert chain.commands() == ["policy"]


def test_the_provider_side_is_checked_too_and_only_needs_the_bond(environment):
    """The provider posts a bond, not the price, so the two wallets are checked separately.

    Checking the provider against the price would refuse runs it could comfortably afford, and
    checking the buyer against the bond would admit runs it cannot.
    """

    chain = FakeChain()
    # Enough for the bond of 2480 bps on 1.18e14 and the gas allowance, and nowhere near the
    # price. The provider never pays the price, so this must run.
    bond = 118_000_000_000_000 * 2480 // 10_000
    run = make_run(environment)
    runner(
        chain, balances={"buyer": 10**18, "provider": bond + executor.GAS_ALLOWANCE_WEI}
    ).execute(run)
    assert run.status == executor.SUCCEEDED, run.error


def test_the_shipped_ceiling_admits_the_page_s_own_default_baseline(environment, monkeypatch):
    """A guard that refuses the demo's front page is not a guard, it is an outage.

    The first ceiling here was 5e12 while the page opens at a baseline of 1e14, which the
    urgent profile settles at 1.18e14. Every judge pressing the button on an untouched page
    would have met a refusal about a limit they had not gone near. This pins the relationship
    rather than the number, so changing either one deliberately is fine and changing one by
    accident is not.
    """

    monkeypatch.undo()
    monkeypatch.setenv("WRASSE_BUYER_ADDRESS", "0x30C95B7eb3E08F83992E803Be2A5AB0E0af93d22")
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", "0x0b920573ADf657f45Fecd9f7e48e66B5535A90C0")
    monkeypatch.setattr(executor, "POLL_SECONDS", 0)

    settled = json.loads(SAMPLE.read_text())["buyer"]["profiles"]["urgent"]["terms"]["price_wei"]
    assert settled <= executor.MAX_PRICE_WEI, (
        f"the demo settles at {settled} wei and the ceiling is {executor.MAX_PRICE_WEI}"
    )


# ------------------------------------------------------------------------------------------
# The two outcomes that cost real time, because the contract enforces the deadlines.
# ------------------------------------------------------------------------------------------


def _waiting_chain(chain: FakeChain, clock_values, seen=None):
    """A runner whose chain clock advances through the supplied values.

    `seen` collects every reading. Asserting the command order alone did not test the wait at
    all: with the wait deleted the sequence is identical and only the timing changes, so two
    mutations survived until these tests started counting how many times the clock was asked.
    """

    times = iter(clock_values)

    def now():
        value = next(times)
        if seen is not None:
            seen.append(value)
        return value

    return Runner(
        command=chain, deal_id_reader=lambda tx_hash: 7,
        balance_reader=lambda: RICH,
        deal_reader=lambda deal_id: {"deadline": 1000, "payout_available_at": 2000},
        chain_now=now, sleep=lambda _: None, clock=bounded_clock(),
    )


def test_a_timeout_is_produced_rather_than_asserted(environment):
    """Nothing is delivered, the window runs out, and the buyer takes the deal back.

    The point of doing it this way is that the receipt at the end is the outcome. A page that
    let a visitor declare a timeout would be teaching a memory something nobody could check,
    which is the one thing this design refuses.
    """

    chain = FakeChain()
    seen: list[int] = []
    run = make_run(environment)
    run.outcome = executor.TIMEOUT
    run.steps = [executor.Step(*step) for step in executor.SHAPES[executor.TIMEOUT]]
    _waiting_chain(chain, [900, 950, 1010, 1010, 1010, 1010], seen).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    # It waited: the clock was read while the deadline was still ahead, more than once, and the
    # claim only went out once a reading was past it.
    assert seen[:3] == [900, 950, 1010]
    lifecycle = [name for name in chain.commands() if name != "tx-resolve"]
    assert lifecycle == ["policy", "create-deal", "accept-deal", "claim-timeout", "reconcile"]
    assert "mark-delivered" not in chain.commands()
    assert "release-deal" not in chain.commands()


def test_a_delayed_claim_delivers_first_then_waits(environment):
    """Delivered, not released, and the seller waits out the delay it agreed to."""

    chain = FakeChain()
    seen: list[int] = []
    run = make_run(environment)
    run.outcome = executor.DELAYED
    run.steps = [executor.Step(*step) for step in executor.SHAPES[executor.DELAYED]]
    _waiting_chain(chain, [1500, 1900, 2010, 2010, 2010, 2010], seen).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    # And it waited on the payment delay, which is a later deadline than the delivery window.
    assert seen[:3] == [1500, 1900, 2010]
    lifecycle = [name for name in chain.commands() if name != "tx-resolve"]
    assert lifecycle == [
        "policy", "create-deal", "accept-deal", "mark-delivered", "claim-payment", "reconcile"
    ]
    assert "release-deal" not in chain.commands()


def test_the_wait_is_measured_against_the_chain_and_the_deal(environment):
    """Not against this machine's clock, and not against the terms.

    A deadline is enforced by the block that mines the transaction, and the window runs from
    the block that mined the acceptance rather than from when this process sent it. A wait
    derived from either of the convenient wrong sources is a transaction that reverts for a
    reason nobody can see afterwards.
    """

    chain = FakeChain()
    INTENTS["claim-timeout"] = "i-claim"
    asked = []
    run = make_run(environment)
    run.outcome = executor.TIMEOUT
    run.steps = [executor.Step(*step) for step in executor.SHAPES[executor.TIMEOUT]]

    times = iter([900, 1010, 1010, 1010, 1010])
    Runner(
        command=chain, deal_id_reader=lambda tx_hash: 7, balance_reader=lambda: RICH,
        deal_reader=lambda deal_id: (asked.append(deal_id) or
                                     {"deadline": 1000, "payout_available_at": 2000}),
        chain_now=lambda: next(times), sleep=lambda _: None, clock=bounded_clock(),
    ).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    assert asked == [7], "the deadline must come from the deal the contract is holding"


# ------------------------------------------------------------------------------------------
# The waiting endings, and the budget they are actually held to.
# ------------------------------------------------------------------------------------------


def _waiting_run(environment, outcome):
    run = make_run(environment)
    run.outcome = outcome
    run.steps = [executor.Step(*step) for step in executor.SHAPES[outcome]]
    return run


@pytest.mark.parametrize(
    ("outcome", "term", "settled"),
    [(executor.TIMEOUT, "service window", 300), (executor.DELAYED, "payout delay", 900)],
)
def test_an_ending_nobody_can_wait_out_is_refused_before_the_deal_exists(
    environment, monkeypatch, outcome, term, settled
):
    """The deposit is not locked up to discover a number that was known one step earlier.

    Both of these endings are produced by letting a deadline the contract enforces actually
    pass, so the run has to stay alive for the whole of it. The settled number is in the
    document before anything is signed. Comparing it there costs nothing; comparing it after
    `acceptDeal` costs the visitor their run, their gas, and a price and a stake locked inside
    a deal this procedure has already given up on.
    """

    monkeypatch.setattr(executor, "WAIT_TIMEOUT", 120.0)
    chain = FakeChain()
    run = _waiting_run(environment, outcome)

    runner(chain, clock=bounded_clock()).execute(run)

    assert chain.commands() == ["policy"], "nothing may be signed for a run that cannot finish"
    assert run.status == executor.FAILED
    assert str(settled) in run.error and term in run.error, run.error


def test_the_ending_that_waits_for_nothing_is_never_refused_for_waiting(
    environment, monkeypatch
):
    """The guard is about the ending, not about the terms.

    A delivered-and-paid run outlasts no deadline at all, so a budget below every settled
    duration must not touch it. Without this the same constant that protects two endings
    quietly deletes the third.
    """

    monkeypatch.setattr(executor, "WAIT_TIMEOUT", 1.0)
    chain = FakeChain()
    run = make_run(environment)

    runner(chain, clock=bounded_clock()).execute(run)

    assert run.status == executor.SUCCEEDED, run.error


def test_the_wait_is_bounded_by_its_own_budget_and_not_the_confirmation_one(environment):
    """Two different questions, and for a while they shared a constant.

    A confirmation wait is bounded by how far behind the tip Base's safe head runs, which is a
    property of the chain. This wait is bounded by a number the visitor typed. Sharing
    `CONFIRMATION_TIMEOUT` at 420 seconds made every ending that waits fail at the default
    baseline, because the settled payout delay there is 900.

    The clock advances a minute per read, so the two budgets are distinguishable by how many
    readings a stalled wait takes: seven for the confirmation budget, twenty for this one.
    """

    chain = FakeChain()
    run = _waiting_run(environment, executor.TIMEOUT)
    reads = []

    def clock():
        reads.append(len(reads) * 60)
        return reads[-1]

    Runner(
        command=chain, deal_id_reader=lambda tx_hash: 7, balance_reader=lambda: RICH,
        deal_reader=lambda deal_id: {"deadline": 10**9, "payout_available_at": 10**9},
        chain_now=lambda: 0, sleep=lambda _: None, clock=clock,
    ).execute(run)

    assert run.status == executor.FAILED
    assert "gave up waiting" in run.error, run.error
    assert reads[-1] >= executor.WAIT_TIMEOUT > executor.CONFIRMATION_TIMEOUT, (
        "a stalled wait gave up on the confirmation budget rather than its own"
    )


# ------------------------------------------------------------------------------------------
# The refund, which has to look at both sides of the escrow.
# ------------------------------------------------------------------------------------------


def _refund_run(tmp_path):
    return Run(
        run_id="r", session_id="s", kind=executor.REFUND, profile="", baseline={},
        paths={"buyer": tmp_path / "b.db", "provider": tmp_path / "p.db"},
        workdir=tmp_path,
    )


class _Withdrawals:
    def __init__(self):
        self.roles = []

    def __call__(self, argv, env, timeout):
        if argv[0] == "withdraw":
            self.roles.append(argv[argv.index("--role") + 1])
            return 0, json.dumps(
                {"intent_id": f"i-{len(self.roles)}", "tx_hash": "0xw", "status": "pending"}
            ), ""
        if argv[0] == "tx-resolve":
            return 0, json.dumps(
                [{"intent_id": f"i-{n}", "status": "included_success"}
                 for n in range(1, len(self.roles) + 1)]
            ), ""
        raise AssertionError(argv[0])


def test_a_session_whose_only_outcome_was_a_timeout_still_gets_its_money_back(
    environment, tmp_path
):
    """`claimTimeout` credits the buyer, and the refund used to collect only the seller.

    So a visitor who produced the failure the demo exists to show left the price and the stake
    sitting in the escrow, and `withdraw` reverts on a zero credit, so the refund reported a
    failure while the money it never looked at stayed put.
    """

    withdrawals = _Withdrawals()
    run = _refund_run(tmp_path)

    Runner(
        command=withdrawals,
        balance_reader=lambda: {"buyer": 1, "provider": 10**18},
        credit_reader=lambda: {"buyer": 5 * 10**14, "provider": 0},
        sleep=lambda _: None,
    ).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    assert withdrawals.roles == ["buyer"]


def test_a_refund_collects_every_side_the_escrow_is_holding_for(environment, tmp_path):
    """A session of mixed endings leaves a credit on both sides, and both are owed back."""

    withdrawals = _Withdrawals()
    run = _refund_run(tmp_path)

    Runner(
        command=withdrawals,
        balance_reader=lambda: {"buyer": 1, "provider": 10**18},
        credit_reader=lambda: {"buyer": 10**14, "provider": 10**14},
        sleep=lambda _: None,
    ).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    assert sorted(withdrawals.roles) == ["buyer", "provider"]


def test_an_empty_escrow_is_not_a_failed_refund(environment, tmp_path):
    """Nothing to collect is an answer, and sending a transaction to prove it is not.

    `withdraw` reverts on a zero credit, so asking anyway would spend gas to produce an error
    and show the visitor a failure at the end of a session that went perfectly well.
    """

    withdrawals = _Withdrawals()
    run = _refund_run(tmp_path)

    Runner(
        command=withdrawals,
        balance_reader=lambda: {"buyer": 1, "provider": 10**18},
        credit_reader=lambda: {"buyer": 0, "provider": 0},
        sleep=lambda _: None,
    ).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    assert withdrawals.roles == []
    assert run.refund_wei == 0


# ------------------------------------------------------------------------------------------
# Closing the deals a failed run left open, which `withdraw` cannot see.
# ------------------------------------------------------------------------------------------


class _Recovering(_Withdrawals):
    """A chain that accepts the three closing commands as well as a withdrawal."""

    CLOSERS = ("cancel-unaccepted", "claim-timeout", "release-deal")

    def __init__(self):
        super().__init__()
        self.closed = []

    def __call__(self, argv, env, timeout):
        if argv[0] in self.CLOSERS:
            self.closed.append((argv[0], argv[argv.index("--deal-id") + 1]))
            return 0, json.dumps(
                {"intent_id": f"c-{len(self.closed)}", "tx_hash": "0xc", "status": "pending"}
            ), ""
        if argv[0] == "tx-resolve":
            rows = [{"intent_id": f"c-{n}", "status": "included_success"}
                    for n in range(1, len(self.closed) + 1)]
            rows += [{"intent_id": f"i-{n}", "status": "included_success"}
                     for n in range(1, len(self.roles) + 1)]
            return 0, json.dumps(rows), ""
        return super().__call__(argv, env, timeout)


class FakeLiabilities:
    """The durable index of open deals, in memory.

    Injected rather than pointed at a scratch file because what these tests are about is which
    deals the procedure asks for and which it writes off, not SQLite. It keeps the real shape:
    a row exists before a transaction is signed and gains its hash and then its id, because the
    ordering is the property under test rather than an implementation detail.
    """

    def __init__(self, open_by_session=None, extra_rows=None):
        self.entries: list[dict] = list(extra_rows or [])
        for session_id, ids in (open_by_session or {}).items():
            for deal_id in ids:
                self.entries.append({
                    "intent_id": f"pre-{deal_id}", "session_id": session_id,
                    "tx_hash": f"0x{deal_id}", "deal_id": int(deal_id),
                })
        self.closed: list[int] = []
        self.discarded: list[str] = []

    def open_intent(self, intent_id, session_id, detail=None):
        self.entries.append({"intent_id": str(intent_id), "session_id": session_id,
                             "tx_hash": None, "deal_id": None})

    def attach(self, intent_id, *, tx_hash=None, deal_id=None):
        for row in self.entries:
            if row["intent_id"] == str(intent_id):
                if tx_hash is not None:
                    row["tx_hash"] = tx_hash
                if deal_id is not None:
                    row["deal_id"] = int(deal_id)

    def discard(self, intent_id):
        self.discarded.append(str(intent_id))
        self.entries = [r for r in self.entries
                        if not (r["intent_id"] == str(intent_id) and r["deal_id"] is None)]

    def close(self, deal_id):
        self.closed.append(int(deal_id))
        self.entries = [r for r in self.entries if r["deal_id"] != int(deal_id)]

    def rows(self, session_id=None):
        if session_id is None:
            return list(self.entries)
        return [r for r in self.entries if r["session_id"] == session_id]

    def unresolved(self, session_id=None):
        return [r for r in self.rows(session_id) if r["deal_id"] is None and r["tx_hash"]]

    def open_deals(self, session_id=None):
        return sorted(r["deal_id"] for r in self.rows(session_id) if r["deal_id"] is not None)


def _recovery_runner(chain, deals, liabilities=None, **kwargs):
    return Runner(
        command=chain,
        balance_reader=lambda: {"buyer": 1, "provider": 10**18},
        credit_reader=lambda: {"buyer": 0, "provider": 10**14},
        deal_reader=lambda deal_id: deals[deal_id],
        chain_now=lambda: 10**9,
        liabilities=liabilities or FakeLiabilities(),
        sleep=lambda _: None, clock=bounded_clock(), **kwargs,
    )


@pytest.mark.parametrize(
    ("state", "command"),
    [
        ("Offered", "cancel-unaccepted"),
        ("Accepted", "claim-timeout"),
        ("Delivered", "release-deal"),
    ],
)
def test_a_deal_a_failed_run_left_open_is_closed_before_the_escrow_is_collected(
    environment, tmp_path, state, command
):
    """`withdraw` collects credits, and an unfinished deal has assigned none.

    So a settlement that failed after `createDeal` left the price sitting inside a live deal
    that no endpoint could reach, and the money stayed there until somebody ran a lifecycle
    command by hand. Every state the contract can leave a deal in has exactly one action that
    ends it, and the refund takes it.
    """

    chain = _Recovering()
    deals = {4: {"state": state, "accept_by": 1, "deadline": 1, "payout_available_at": 1}}
    run = _refund_run(tmp_path)
    held = FakeLiabilities({run.session_id: [4]})

    _recovery_runner(chain, deals, held).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    assert chain.closed == [(command, "4")], "the open deal was left holding its deposit"
    assert chain.roles == ["provider"], "and the collection still happened afterwards"


@pytest.mark.parametrize("state", ["Released", "TimedOut", "Cancelled"])
def test_a_deal_that_already_ended_is_not_touched(environment, tmp_path, state):
    """It has assigned its credits already, and a second closing action would only revert."""

    chain = _Recovering()
    deals = {4: {"state": state, "accept_by": 1, "deadline": 1, "payout_available_at": 1}}
    run = _refund_run(tmp_path)
    held = FakeLiabilities({run.session_id: [4]})

    _recovery_runner(chain, deals, held).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    assert chain.closed == []


def test_closing_a_deal_waits_for_the_deadline_the_contract_will_check(environment, tmp_path):
    """`claimTimeout` reverts before the deadline, so the recovery has to sit it out too."""

    chain = _Recovering()
    deals = {4: {"state": "Accepted", "accept_by": 1, "deadline": 500,
                 "payout_available_at": 1}}
    run = _refund_run(tmp_path)
    seen = []
    times = iter([100, 200, 600, 600, 600, 600])

    Runner(
        command=chain,
        balance_reader=lambda: {"buyer": 1, "provider": 10**18},
        credit_reader=lambda: {"buyer": 0, "provider": 10**14},
        deal_reader=lambda deal_id: deals[deal_id],
        chain_now=lambda: seen.append(next(times)) or seen[-1],
        liabilities=FakeLiabilities({run.session_id: [4]}),
        sleep=lambda _: None, clock=bounded_clock(),
    ).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    assert chain.closed == [("claim-timeout", "4")]
    assert len([t for t in seen if t < 504]) >= 2, "it did not wait for the deadline"


def test_every_deal_the_session_opened_is_closed_not_just_the_last(environment, tmp_path):
    """A visitor gets five runs, and any of them can be the one that failed."""

    chain = _Recovering()
    deals = {
        1: {"state": "Released", "accept_by": 1, "deadline": 1, "payout_available_at": 1},
        2: {"state": "Offered", "accept_by": 1, "deadline": 1, "payout_available_at": 1},
        3: {"state": "Delivered", "accept_by": 1, "deadline": 1, "payout_available_at": 1},
    }
    run = _refund_run(tmp_path)
    held = FakeLiabilities({run.session_id: [1, 2, 3]})

    _recovery_runner(chain, deals, held).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    assert chain.closed == [("cancel-unaccepted", "2"), ("release-deal", "3")]


def test_a_reclaim_resolves_what_the_previous_process_left_open(environment, tmp_path):
    """The ledger survives a restart; the queue, the sessions and the runs do not.

    A row left holding a nonce makes the first visitor after a restart meet a wallet that
    refuses to sign for a transaction they know nothing about. Resolving by query is all this
    does: it asks the chain what happened and records the answer.
    """

    calls = []

    def command(argv, env, timeout):
        calls.append(argv[0])
        if argv[0] == "tx-resolve":
            return 0, json.dumps([
                {"intent_id": "i-1", "status": "confirmed_success", "action": "resolved"},
                {"intent_id": "i-2", "status": "confirmed_success", "action": "none"},
            ]), ""
        raise AssertionError(argv[0])

    run = Run(
        run_id="r", session_id="", kind=executor.RECLAIM, profile="", baseline={},
        paths={"buyer": tmp_path / "b.db", "provider": tmp_path / "p.db"}, workdir=tmp_path,
    )
    Runner(command=command, sleep=lambda _: None,
           liabilities=FakeLiabilities()).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    assert calls == ["tx-resolve"], "a reclaim with nothing to close must not send anything"
    assert "1 of 2" in run.steps[0].detail


def test_a_reclaim_refuses_to_open_the_queue_while_a_wallet_is_still_held(
    environment, tmp_path
):
    """`tx-resolve` exits zero having said a transaction is absent and needs a rebroadcast.

    The row and its nonce stay exactly where they were, so counting a successful exit as a
    reclaimed wallet let the next visitor's run reach a ledger that would refuse it. Every
    row's status has to be read, not the command's.
    """

    def command(argv, env, timeout):
        return 0, json.dumps([
            {"intent_id": "i-1", "status": "included_success", "action": "resolved"},
            {"intent_id": "i-2", "status": "unknown",
             "action": "pass --rebroadcast to resend the identical bytes"},
        ]), ""

    run = Run(
        run_id="r", session_id="", kind=executor.RECLAIM, profile="", baseline={},
        paths={"buyer": tmp_path / "b.db", "provider": tmp_path / "p.db"}, workdir=tmp_path,
    )
    Runner(command=command, sleep=lambda _: None,
           liabilities=FakeLiabilities()).execute(run)

    assert run.status == executor.FAILED
    assert "i-2 is unknown" in run.error
    assert "nonce is not free" in run.error


def test_a_reclaim_closes_the_deals_a_previous_process_left_behind(environment, tmp_path):
    """The whole point of writing them down. The sessions that opened them no longer exist."""

    chain = _Recovering()
    deals = {8: {"state": "Accepted", "accept_by": 1, "deadline": 1, "payout_available_at": 1}}
    held = FakeLiabilities({"a session that died": [8]})
    run = Run(
        run_id="r", session_id="", kind=executor.RECLAIM, profile="", baseline={},
        paths={"buyer": tmp_path / "b.db", "provider": tmp_path / "p.db"}, workdir=tmp_path,
    )

    Runner(
        command=chain, balance_reader=lambda: {"buyer": 1, "provider": 10**18},
        credit_reader=lambda: {"buyer": 0, "provider": 10**14},
        deal_reader=lambda deal_id: deals[deal_id], chain_now=lambda: 10**9,
        liabilities=held, sleep=lambda _: None, clock=bounded_clock(),
    ).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    assert chain.closed == [("claim-timeout", "8")]
    assert held.closed == [8], "the index still lists a deal that has been closed"
    assert chain.roles == ["provider"], "and the money it released was collected"


def test_a_reclaim_that_cannot_reach_the_chain_fails_rather_than_reporting_success(
    environment, tmp_path
):
    """Reporting a clean ledger it never read would let the next run take a held nonce."""

    def command(argv, env, timeout):
        return 1, "", "the rpc is unreachable"

    run = Run(
        run_id="r", session_id="", kind=executor.RECLAIM, profile="", baseline={},
        paths={"buyer": tmp_path / "b.db", "provider": tmp_path / "p.db"}, workdir=tmp_path,
    )
    Runner(command=command, sleep=lambda _: None).execute(run)

    assert run.status == executor.FAILED
    assert "unreachable" in run.error


def test_the_queue_refuses_to_settle_while_the_reclaim_says_a_wallet_is_held(
    environment, tmp_path
):
    """Freeing the wallets is a gate, not a report.

    The reclaim runs first, which put it in the right order and nothing more: the worker went
    on to the next run whatever it concluded. A settlement admitted then meets the ledger's
    refusal three transactions in, with a deal already open and a visitor watching.
    """

    class Failing(Runner):
        def __init__(self):
            pass

        def execute(self, run):
            if run.kind == executor.RECLAIM:
                run.status = executor.FAILED
                run.error = "i-2 is unknown; the nonce is not free"
                return
            run.status = executor.SUCCEEDED
            raise AssertionError("a settlement ran while a wallet was still held")

    queue = executor.Queue(Failing())
    reclaim = Run(
        run_id="reclaim", session_id="", kind=executor.RECLAIM, profile="", baseline={},
        paths={}, workdir=tmp_path,
    )
    settlement = make_run(environment)
    queue.submit(reclaim)
    queue.submit(settlement)

    deadline = time.time() + 5
    while time.time() < deadline and settlement.status in (executor.QUEUED, executor.RUNNING):
        time.sleep(0.01)
    queue.stop()

    assert settlement.status == executor.FAILED
    assert "cannot sign until its wallets are free" in settlement.error
    assert "the nonce is not free" in settlement.error
    assert all(
        step.status != executor.PENDING for step in settlement.steps
    ), "a refused run must not leave steps looking like they are still going to happen"


def test_a_refund_still_runs_when_the_queue_is_closed_to_settlements(environment, tmp_path):
    """Collecting and closing is exactly what a service in that state should still do."""

    ran = []

    class Failing(Runner):
        def __init__(self):
            pass

        def execute(self, run):
            ran.append(run.kind)
            if run.kind == executor.RECLAIM:
                run.status = executor.FAILED
                run.error = "a wallet is held"
                return
            run.status = executor.SUCCEEDED

    queue = executor.Queue(Failing())
    queue.submit(Run(run_id="reclaim", session_id="", kind=executor.RECLAIM, profile="",
                     baseline={}, paths={}, workdir=tmp_path))
    refund = _refund_run(tmp_path)
    queue.submit(refund)

    deadline = time.time() + 5
    while time.time() < deadline and refund.status in (executor.QUEUED, executor.RUNNING):
        time.sleep(0.01)
    queue.stop()

    assert refund.status == executor.SUCCEEDED, refund.error
    assert ran == [executor.RECLAIM, executor.REFUND]


def test_a_deal_is_written_down_before_the_transaction_that_creates_it_is_signed(
    environment, tmp_path
):
    """The id is knowable only after the value has already moved.

    So writing the row when the id arrives leaves a window in which creation is included, the
    buyer's ETH is in an Offered deal, and a crash takes the only thing that could find it.
    The row exists before the send; the hash and the id are attached to it as they become
    known.
    """

    chain = FakeChain()
    held = FakeLiabilities()
    run = make_run(environment)
    order: list[str] = []

    class Watching(FakeChain):
        def __call__(self, argv, env, timeout):
            if argv[0] == "create-deal":
                order.append("row before send" if held.rows() else "SEND BEFORE ROW")
            return FakeChain.__call__(self, argv, env, timeout)

    runner(Watching(), clock=bounded_clock(), liabilities=held).execute(run)

    assert order == ["row before send"], "the deposit could move with nothing pointing at it"
    del chain


def test_a_creation_left_unnamed_by_a_crash_is_resolved_from_its_receipt(
    environment, tmp_path
):
    """A row with a hash and no id is the crash window, and the hash is enough to close it."""

    chain = _Recovering()
    held = FakeLiabilities(extra_rows=[{
        "intent_id": "interrupted", "session_id": "a session that died",
        "tx_hash": "0xcreate", "deal_id": None,
    }])
    deals = {11: {"state": "Offered", "accept_by": 1, "deadline": 1, "payout_available_at": 1}}
    run = Run(
        run_id="r", session_id="", kind=executor.RECLAIM, profile="", baseline={},
        paths={"buyer": tmp_path / "b.db", "provider": tmp_path / "p.db"}, workdir=tmp_path,
    )

    Runner(
        command=chain, deal_id_reader=lambda tx_hash: 11,
        balance_reader=lambda: {"buyer": 1, "provider": 10**18},
        credit_reader=lambda: {"buyer": 10**14, "provider": 0},
        deal_reader=lambda deal_id: deals[deal_id], chain_now=lambda: 10**9,
        liabilities=held, sleep=lambda _: None, clock=bounded_clock(),
    ).execute(run)

    assert run.status == executor.SUCCEEDED, run.error
    assert chain.closed == [("cancel-unaccepted", "11")], (
        "an offered deal nobody had named kept its deposit"
    )
    assert held.closed == [11]


def test_a_creation_that_was_never_recorded_as_sent_closes_the_queue(environment, tmp_path):
    """Whether value moved is not knowable from here, so nothing more may be signed."""

    held = FakeLiabilities(extra_rows=[{
        "intent_id": "blind", "session_id": "s", "tx_hash": None, "deal_id": None,
    }])
    run = Run(
        run_id="r", session_id="", kind=executor.RECLAIM, profile="", baseline={},
        paths={"buyer": tmp_path / "b.db", "provider": tmp_path / "p.db"}, workdir=tmp_path,
    )

    def command(argv, env, timeout):
        return 0, json.dumps([]), ""

    Runner(command=command, liabilities=held, sleep=lambda _: None).execute(run)

    assert run.status == executor.FAILED
    assert "blind" in run.error
    assert "not knowable" in run.error


def test_a_creation_refused_before_broadcast_leaves_no_liability(environment, tmp_path):
    """Nothing reached a node, so a row would be a liability that does not exist."""

    class Refusing(FakeChain):
        def __call__(self, argv, env, timeout):
            if argv[0] == "create-deal":
                return 0, json.dumps({
                    "intent_id": "i-create", "tx_hash": None,
                    "status": executor.UNBROADCAST_STATUS,
                    "error": "broadcasting was not opted into",
                }), ""
            return FakeChain.__call__(self, argv, env, timeout)

    held = FakeLiabilities()
    run = make_run(environment)

    runner(Refusing(), clock=bounded_clock(), liabilities=held).execute(run)

    assert run.status == executor.FAILED
    assert held.rows() == [], "an unsent creation was left recorded as a live deposit"
    assert held.discarded == [run.run_id]

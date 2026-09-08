# Adversarial service review

Range reviewed: `81674a3..ff00689` (`ff00689`), branch `rename-and-bilateral`.
I read `docs/REVIEW-PROTOCOL.md` and applied U1–U9. The cold-read inventory was:

1. session creation can copy missing/uninitialised warm stores;
3. a chain failure after creation leaves escrow liabilities with no recovery;
4. in-memory queue/session state is lost on restart;
5. refund state is marked before refund success and execution remains allowed afterward;
6. the new one-sided agent process is not on the real policy/execution path;
7. session eviction can delete a live run's files.

The U7 counts below are indicative, not rigorous: this was one prompt containing both the
protocol and the author's steering questions, so the prompt was already visible while forming
the cold list.

## Findings

### MAJOR — a session can be created before the warm source exists, silently starting cold

**OUTSIDE section 3's listed concerns (session lifecycle).**

`wrasse/service.py:467-473` creates a session without first calling `_open(WARM)` or
`prepare_working_copies`. `wrasse/sessions.py:114-140` then calls `copy_database` for the paths in
`_PATHS[WARM]`; `copy_database` silently skips missing source files at lines 57-64 and still
returns a session. The warm working copies are only created/validated from `_open` at
`wrasse/service.py:190-198`.

Concrete sequence: a visitor opens the executing deployment and presses the real-run button
before making a quote request. `/api/session` copies no databases, so the first policy runs
against newly created cold stores rather than the seeded two-receipt history. The next quote can
then refuse for missing dimensions or show a cold-start result, while the page claims the visitor
started from the observed memories. This is a wrong memory/term result, not merely a cosmetic
startup detail. Initialise and validate the warm pair before allowing session creation, or make
`copy_database` fail loudly when either source is absent.

### MAJOR — failures after `createDeal` leave money in escrow with no automatic recovery

**INSIDE section 3's money-path concern.**

`wrasse/executor.py:459-500` stops on `ExecutionError` and marks the run failed. There is no
cleanup path for a deal that has already been created: `_create` at lines 763-782 may have
escrowed the price, then `accept`, `deliver`, `release`, a wait, confirmation, or reconciliation
can fail. `/api/finish` only queues `withdraw` for already credited balances; it cannot cancel an
open offer or claim a timed-out accepted deal. `wrasse/executor.py:620-628` only marks UI steps
failed.

Concrete sequence: `createDeal` is included, `acceptDeal` reverts or the RPC times out, and the
worker reports a failed run. The buyer's price remains an open contract liability; the visitor
cannot recover it from the page, and subsequent runs can leave more money locked. The same occurs
after acceptance if delivery/release fails until a human runs the appropriate later lifecycle
command. Add an explicit recovery state/procedure for every post-creation failure, or refuse to
advertise the run as finished until the deal is terminal and its credit is refundable.

### MAJOR — a process restart loses the only objects that know how to finish a run

**OUTSIDE section 3's listed concerns (deployment/recovery).**

`wrasse/service.py:394-395` keeps `_QUEUE` and `_SESSIONS` only in process memory;
`wrasse/executor.py:848-855` keeps queued/running `Run` objects only in memory; and
`wrasse/sessions.py:110-113` keeps the session registry only in memory. The ledger is persistent,
but it stores transaction intents, not the run procedure, session id, outcome, paths, or refund
state.

Concrete sequence: Railway restarts after a transaction is sent or while a run is waiting for the
safe head. The new process has no session or run id, `/api/run/{id}` and `/api/quote?session_id=`
return 404, and no worker resumes the unresolved ledger row, teaches the copied memories, or
refunds the escrow. The deployment's restart policy therefore turns a normal restart into a
money-and-memory interruption. Persist run/session state or add startup recovery that reconstructs
and resumes/terminates every in-flight ledger action before accepting visitors.

### MAJOR — refund bookkeeping permits post-refund runs and makes failed refunds unretryable

**INSIDE section 3's money-path concern.**

`wrasse/service.py:543-587` sets `session.refunded = True` before the worker has performed a
withdrawal. `wrasse/sessions.py:146-155` checks only the run count, not `refunded`, so
`POST /api/execute` still accepts new runs after `/api/finish`. Meanwhile
`wrasse/executor.py:564-571` can fail a refund after one withdrawal, and the session remains
marked refunded; a later `/api/finish` returns the old run id instead of retrying. The same is
true if the process dies after the flag is set and before the worker succeeds.

Concrete sequences:

* finish a new session, then execute a settlement: the later credit is never collected because
  the session is already marked refunded;
* queue a refund, let one withdrawal revert or time out, then press Finish again: the remaining
  credit stays in escrow and the UI reports an already-refunded session.

Refuse execution once a session is finishing/refunded, and mark refund completion only after all
withdrawals have succeeded, with a durable retryable state for partial completion.

### MAJOR — the advertised two-agent isolation is not used by the real quote or executor

**OUTSIDE section 3's listed concerns (unit D/architecture).**

The new `wrasse/agent.py` and `agent-positions` command can publish one side from one store, but
the real path still invokes `policy` in `wrasse/executor.py:653-664`. That command calls
`wrasse/cli.py:_bilateral_quote` at lines 569-660, which opens and locks both stores and computes
both sides in one process. The service quote path does the same. There is no exchange/coordinator
that consumes two `agent-positions` outputs.

Concrete result: a judge inspecting the live execution can truthfully say that the settlement was
computed by one process with access to both memories, despite the page/docs presenting two literal
agents. The numbers may be correct, but the architectural claim is unverifiable and the new
isolation code is dead for the path that matters. Either wire the two one-sided processes into the
policy exchange, or narrow the public claim back to “two identified stores, one orchestrator.”

### MAJOR — session eviction deletes active or queued run databases

**OUTSIDE section 3's listed concerns (session lifecycle).**

`wrasse/sessions.py:136-140` calls `_evict` after every session creation, and `_evict` at
lines 157-165 unconditionally `shutil.rmtree`s the oldest session directory. It does not check
whether that session has a queued/running run, is waiting for confirmation, or has an unrefunded
escrow balance.

Concrete sequence: 200 sessions exist and one is still queued or executing; a stranger creates
the 201st. The oldest directory is deleted while the worker still holds its paths. The subprocess
then fails to open/reconcile the memory, and any subsequent refund cannot use the deleted session
paths. A public endpoint can reach this with session creation alone. Evict only sessions with no
active/queued run and no pending refund, or retain them until terminal cleanup.

### MINOR — refund is global wallet credit, not credit belonging to this session

**INSIDE section 3's money-path concern.**

`wrasse/executor.py:523-554` reads `withdrawable` for the two shared wallets and withdraws all
credit it finds. The escrow contract aggregates credits across every deal and has no session id.
Thus a session's `/api/finish` can collect credits created by another visitor's session, while
that other session later reports a refund with nothing to collect.

No value leaves the two operator wallets, so this is less severe than an external theft, but it
breaks the per-visitor accounting and can make the displayed refund/transaction belong to the
wrong visitor. The UI should say “collect all shared escrow credit” if that is intentional, or
the service needs global settlement/refund coordination rather than claiming session-local
refunds.

### MINOR — the run result still exposes raw wei in the user-facing live panel

**OUTSIDE section 3's listed concerns (page usability/units).**

`web/run-panel.html:270-282` renders `t.price_wei` with the literal `wei` label, while the input
surface uses ETH. This is technically labelled, so it is not a cryptographic mismatch, but a
judge who typed `0.0001 ETH` is shown `118000000000000 wei` in the most important settlement card.
The simulator has human-unit formatting, so the two panels disagree in readability. Convert the
display to ETH while retaining the exact wei value in a technical disclosure.

### MINOR — the live panel claims four transactions for a three-transaction timeout

**OUTSIDE section 3's listed concerns (page truthfulness).**

`web/run-panel.html:445` always renders “Four transactions and one confirmation”. The timeout
ending omits delivery and release, so its real chain sequence is create, accept, and
claim-timeout. A judge following the progress card sees a transaction count that cannot match
the receipt links or the run log. Render the count from the selected ending, or describe the
steps without a fixed number.

### MINOR — the page calls an included payout “paid” and “already checked” before confirmation

**OUTSIDE section 3's listed concerns (page truthfulness).**

`web/run-panel.html:270-282` displays “Settled on chain. Both sides are paid” as soon as the
release step is included. The same block says the agent has checked the receipt, sender, logs,
and escrow state, but the five-way reconciliation and memory write happen only in
`wrasse/executor.py:801-818`, after safe-head confirmation. Inclusion credits withdrawable escrow
balances; it does not yet put ETH in either wallet. A judge can therefore read a provisional
state as a confirmed, wallet-paid and memory-verified result. Split “included/credited” from
“confirmed/reconciled/withdrawn” and only claim the latter after those steps complete.

## Direct unit answers

**A — Run procedure.** The happy-path ordering and confirmed-only teaching are well covered by
the injected tests, but post-creation cleanup, restart recovery, and refund retry are not
guaranteed. The run can leave an open deal or an unrefundable credit.

**B — Sessions.** Copying WAL sidecars is the right shape, but creation-before-initialisation and
unconditional eviction violate the isolation guarantee under fresh startup and load.

**C — Service.** Execution is gated by the flag and health reports it. The service TestClient
suite did not complete here: the first quote test hung in the local AnyIO/TestClient harness.
Direct calls to the service open and quote helpers completed, so I cannot attribute that hang to
an application deadlock without a working HTTP test environment.

**D — Agent.** The one-sided publisher is testable in isolation, but the live coordinator does
not call it. The two-agent claim is therefore not true of the deployed execution path.

**E — Simulator.** It is stateless and uses the real engine/settlement code, and it refuses
unlearned outcomes by name. It is hypothetical by design; it does not create a real chain history
or write memories.

**F — Page.** The projection uses text nodes for dynamic values, which avoids an obvious HTML
injection path. The live result still uses raw wei for the price presentation.

**G — Deployment.** The Docker image, Railway build, and secret handling could not be built or
run here. Docker/Foundry availability and the platform's actual volume/healthcheck behavior are
unverified. In particular, the image's first real build remains a required pre-judge test.

**H — Documents/live check.** `scripts/live-demo-check.py` is valuable, but I could not run its
chain half. It does not exercise restart recovery, active-session eviction, post-create failure
cleanup, or a failed/partial refund.

## U8 limitations

I did not run Anvil or any live Base transaction. The non-Anvil pytest run stalled in
`tests/test_service.py` at the first quote test in this environment, so I do not treat the
advertised full green count as verified. I did not build Docker or deploy to Railway. The review
therefore cannot clear bytecode compilation, secret materialisation, volume mounts, or real RPC
behavior.

## U9 scope/root notes

All findings above have roots in this range. The deployed contract is unchanged and was not
re-reviewed except for the Python call/withdraw paths that depend on it. The five deferred Gate 4
orchestrator findings and the decisions explicitly listed in `KNOWN-LIMITS.md` are not repeated;
the post-creation cleanup and persistent-run-state gaps are not listed there and remain findings.

## Counts and priority

INSIDE section 3's stated concerns: **3 findings** (post-create escrow leak, refund lifecycle,
and global-credit attribution).

OUTSIDE section 3's stated concerns: **7 findings** (session-before-warm, restart loss, unused
agent split, active-session eviction, raw-wei presentation, fixed timeout transaction count, and
premature “paid/checked” copy).

Given three days, fix post-create recovery, persistent/restart recovery, session initialization,
and refund state first. The agent split is a pitch-integrity decision: wire it or narrow the claim
before judging. Eviction and page truthfulness follow if time remains, but neither should mask the
money and memory failures.

VERDICT: do not ship

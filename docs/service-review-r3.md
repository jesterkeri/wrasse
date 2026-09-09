# Adversarial service review — round three

Range reviewed: `6381093..0b90b32` at
`0b90b32b6b8eb64e3d5b37caf1fabac0b8eabd70`, branch
`rename-and-bilateral`.

I read `docs/REVIEW-PROTOCOL.md` and applied U1–U9. My cold-read list before
using the previous report as a closure checklist was:

1. the new liability database is updated after, rather than atomically with,
   discovering a created deal;
2. `spend()` and `begin_finishing()` share a lock but do not make admission and
   refund ordering one transition;
3. the new `finishing` claim can survive an exception before there is a queued
   refund to retry;
4. the storage limit still deliberately retains an unbounded set of busy
   sessions;
5. the entrypoint validates only one of the two seeded memories; and
6. the live check invokes the manual finish endpoint before asserting a refund.

The U7 measurement is indicative, not rigorous: the prompt's section 3 was
already visible in the same context window as the range, as U1 explains.

## Findings, ordered by cost of leaving them in tomorrow

### CRITICAL — a crash after `createDeal` is included can still lose the only recovery record

**INSIDE section 3 (the durable liability index). Root: inside this range.**

`Runner._create()` waits for the creation transaction to be included at
`wrasse/executor.py:930-943`, then separately reads the receipt for its deal id
at line 947, and only then writes the new durable record at lines 948-951.
`liabilities.record()` has no pre-deal or creation-intent row to bridge that
gap (`wrasse/liabilities.py:59-75`).

Concrete sequence:

1. `createDeal` is included and the buyer's ETH is in an Offered deal.
2. Railway restarts, the worker is killed, or the receipt/id RPC read fails
   after line 943 and before line 951.
3. The ledger can still resolve the creation transaction, but
   `liabilities.db` has no deal id. Boot reclaim reads only that index
   (`wrasse/executor.py:577-583`), so it cannot cancel the offered deal.

The buyer's price then remains in escrow until an operator discovers and closes
the deal manually. This contradicts the new README/DEPLOY claim that the index
records *every* deal once its id exists. The index fixes the ordinary restart
case, not the crash boundary it was introduced to make safe.

Persist a durable creation-intent record before signing, update it with the
transaction hash before/at broadcast, and have boot recovery parse that
creation receipt into a deal id. Deleting either record must happen only after
the contract is terminal. A test needs to fault precisely between inclusion and
`record()`; the present FakeLiabilities tests start after the id is already
known.

### MAJOR — Finish can still pass an admitted settlement and leave its credit with no later collection

**INSIDE section 3 (the two session flags). Root: the incomplete lock-based
fix is inside this range; the initial admission sequence predates it.**

The lock protects the individual calls to `Sessions.spend()`
(`wrasse/sessions.py:198-219`) and `begin_finishing()` (lines 176-189), but it
does not cover creating and submitting the settlement. `/api/execute` releases
the registry lock after `spend()` at `wrasse/service.py:610-616`, and does not
submit the run until line 629. `/api/finish` can therefore submit its refund in
between (`wrasse/service.py:685-724`).

Concrete sequence:

1. Request A calls `/api/execute`; `spend()` increments `session.runs`, then A
   is paused before `_queue().submit(run)`.
2. Request B calls `/api/finish`; it claims `finishing`, queues a refund, and
   the refund runs first. The liability index is still empty, so it collects
   nothing and succeeds.
3. A queues its already-admitted settlement. It then creates and terminates a
   deal after the refund.

On the next status poll `_reconcile_refund()` marks the session refunded
(`wrasse/service.py:662-682`). Later Finish requests return the old refund id
instead of collecting the newly credited ETH. If nobody polls, `finishing`
instead remains true and the session is unusable. The durable index cannot
repair this because there is no later refund scheduled for a session now
considered complete.

Make settlement admission, its queue publication (or a durable reservation),
and the decision to begin finishing one session state transition. Finish must
wait for every already-admitted run, not merely derive a recovery list when it
executes.

### MAJOR — a refund can become permanently “finishing” before it has a job to retry

**INSIDE section 3 (completion callback). Root: inside this range.**

`_refund()` sets `session.finishing` through `begin_finishing()` before it
creates the run directory, records `refund_run_id`, or submits the job
(`wrasse/service.py:691-724`). None of those later operations is compensated.
The worker deliberately swallows all completion-callback exceptions
(`wrasse/executor.py:1157-1164`).

Concrete sequence: the fifth run completes while the persistent volume is full,
or `workdir.mkdir()` at `wrasse/service.py:699-701` raises for an I/O error.
The automatic callback catches and discards that exception. The session stays
`finishing=True`, `refund_run_id=None`; every later Finish request reaches
lines 694-697 and returns no job id, so its credits cannot be retried or
collected. The same sequence through a manual Finish returns a 500 but leaves
the session stuck in exactly the same state.

Put all pre-submit work in a compensating `try` block that calls
`abandon_finishing()` on failure. Publish the run id and job while holding the
same session-state lock, and make callback failure visible in durable/session
state rather than silently dropping it.

### MAJOR — an early failed run still has no public recovery route

**OUTSIDE section 3 (existing failed-run recovery path). Root: outside this
range; this is round two's unclosed finding.**

The liability index records a known deal, but it is acted on only by a refund.
The worker queues an automatic refund only after the session has used all five
runs (`wrasse/service.py:421-440`). The page exposes Finish only following a
successful run (`web/run-panel.html:519-531`); the failed branch at lines
536-538 prints the error and leaves it hidden.

Concrete sequence: creation is included and recorded, then accept, delivery,
or RPC resolution fails on run one. The Offered/Accepted deal remains in the
index, but the visitor is shown only the failure. They cannot start the
recovery/refund from the page, and stopping after that run leaves the deposit
locked until a later operator action or until they happen to complete four more
runs.

Queue recovery/refund whenever a settlement finishes with an open liability,
or expose an explicit recovery operation in the failed state. Do not make a
visitor manufacture four more histories to recover the first deposit.

### MINOR — the stated session storage limit still does not bind under public load

**OUTSIDE section 3 (session capacity). Root: partly outside the range; this
fold changes the busy predicate but preserves the bypass.**

`Sessions._evict()` deliberately retains each busy session and restores it to
`_order` (`wrasse/sessions.py:236-254`). A queued run is busy
(`wrasse/executor.py:1107-1119`), so a public client can create more than
`WRASSE_SESSION_LIMIT` sessions, submit one run for each, and keep every one
while the single worker drains the queue. The global run ceiling is a different,
larger cap, not the documented stored-session cap.

At the configured values, 201 rapid session/execute pairs already leave 201
private database pairs at a limit of 200; the same pattern can grow until the
global 400-run ceiling. This is not an eviction fix: it trades deletion of live
files for an unbounded exception to the disk limit.

Reject session creation when all retained sessions are busy, or reserve bounded
capacity before creating/copying the session. The present test covers one busy
survivor followed by an idle one, not the all-busy set.

### MINOR — a broken provider seed makes a healthy deployment fail on its first quote

**OUTSIDE section 3 (deployment readiness). Root: inside this range.**

The entrypoint refuses a missing `buyer-memory.db` source at
`deploy/entrypoint.sh:112-116`, but it never checks the equally required
`provider-memory.db`. `prepare_working_copies()` does reject it later
(`wrasse/service.py:129-156`), when the first WARM store is opened, after
Uvicorn has started and `/api/health` can already return `ok`.

Concrete sequence: seed `/data/source/buyer-memory.db` but omit the provider
file. Railway reports a healthy public deployment; a judge opens it and the
first warm quote/session fails with a server error. Validate both required
source files before starting Uvicorn.

### MINOR — the live check can pass while automatic cleanup is broken

**OUTSIDE section 3 (live-check design). Root: outside this range.**

`scripts/live-demo-check.py` performs two runs and then calls `POST /api/finish`
itself at lines 206-215 before it asserts that collection succeeded. It therefore
checks manual Finish, not the worker-owned automatic final-refund mechanism it
is supposed to protect. Removing `_run_finished()`'s call to `_refund()` would
not make this check fail.

Add a separate five-run check that never calls Finish and waits for the worker
to enqueue/complete the final refund. Keep the manual-Finish exercise as a
separate test.

## Round-two closure table

| Round-two finding | Status | Round-three assessment |
|---|---|---|
| Durable recovery cannot discover open deals after restart | PARTIAL | `liabilities.db` covers ids successfully recorded, but the creation-to-record crash window above still leaves no durable discoverable row. |
| Startup reclaim reports held wallets as free and does not gate settlement | CLOSED | Reclaim checks every returned status and `Queue._closed` refuses settlements after reclaim failure. |
| Finish snapshots recovery before a queued settlement has a deal id | PARTIAL | Refund now reads the index at execution time, but execute can still be admitted before Finish and submitted after its refund. |
| A failed live run has no public recovery action | OPEN | The page still hides Finish on failure and auto-refund still waits for run five. |
| Final refund depends on browser polling | CLOSED | `Queue.after` invokes the completion handler; GET now reports rather than causes that action. |
| Moving Foundry compiler image can reject the deployed contract | CLOSED | Docker pins Foundry and fails the image build if the compiled runtime hash differs from the recorded deployment hash. |
| Unset session-root can chown `/` | CLOSED | `take()` rejects empty/root-like paths and only takes configured directories. |
| Session bound can be defeated and cached stores outlive eviction | PARTIAL | Cache eviction is coupled to file eviction, but all-busy sessions still exceed the stated storage limit. |
| Refund completion is asserted at inclusion rather than safe-head confirmation | OPEN (recorded decision) | `KNOWN-LIMITS.md` now accurately states the inclusion/reorg trade-off; I do not count it again as a defect. |
| Page overstates verification and refund transaction count | CLOSED | Visible copy now distinguishes included credits from wallet payment and describes one withdrawal per credited side plus recovery sends. |
| A stale browser session remains permanently unusable after restart | CLOSED | The panel probes the stored id, clears a 404, and creates a new session. |
| Health can deny keys that the entrypoint materialised | CLOSED | The entrypoint unsets secret material on a non-executing deployment before materialising key files. |

## Unnamed invariants and documentation observations

- A durable liability index needs a *pre-index* durable identity for the create
  action. “Write immediately after the id is known” is not an atomicity
  guarantee when the id becomes knowable only after value has moved.
- Session capacity needs an admission rule, not only a deletion rule. A limit
  with an unbounded busy exception is not a capacity bound.
- The visible page copy is materially improved. Its source comment at
  `web/run-panel.html:294-298` still says the five-way check happened at
  inclusion, but the visible text no longer makes that claim. This is stale
  internal rationale, not a judge-facing finding.
- `git diff --check 6381093..0b90b32` reports trailing whitespace at
  `deploy/entrypoint.sh:25`; it has no behavioural impact.

## Verification and U8 limitations

`UV_CACHE_DIR=/tmp/wrasse-uv-cache uv run pytest -q tests/test_executor.py
tests/test_sessions.py` passed (46 tests). A combined run including
`tests/test_service.py` produced progress but did not complete within the
review wait window, so I do not represent the advertised full suite as
independently verified. I did not run Anvil, send a Base transaction, inspect
Railway's deployed image/volume/secrets, rebuild Docker, or regenerate the
designer bundle. Those omissions mean the live service, actual volume paths,
platform health behaviour, byte-idempotence, and the claimed 505-test total are
not cleared by this review.

## U7/U9 counts

The five Gate 4 deferrals and the three documented design decisions were not
re-reported as defects. Two outstanding roots are outside this range: the
failed-run public recovery path and the live-check's manual-cleanup blind spot.
The execute/Finish race has a pre-existing admission half, but the range's new
locking claim does not close it; that provenance is stated in the finding.

INSIDE section 3's stated concerns: **3 findings**.

OUTSIDE section 3's stated concerns: **4 findings**.

## Ship order

1. Fix the creation-intent/index crash window. It is the only remaining path
   that can make an on-chain deposit undiscoverable after restart.
2. Make settlement admission and Finish mutually ordered, then compensate every
   pre-submit refund failure. Together these prevent a successful later deal or
   a failed final cleanup from being permanently uncollectable.
3. Expose/trigger recovery immediately for any failed post-create run. This is
   already a known public-path hole, not a polish item.
4. Add the two-file startup validation and the all-busy session-cap admission
   check. They protect judge availability rather than settlement arithmetic.
5. Split the live check's automatic-cleanup assertion from its manual-Finish
   exercise. This is valuable regression coverage but should not delay the
   money-path fixes.

VERDICT: do not ship


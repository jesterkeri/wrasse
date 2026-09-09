# Adversarial service review — round four

Range reviewed: `0b90b32..f632d10` at
`f632d10543dd69064334f7d5b294b8c4501f40f1`, branch
`rename-and-bilateral`.

I read `docs/REVIEW-PROTOCOL.md` and applied U1–U9.  My cold-read list,
formed before using the prompt's section 4 or the round-three report as a
checklist, was:

1. the new liability schema discards the deployed schema's rows;
2. the pre-sign record can remain permanently nameless after an ordinary
   command failure, closing the whole queue on its next boot;
3. removing a terminal deal before reconciliation makes a terminal-but-unteached
   run look as though it needs neither recovery nor a refund;
4. refund reconciliation mutates a session outside the lock that is meant to
   serialize those state transitions; and
5. session-capacity admission is checked before an unlocked, slow copy operation,
   so concurrent creation can exceed the stated cap.

The U7 measurement is indicative, not rigorous: as U1 says, the prompt's
questions were visible in the same context as the range.

## Deposit-loss or demo-stopping findings

### CRITICAL — upgrading the live volume silently deletes the only index of old open deals

**INSIDE section 4's durable-index concern. Root: inside this range.**

`wrasse/liabilities.py:68-73` detects the previous `open_deals` schema by the
absence of `intent_id` and executes `drop table open_deals`.  The immediately
preceding implementation stored exactly the information recovery needs:
`deal_id`, `session_id`, `opened_at`, and `detail` (at
`0b90b32:wrasse/liabilities.py:33-40`).  There is no migration, backup, startup
refusal, or chain scan after the drop.

Concrete sequence:

1. The deployment at `0b90b32` records an Offered, Accepted, or Delivered deal
   in `liabilities.db` on the Railway volume.
2. Railway starts `f632d10` against that same volume.  Boot reclaim calls
   `liabilities.rows()` through `Runner._name_unresolved()`
   (`wrasse/executor.py:586`, `729-739`), which opens the database and drops the
   old table.
3. Reclaim now sees an empty index and cannot name or close that deal.  The
   contract continues to hold the price/bond, but the service has removed its
   only durable route to it.

This directly contradicts `KNOWN-LIMITS.md:38-47` and
`docs/DEPLOY.md:53-58`, which present the index as the restart-recovery answer.
It is not a safe "reset": the chain holds the deal, but this code has no
full-chain reconstruction path and deliberately does not parse historical
creation rows from the transaction ledger.

Do not deploy this schema transition over a live liability database.  Either
migrate each old row into the new shape (preserving its `deal_id`), or refuse
startup with an explicit operator migration step.  A destructive migration is
not acceptable for a money-recovery index.  Add a real SQLite migration test
starting from the `0b90b32` schema with an open row; every current executor test
injects `FakeLiabilities` (`tests/test_executor.py:691-739`), so none executes
`liabilities._connect()`.

### MAJOR — a timeout or malformed create response can brick every later public settlement

**INSIDE section 4's reclaim concern. Root: inside this range.**

`Runner._create()` writes a blind row under the service-local random `run_id`
before invoking the CLI (`wrasse/executor.py:976-985`).  It learns and attaches
the real transaction hash only after `_cli()` returns at line 1003.  But `_cli()`
turns a subprocess timeout, non-zero exit, or unreadable JSON into
`ExecutionError` (`wrasse/executor.py:435-449`); those paths do not call
`discard()`.  A process crash in the same interval does the same thing.

On the next boot, `_name_unresolved()` treats a row with neither hash nor deal id
as unknowable and fails reclaim, closing the queue to all settlements
(`wrasse/executor.py:729-739`).  This is a safe refusal about possible value, but
it is not a recoverable public service: one transient RPC/subprocess failure
before the JSON reply can make every later judge see "cannot sign" until an
operator intervenes.  If the CLI actually broadcast before timing out, the
deposit may also be real; if it did not, the service is needlessly bricked.

The stored key is also not the CLI/ledger identity: `run.run_id` is generated in
`wrasse/service.py:622-634`, whereas `_create()` later receives the CLI's
`sent["intent_id"]` at `wrasse/executor.py:1004`.  Thus boot recovery has no
durable linkage with which to distinguish those two cases from the ledger.

Persist the actual creation action identity before signing (or make the CLI
return/persist it before broadcast), and make every pre-send failure
unambiguously discardable.  A recovery row must be able to resolve a possible
broadcast from its own durable action identity, not depend on a response that
may be exactly what was lost.

### MAJOR — a confirmed terminal deal can be left credited but without the automatic refund path

**INSIDE section 4's index/run-procedure concern. Root: inside this range.**

The regression fix removes the liability immediately after safe-head confirmation
at `wrasse/executor.py:516-523`, before `reconcile` teaches the two stores at
line 523.  That ordering prevents a completed deal being mistaken for an open
one, but it makes `liabilities.open_deals()` an incomplete signal for whether a
session has money awaiting collection.

Concrete sequence:

1. A released, timeout, or delayed run reaches a terminal contract state and its
   ending is confirmed at the safe head.
2. Line 522 deletes its liability row.  The subsequent `reconcile --tx` fails
   (for example, Sibyl/RPC is unavailable or its memory write is refused), and
   `execute()` marks the run failed at `wrasse/executor.py:531-535`.
3. For runs one through four, `_run_finished()` sees neither `spent` nor
   `stranded` because the row was already removed (`wrasse/service.py:441-450`),
   so it queues no refund.  `_session_is_busy()` likewise considers the session
   idle (`wrasse/service.py:512-522`), allowing eviction of the session and its
   visitor-facing recovery route.

The contract has assigned the credit, but the visitor is shown a failed run and
the service does not schedule the withdrawal it promises after an interrupted
run.  The fault is especially likely at the stated external-dependency boundary:
the chain fact is already permanent while the memory import is allowed to fail.

Keep terminal-chain/credit cleanup distinct from successful teaching.  For
example, persist a terminal-credit-pending state until the session's refund has
been queued, or have `_run_finished()` queue a refund for a terminal failed run
independently of the open-deal index.  Do not retain the normal liability row
solely to trigger refund; that would reintroduce the just-fixed run-one
regression.

### MAJOR — two simultaneous Finish/retry calls can clear a newer refund's ownership

**INSIDE section 4's session-state concern. Root: partly outside the range; this
fold does not make the claimed serialization hold for reconciliation.**

`Sessions.begin_finishing()` and `abandon_finishing()` take the registry lock
(`wrasse/sessions.py:192-212`), but `_reconcile_refund()` reads and writes the
same `Session` fields outside it (`wrasse/service.py:681-701`).  In particular,
it reads an old failed refund and later calls `abandon_finishing(session)` without
checking that `refund_run_id` still names that old refund.

Concrete sequence:

1. Refund `r1` has failed.  Request A enters `_reconcile_refund`, observes `r1`
   as failed, and is paused just before line 701.
2. Request B reconciles `r1`, abandons it, claims `finishing` again, sets
   `refund_run_id` to a new queued refund `r2` (`wrasse/service.py:713-727`).
3. A resumes and calls `abandon_finishing`, clearing `finishing` and `r2`'s id.

The queued withdrawal is now untracked.  A new settlement can be admitted while
it is in flight, or another Finish can queue a second withdrawal.  If the first
withdrawal completes before that new settlement, the new credit has no automatic
refund scheduled; a judge sees either an inexplicable failed/duplicate Finish or
an escrow balance that did not return automatically.

Reconcile/refund state must use one locked compare-and-transition operation
("clear only if refund_run_id is r1"), and recording `r2` must occur under that
same lock as the claim and submission reservation.  The new test at
`tests/test_service.py:1106-1132` covers a pre-submit exception, not this
interleaving.

## Other findings

### MINOR — the session storage cap is still not an atomic admission bound

**OUTSIDE section 4's stated concerns. Root: inside this range.**

`Sessions.create()` checks whether all retained sessions are busy while holding
the lock (`wrasse/sessions.py:158-167`), releases it to copy both databases
(`169-180`), then reacquires it to add the new session (`182-185`).  Several
concurrent `POST /api/session` requests can all pass the check before any adds
one.  If their visitors immediately queue work, eviction correctly retains the
newly busy sessions and the configured cap is exceeded.

This does not corrupt a deal, but it contradicts the advertised admission-bound
claim and lets a public burst consume more memory/database copies than the
limit.  Reserve a pending slot while holding the lock (and release it if copy
fails), or include in-progress creations in the cap calculation.  The new test
in `tests/test_sessions.py` is sequential and cannot exercise this race.

## Round-three closure table

| Round-three finding | Status | Round-four assessment |
|---|---|---|
| Crash between creation inclusion and recording its deal id | PARTIAL | A pre-sign row and hash-to-id recovery close the original post-inclusion gap, but the new schema destroys rows from the deployed predecessor and blind rows still cannot be resolved. |
| Execute/Finish can pass an admitted settlement | CLOSED | `Sessions.admit()` charges and publishes while holding the same registry lock used by `begin_finishing()` (`wrasse/sessions.py:214-234`). |
| Refund can remain finishing before a job exists | CLOSED | `_refund()` compensates directory/job-submission failures with `abandon_finishing()` (`wrasse/service.py:718-730`). |
| Early failed run has no public recovery route | PARTIAL | An open indexed deal now causes worker-owned refund/recovery and the page exposes it, but a terminal run whose later reconciliation fails has no equivalent route (third finding above). |
| All-busy sessions defeat the stored-session bound | PARTIAL | Sequential all-busy admission is refused, but concurrent creation can pass the pre-copy check together. |
| Missing provider seed is discovered only on first quote | CLOSED | Entrypoint checks both buyer and provider seed files before Uvicorn starts (`deploy/entrypoint.sh:112-123`). |
| Live check can pass while automatic final cleanup is broken | OPEN (accurately documented) | `scripts/live-demo-check.py:206-210` now says it exercises only manual Finish. It still does not run five settlements without Finish, so it cannot detect a broken worker-owned automatic-refund trigger. |

## Unnamed invariants and claim checks

- A liability schema is itself liability-bearing data.  "Reset on schema change"
  is defensible for rebuildable cache data, not for the only list of open deals.
  The code's comment at `wrasse/liabilities.py:62-67` asserts the opposite of
  the actual recovery design.
- “Terminal” and “safe to forget from recovery” are not the same as “the
  visitor's wallet credit has a queued collection.”  The durable state needs to
  represent both facts, or cleanup must derive the latter directly from the
  contract credits.
- The live check honestly names its automatic-cleanup blind spot, so I do not
  treat that documentation as a false claim.  It remains a material test gap,
  especially after a production-only lifecycle regression.
- `git diff --check 0b90b32..f632d10` reports a trailing blank line in
  `docs/service-review-r3.md`; it has no behavioural effect.

## Verification and U8 limitations

`UV_CACHE_DIR=/tmp/wrasse-uv-cache uv run pytest -q tests/test_executor.py
tests/test_sessions.py` passed: 54 tests.  A combined invocation including
`tests/test_service.py` did not finish in the review wait window, so I do not
claim the advertised full suite independently passed.

I did not run Anvil, send Base transactions, inspect Railway's actual mounted
volume or secrets, build Docker, regenerate the page bundle, or run the live
check.  In particular, I could not inspect whether the already-deployed volume
contained an old-schema `liabilities.db`; the migration is nevertheless
deterministically destructive if it did.  The test suite uses injected command,
chain, and liability collaborators, so it cannot by itself cover process death
between CLI signing/broadcast/response, an upgrade from the old SQLite schema,
or a real `reconcile` failure after safe-head confirmation.

## U7/U9 counts and priority

The recorded decisions about one-process agents, shared-credit refunds, and
refund confirmation depth are not re-reported as defects.  There are no new
findings rooted solely outside the range except the refund-state race's older
unlocked design half; its current failure to close remains relevant to this
fold's serialization claim.

INSIDE section 4's stated concerns: **4 findings**.

OUTSIDE section 4's stated concerns: **1 finding**.

The deposit-loss/demo-stopping list is **not empty**.  The other-findings list
contains one availability/capacity item.

Fix order for the remaining hours:

1. Replace the destructive liability migration before another deployment touches
   the live volume; inspect/back up that volume first.
2. Give every pre-sign creation row a recoverable ledger identity, including the
   timeout/no-response path.
3. Separate terminal credit/refund scheduling from memory reconciliation, then
   make refund reconciliation an atomic compare-and-transition.
4. Reserve concurrent session capacity and add a true five-run no-Finish live
   check when the money paths are safe.

VERDICT: do not ship

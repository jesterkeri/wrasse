# Adversarial service review — round two

Range reviewed: `ff00689..6381093` at
`6381093bc7499a9c427b27ea14e68c387f3bfc25`, branch `rename-and-bilateral`.

I read `docs/REVIEW-PROTOCOL.md` first and applied U1–U9. My cold-read list, before using
the round-one report as a closure checklist, was:

1. restart recovery has no durable inventory of deals to close;
2. startup reclaim treats an unresolved ledger as successfully reclaimed;
3. Finish snapshots recovery state before queued work has necessarily created its deal;
4. the `finishing` transition is not atomic with starting settlement or refund work;
5. automatic final refund is triggered by a GET poll rather than job completion;
6. busy-session retention defeats the stated storage bound and leaks cached store objects;
7. the root entrypoint can change ownership of `/` when a setting is omitted;
8. the page still describes a potentially multi-transaction refund as one transaction.

The U7 measurement is indicative, not rigorous, as U1 itself requires me to say: the whole
prompt, including section 3, was already in one context window.

## New findings

### MAJOR — restart recovery cannot discover the deals whose deposits it claims to recover

**INSIDE section 3 (reclaim/recovery). Root: inside this range.**

The only recovery inventory is assembled at `wrasse/service.py:657-665` from `deal_id` fields
on the current process's in-memory `Run` objects. Those objects and the session registry are
discarded on restart (`wrasse/service.py:394-395`). The startup reclaim at
`wrasse/executor.py:516-547` only runs `tx-resolve`; it neither reconstructs a deal id from a
confirmed `createDeal` receipt nor scans open deals. There is a same-process hole too:
`wrasse/executor.py:868-886` records the id only after creation is included and a separate
receipt parse succeeds.

Concrete sequences:

- `createDeal` is included, then Railway restarts. The ledger can resolve the create
  transaction, but the new process has no `Run`, session or deal id to put in `recover`; the
  buyer's price remains in the live deal indefinitely.
- Creation is included and `_deal_id(sent["tx_hash"])` fails. The failed run still has
  `deal_id=None`, so even a Finish request in the same process omits it.

This makes the statement in `KNOWN-LIMITS.md:61-67` — “a restart ... recovers only the money”
— false. Losing the old run link may be an accepted limitation; losing the only index that can
find its escrow liability is not. Persist the deal/session association before proceeding, or
reconstruct it from durable ledger receipts at startup and close every nonterminal deal before
serving.

### MAJOR — the startup reclaim is not a gate and reports unresolved wallets as reclaimed

**INSIDE section 3 (reclaim). Root: inside this range.**

`wrasse/executor.py:535-547` declares reclaim successful whenever `tx-resolve` exits zero. It
does not inspect the returned statuses. Worse, it counts every row whose `action` is not
`none` as “resolved”. For an absent transaction, `wrasse/cli.py:1718-1727` returns the action
“pass --rebroadcast to resend the identical bytes” while leaving the row and nonce held,
because reclaim did not pass `--rebroadcast`. A `stuck` or `nonce_conflict_pending` row is also
not rejected. `Queue._work` at `wrasse/executor.py:1045-1059` proceeds to the next settlement
regardless of whether the reclaim run failed.

Concrete sequence: the previous process dies after broadcasting an action whose bytes then
disappear from both RPCs. On boot, reclaim receives `unknown` plus “pass --rebroadcast”, calls
it successfully reclaimed, and the next visitor's run reaches the ledger with that wallet
still blocked. If the RPC is unavailable, reclaim is marked failed but the next queued run is
still executed. The claim in `KNOWN-LIMITS.md:63-64` that the worker frees both wallets before
settling is therefore unsupported.

Reclaim must fail closed unless every blocking row is terminal or included, and queue startup
must remain closed when reclaim fails. If rebroadcast is the selected policy, it must use the
existing deliberate rebroadcast path rather than interpreting an instruction to rebroadcast as
success.

### MAJOR — Finish can run before the settlement whose deposit it is supposed to recover

**INSIDE section 3 (new session state and recovery transactions). Root: inside this range.**

`wrasse/service.py:559-578` spends the session, then constructs and submits its settlement
outside the session lock. `_refund` independently snapshots the currently known deal ids at
`wrasse/service.py:646-673`. Neither transition shares a lock. The same defect occurs without a
thread race when Finish is pressed while an existing settlement is merely queued: its
`deal_id` is still `None`, so it is omitted from the refund's immutable `recover` list.

Concrete interleaving:

1. `/api/execute` increments `session.runs` and pauses before queue submission.
2. `/api/finish` sees no run/deal, sets `finishing`, and queues an empty refund.
3. execute queues the settlement behind it.
4. the refund succeeds with nothing to collect; the settlement then creates a deal and credits
   value after the session is already marked refunded.

A more ordinary variant is Finish while a run is queued, followed by that run failing after
creation: the refund runs afterward but its earlier snapshot contains no deal id, so the open
deal remains. Make “accept new settlement / begin finishing / capture liabilities” one atomic
session transition, and derive the recovery list when the refund actually executes from durable
state rather than when it is queued.

### MAJOR — a failed live run does not expose the recovery action that was added for it

**INSIDE section 3 (recovery path, including whether a visitor can invoke it). Root: inside
this range.**

The new recovery procedure only runs from a refund. The Finish control starts hidden at
`web/run-panel.html:124-125` and is made visible only in the `run.status === 'succeeded'`
branch at `web/run-panel.html:489-501`. The failed branch at lines 506-507 merely prints the
error. Automatic refund likewise requires a successful settlement at
`wrasse/service.py:695-702`.

Concrete sequence: creation succeeds, acceptance or delivery fails, and the worker correctly
records the run as failed. This is exactly the case `_recover_deals` was introduced to repair,
but a first-time visitor gets no Finish button and no automatic refund, so they cannot invoke
it from the public page. The escrow remains open until an operator uses an API or CLI the page
does not expose. Show/trigger recovery whenever a failed run has created a deal, and keep its
progress distinct from a normal successful-session refund.

### MAJOR — the promised last-run refund depends on the visitor continuing to poll

**OUTSIDE section 3's explicit list (worker/API ownership). Root: the trigger predates this
range; this range changes its state handling but leaves the root architecture in place.**

The worker does not enqueue a refund when the fifth run completes. That side effect lives in
the GET handler `run_status` at `wrasse/service.py:686-702`. The page polls every two seconds,
but a browser is not a durable job worker.

Concrete sequence: a visitor starts run five and closes the tab, loses connectivity, or stops
polling after seeing its terminal transaction. The worker completes and teaches both memories,
but nobody calls the GET branch after success, so no refund is queued and the escrow credits
remain. This contradicts README's claim that the escrow is emptied at the end of a session and
the live check cannot catch it because the check itself polls. Move the transition into worker
completion (or a durable queue callback); GET must observe state, not cause the promised cleanup.

### MAJOR — a mutable compiler image can make a green source revision refuse every live run

**OUTSIDE section 3 (deployment reproducibility). Root: outside this range; `stable` was already
present at `ff00689`, but this range is the first claimed live build.**

Committed HEAD uses `ghcr.io/foundry-rs/foundry:stable` at `Dockerfile:9` and copies whatever
artifact that moving tag produces at lines 43-44. The signing path compares deployed bytecode
against that artifact, but the image build does not compare it with the recorded deployment.

Concrete result: a later Railway rebuild resolves `stable` to a Foundry version whose metadata
or compiler invocation produces different runtime bytes. `/api/health`, quotes, simulation and
the page all work; every real execution then fails at deployment-identity validation before its
first send. Pin the exact Foundry image that reproduced the deployed artifact and make the image
build compare `artifact_runtime_hash()` with `deployments/base-sepolia.json` so the failure is a
red build rather than a judge-facing run.

### MAJOR — omitting one optional-looking setting makes the root entrypoint `chown /`

**INSIDE section 3 (entrypoint privilege). Root: inside this range.**

`deploy/entrypoint.sh:18-26` appends `/x` to `${WRASSE_SESSION_ROOT:-}` before deriving its
directory. If the variable is unset, the resulting path is `/x`, `dirname` is `/`, and the
root startup path at lines 29-31 executes `chown wrasse:wrasse /`. The Python service itself
has a valid relative default for this variable (`wrasse/sessions.py:31-37`), so the shell has
silently changed an omitted setting from “use the default” to “take ownership of the filesystem
root”.

The current Railway configuration may set `/data/sessions`, but rebuilding the image without
that variable grants the service user ownership of `/` before privilege drop. Use an explicit,
validated default directory and refuse paths whose resolved parent is `/`; never synthesize a
sentinel below an unresolved variable.

### MINOR — the session limit no longer limits retained sessions or open store objects

**INSIDE section 3 (new session flags). Root: inside this range.**

`wrasse/service.py:451-461` calls every session with one attempted run “busy” until it is
refunded, even when the run refused before sending or failed before creation. `_evict` then
retains busy entries at `wrasse/sessions.py:208-221`, so if more than the configured limit are
busy it returns with `_order` and `_sessions` still over the limit. Separately,
`_session_stores` at `wrasse/service.py:399-403` is never pruned when an idle session is evicted,
so quoted sessions leave store/client objects reachable after their files are deleted.

Concrete sequence: create 201 sessions at a limit of 200, make one refused attempt in each, and
never press Finish. All 201 are retained despite the documented bound; repeated create/quote/
evict cycles also grow `_session_stores` without bound. The global run ceiling eventually caps
the first route, but the quote/cache route does not. Track actual liabilities rather than
`runs > 0`, and couple registry eviction to closing/removing cached store objects. If busy
sessions are intentionally allowed above the disk limit, document a separate hard capacity.

### MINOR — a refund is declared complete at inclusion, not at the safe head

**OUTSIDE section 3's explicit list (confirmation semantics). Root: inside this range.**

Each withdrawal waits only for `_INCLUDED` at `wrasse/executor.py:588-603`; the refund run then
becomes `SUCCEEDED`. `_reconcile_refund` turns that directly into `session.refunded=True` at
`wrasse/service.py:619-626`, after which no retry is possible. The rest of this build carefully
distinguishes inclusion from confirmation before teaching memory.

Concrete sequence: a withdrawal is included, the API marks the session refunded and the page
says nothing remains, then that block is reorged before the safe head. The escrow credit is back
but this session will return `already_refunded`. Later global collection may rescue the shared
wallet funds, but the session's completion claim is false. Either confirm withdrawals before
marking them complete or label the state “included” and continue reconciling it.

### MINOR — the page still claims verification and refund transaction counts the code has not met

**INSIDE section 3 (page claims and transaction counts). Root: inside this range.**

The settlement count itself is now correct: `web/run-panel.html:475-480` says three sends for a
timeout and four for the other endings. Two adjacent claims remain wrong:

- the visible confirmation progress text at `web/run-panel.html:171-175` says the agent has
  already checked the receipt, sender, logs and contract state, but the five-way reconciliation
  does not run until `wrasse/executor.py:919-923`, after confirmation;
- `web/run-panel.html:295-301` says credits are collected “in one transaction when you finish”,
  while `_run_refund` sends one withdrawal for each credited role at
  `wrasse/executor.py:579-603` and may first send one recovery transaction per open deal.

A mixed released/timeout session can therefore require two withdrawals plus recoveries, while
the page promises one. Use “one collection step” if that is the intended UX abstraction, and
describe the inclusion-time check only as the transaction resolver's observation; reserve the
five-way evidence claim for the completed reconciliation step.

### MINOR — after a restart the page cannot perform the documented “start again” recovery

**OUTSIDE section 3 (browser/session boundary). Root: outside this range.**

`web/run-panel.html:255-263` returns any id found in `sessionStorage` without validating it or
clearing it on a 404. The server explicitly loses its registry on restart and returns 404 for
old ids (`wrasse/service.py:492-501`).

Concrete sequence: Railway restarts while a judge keeps the tab or revisits it later. Every Run
press reuses the dead id and gets “no such session”; the catch displays the error but leaves the
same id in storage, so pressing again can never create the new session that
`KNOWN-LIMITS.md:65-67` says the visitor starts. On a session 404, clear the stored id, create a
new session once, and clearly tell the visitor that the old private history is unavailable.

### MINOR — health can report that it holds no keys while the entrypoint has materialised them

**OUTSIDE section 3 (configuration/health truthfulness). Root: outside this range.**

The entrypoint writes both keystore JSON secrets whenever their variables are present at
`deploy/entrypoint.sh:47-56`, regardless of `WRASSE_ENABLE_EXECUTION`. Health derives
`holds_keys` solely from that execution flag at `wrasse/service.py:230-244`.

Concrete sequence: clone the executing Railway service into a read-only deployment, retain its
secret variables, and omit `WRASSE_ENABLE_EXECUTION`. Nothing signs, which is good, but health
reports `holds_keys: false` while both key files exist in `/run/wrasse`. Materialise keys only
inside the execution-enabled branch, or calculate the health claim from what was actually
provided.

## Round-one closure table

| Round-one finding | Status | Round-two assessment |
|---|---|---|
| Session could copy missing warm memories | CLOSED | `/api/session` opens WARM first and `copy_database` now refuses a missing source. |
| Failure after `createDeal` had no recovery | PARTIAL | Recovery handles known in-process deal ids, but misses restart, receipt-parse failure, early Finish, and is not reachable from the page after a failed first run. |
| Restart loses the procedure and money | PARTIAL | Loss of the link/session is now an accepted limit, but the stated money recovery has no durable deal inventory and reclaim does not prove wallets free. |
| Refund flag was premature and failures unretryable | PARTIAL | `finishing` and sequential retry close the cited straight-line case; execute/finish is not atomic and completion is still asserted at inclusion. |
| One-sided agents are absent from the live path | OPEN | Deliberate open decision; untouched here and not counted again as a new finding. |
| Eviction deleted active run files | PARTIAL | The described active run is retained, but `runs > 0` can defeat the bound and cached session stores are not evicted with their files. |
| Refund collects global rather than session credit | OPEN | Deliberately recorded in `KNOWN-LIMITS.md`; not counted as a new finding. |
| Live result exposed raw wei | CLOSED | Price is converted exactly to ETH and stake/durations use human units. |
| Timeout was called a four-transaction ending | CLOSED | The page now selects three for timeout and four for released/delayed. |
| Included outcome was called wallet-paid and fully checked | PARTIAL | “Paid” became the accurate “assigned/credited”, but the confirmation progress still claims the later evidence checks, and the refund is still described as one transaction. |

## U2/U3 completeness and unnamed invariants

The changed universal claims were swept across their actual sets:

- the three nonterminal contract states all have a recovery action, but the set of *deal ids to
  which those actions must apply* is not durably enumerable;
- the three endings now have the correct settlement send counts (4 released, 3 timeout,
  4 delayed), but Finish can add 0–5 recovery sends and 0–2 withdrawals;
- the two refund flags have more than two meaningful transitions once execute/finish races,
  restart and reorg are included; booleans without an atomic owner do not form a state machine;
- “every unresolved row is reclaimed” requires inspecting every returned status, not merely a
  successful `tx-resolve` process exit.

Two invariants absent from the prompt are load-bearing: cleanup must be caused by durable worker
state rather than by a browser GET, and any cache owning resources for a session must share the
registry's eviction lifecycle.

## Verification and U8 limitations

`tests/test_executor.py` and `tests/test_sessions.py` pass: 42 tests. Ruff passes, and
`git diff --check ff00689..6381093` passes. The full non-Anvil suite reached the service tests
and then stalled in the local Starlette/AnyIO `TestClient` harness, as it did in round one; I
stopped it rather than presenting a partial run as green. The existing service tests cover
sequential refund retry and the presence/order of a reclaim job, but not the interleavings or
unresolved-status cases above.

I did not send a Base transaction, run Anvil, inspect the Railway configuration or logs, or
rebuild the Docker image. I therefore could not independently verify the live service URL,
mounted-volume ownership, secret values, current wallet/escrow balances, or the platform image
digest. I did not regenerate `web/index.html` because the working tree acquired unrelated edits
during this review; byte-idempotence remains unverified here. The checked-in `index.html` does
contain the changed panel, but that is not proof a fresh designer export reproduces it.

The repository advanced from requested HEAD `6381093` to `e0e000d` while this review was in
progress. I did not fold those later commits into the result: diff inspection and the deployment
citations above are anchored to `git show 6381093` and the requested range. The only file I wrote
is this review.

## U9 scope notes and counts

The mutable Foundry image, GET-owned automatic refund, stale browser session id, and health/key
claim have roots outside this four-commit range; they are labelled above rather than suppressed.
All other findings root in the fold. The five Gate 4 deferrals and the accepted global-credit
accounting decision were not re-reported. I challenge the newly added restart entry in
`KNOWN-LIMITS.md`, because its “recovers only the money” rationale is contradicted by the
implementation and is therefore not an accepted limit as written.

INSIDE section 3's stated concerns: **7 findings**.

OUTSIDE section 3's stated concerns: **5 findings**.

Before judging, the minimum ship set is durable deal discovery, fail-closed startup reclaim,
atomic execute/finish/refund transitions, worker-owned final cleanup, and a pinned/verified
contract build. The root-entrypoint path must also be made safe before treating the image as a
reusable deployment artifact. The remaining MINORs can be fixed or stated accurately, but none
should be used to soften the money-recovery blockers.

VERDICT: do not ship

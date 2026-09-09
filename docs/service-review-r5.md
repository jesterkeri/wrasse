# Adversarial service review — round five

Range reviewed: `f632d10..e8f4c45` at
`e8f4c455f5b128fbf48a459fe89c23b87a180086`, branch
`rename-and-bilateral`.

I read `docs/REVIEW-PROTOCOL.md` and applied U1–U9.  My cold-read list before
using section 4 or the round-four report as a checklist was:

1. the new migration performs durable DDL in several independently committed
   statements, so interruption can make the old rows invisible;
2. the new ledger lookup treats a transaction hash as proof that a creation may
   have made a deal, although even an explicitly unbroadcast/reverted ledger row
   has a signed transaction hash; and
3. a failed session-copy releases its slot but leaves the partly copied directory
   on disk.

The U7 measurement is indicative, not rigorous: the questions in section 4
were visible in the same prompt, as U1 explains.

## Would lose a visitor's deposit, or stop the demo working for a judge

This list is **not empty**.

### CRITICAL — an interrupted migration makes old open-deal rows invisible to recovery

**INSIDE section 4's durable-index concern. Root: inside this range.**

`wrasse/liabilities.py:71-80` migrates an old table through four independent
DDL/DML statements: rename `open_deals`, create a new table, copy rows, then
drop the old table.  `_connect()` does not start a transaction before line 72.
With Python's sqlite default transaction mode, the `ALTER TABLE` and `CREATE
TABLE` are committed independently of the later insert; closing the connection
after either does not roll them back.

Concrete sequence:

1. The old on-volume schema holds row `deal_id=41` for an Offered deal.
2. A process is killed after lines 72-73, after it has renamed the old table to
   `open_deals_v1` and created a new, empty `open_deals`, but before lines 74-80
   copy the row.
3. On its next start, `pragma table_info(open_deals)` sees the new schema with
   `intent_id`, skips migration, and `rows()` reads the empty new table
   (`wrasse/liabilities.py:68-81`, `161-179`).  The old row remains in
   `open_deals_v1`, but reclaim never reads it.

I reproduced this exact intermediate database against the reviewed code: its
recovery call returned `[]` while `select deal_id from open_deals_v1` returned
`[(41,)]`.  The escrow can still hold the corresponding price/bond, while the
service has no index entry to close it.  This is the same deposit-loss outcome
as the original destructive migration, reached through an interruption rather
than a completed upgrade.

Use one explicit `BEGIN IMMEDIATE` transaction covering schema detection,
rename, create, copy, validation, and drop; rollback must leave the old
`open_deals` intact.  Alternatively retain and recognise `open_deals_v1` on
every startup until a completed, versioned migration has been committed.  Add a
fault-injection test at each statement boundary.  The new tests verify only the
uninterrupted happy migration (`tests/test_liabilities.py:47-90`), so all pass
despite this failure.

### MAJOR — an explicitly unbroadcast creation becomes a poison liability after the next restart

**INSIDE section 4's ledger-key/reclaim concern. Root: inside this range.**

The new blind-row repair checks whether `tx-status` returns a non-empty
`tx_hash` (`wrasse/executor.py:742-760`).  A ledger row receives that hash when
it is signed, before broadcast.  Therefore `unbroadcast` deliberately retains
the hash: `TransactionLedger.mark_unbroadcast()` changes only its status
(`wrasse/chain.py:615-629`), and `LedgerRow` always retains `tx_hash`
(`wrasse/chain.py:718-740`).  The same shape applies to a creation transaction
that was included/confirmed reverted: it has signed bytes but created no deal.

Concrete sequence:

1. `create-deal` signs, records its ledger row, and the node rejects it before
   mempool admission.  The CLI marks it `unbroadcast` and exits non-zero
   (`wrasse/cli.py:1333-1372`), so executor `_cli()` raises before it receives
   the JSON reply.  The pre-sign liability row remains blank.
2. At boot, `tx-resolve` correctly treats `unbroadcast` as freeing the wallet
   (`wrasse/executor.py:148-152`).  `_name_unresolved()` then asks `tx-status`,
   sees the row's non-empty signed hash, attaches it to the liability, and calls
   `_deal_id()` (`wrasse/executor.py:748-760`).  There is no receipt/deal, so it
   catches the error and leaves the liability row behind.
3. On the following restart that row is now in `unresolved()` because it has a
   hash.  The earlier loop at `wrasse/executor.py:717-725` calls `_deal_id()`
   without this catch, fails reclaim, and closes the settlement queue.  Every
   judge thereafter sees a service that cannot sign until an operator repairs a
   row for a deal that never existed.

The test called `test_a_creation_refused_before_broadcast...` still supplies a
zero-exit synthetic `unbroadcast` response rather than the real CLI's non-zero
path (`tests/test_executor.py:1166-1189`), and the new ledger test uses only an
`included_success` row (`1108-1142`).  Neither exercises this sequence.

Branch on the ledger status, not hash presence.  Discard the liability for
`unbroadcast`, `included_reverted`, and `confirmed_reverted`; keep a
non-terminal row for resolution and only ask for a deal id after a successful
creation receipt is known.  Add a two-boot test for each terminal no-deal
status, especially the real deterministic-rejection/non-zero-exit path.

## Everything else

### MINOR — a failed session copy leaks an unowned directory and any first-store copy

**OUTSIDE section 4's stated concerns. Root: inside this range.**

`Sessions.create()` increments `_pending`, copies each memory database outside
the lock, and on error merely decrements `_pending`
(`wrasse/sessions.py:166-189`).  It does not remove `directory`.  If buyer copy
succeeds and provider copy then raises (or a copy creates a partial target before
raising), the service has released the admission slot but leaves an unregistered
directory and database files under the persistent session root.

A repeated I/O/source failure can therefore consume the same disk capacity the
reservation was intended to protect.  It does not lose an on-chain deposit — no
session was issued and no run can start — but it can turn a recoverable transient
seed/volume problem into later inability to create sessions.  Remove the
newly-created directory in the exception path, without touching a directory that
was not created by this invocation.  The reservation test at
`tests/test_sessions.py:321-352` checks `_pending == 0`, not filesystem cleanup.

This belongs in `KNOWN-LIMITS.md` only if the two blockers above are fixed; it
does not justify delaying the demo on its own.

## Round-four closure table

| Round-four finding | Status | Round-five assessment |
|---|---|---|
| Destructive old-schema migration drops recovery rows | PARTIAL | The normal migration copies the old deal ids, but an interruption between rename/create/copy makes those rows invisible, as described above. |
| Blind liability row closes every future settlement after a lost reply | PARTIAL | The policy intent now matches the ledger intent and an absent ledger row is safely discarded, but `unbroadcast`/reverted ledger rows retain a hash and poison the next restart. |
| Confirmed terminal deal whose later memory import fails gets no refund | CLOSED | `_run_finished()` now queues collection for `FAILED` runs that carry a deal id (`wrasse/service.py:445-459`) while preserving the successful-run-one guard. |
| Stale refund reconciliation can clear a newer refund | CLOSED | `Sessions.settle_refund()` compares the observed run id under the registry lock before changing either state field (`wrasse/sessions.py:223-248`). |
| Concurrent session creation bypasses capacity | CLOSED | `_pending` is reserved before copying and released on success/failure (`wrasse/sessions.py:166-198`), so concurrent creates are counted without holding the registry lock over I/O. |

## Claim and invariant checks

- The live-volume P0 result is an external operational claim.  I did not have
  Railway-volume or Base-wallet access to repeat the stated 17-deal audit, so I
  take that result on trust.  It does not make the migration interruption safe
  for the next old-schema volume.
- A migration for the only durable open-deal index needs an atomicity invariant,
  not merely a row-preservation invariant.  “The completed migration has the
  same rows” does not establish what a restart sees at every crash boundary.
- “A ledger row has a transaction hash” means only that bytes were signed.  It
  is not evidence that a contract call was included or that it created a deal.
  The new repair accidentally collapses those two facts.

## Verification and U8 limitations

`UV_CACHE_DIR=/tmp/wrasse-uv-cache uv run pytest -q tests/test_liabilities.py
tests/test_executor.py tests/test_sessions.py` passed: 64 tests.  I did not
independently complete `tests/test_service.py` or the advertised full suite,
run Anvil, build/deploy Docker, inspect Railway's live volume/logs/secrets, or
send transactions to Base.  I also did not execute the live check.  Those are
not cleared by this review.

I did perform a local SQLite interruption experiment in `/tmp`, using the old
schema and the reviewed `_SCHEMA`; it demonstrated that recovery sees an empty
new table while the pre-migration deal remains in `open_deals_v1`.

## U7 and decision

INSIDE section 4's stated concerns: **2 findings**.

OUTSIDE section 4's stated concerns: **1 finding**.

Fix the CRITICAL migration atomicity first: it can hide a live deposit.  Then
fix the MAJOR terminal-no-deal branch: it can make the public demo fail after an
ordinary rejected creation and restart.  The MINOR cleanup can be recorded and
shipped if time runs out.

VERDICT: do not ship

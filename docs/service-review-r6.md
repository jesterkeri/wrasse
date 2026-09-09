# Adversarial service review — round six

Range reviewed: `e8f4c45..31e2d68` at `31e2d683979436459d3314d2f733c661bcf13236`, branch `rename-and-bilateral`.

I read `docs/REVIEW-PROTOCOL.md` and applied U1–U9. My cold-read finding, formed before using the prompt’s section 4 as a checklist, was that schema detection happens before the migration acquires SQLite’s write lock. A second opener can therefore execute a migration decision that was true for an old schema but false by the time it owns the database.

The U7 measurement is **indicative, not rigorous**: section 4 was visible in the same prompt, as U1 itself explains.

## Would lose a visitor's deposit, or stop the demo working for a judge

This list is **not empty**.

### CRITICAL — a concurrent opener can silently delete a newly created, unnamed liability

**INSIDE section 4’s migration concern. Root: inside this range.**

`wrasse/liabilities.py:78-87` reads the tables and computes `older` before `begin immediate`. The lock serialises DDL only after that decision has already been made. If two processes open a v1 index together, both can retain `older=True`; after the first completes the migration, the second can acquire the lock and rename the already-current `open_deals` table at line 90.

The copy/validation then loses a current row whose `deal_id` is still `NULL`:

- `insert or ignore` at lines 92-97 computes `intent_id` as `'migrated:' || deal_id`; for `NULL`, that is `NULL`, which SQLite ignores because `intent_id` is the primary key.
- The surviving-row count at lines 98-101 does not catch it. SQL evaluates `NULL NOT IN (...)` as unknown, not true, so the row is absent from the count; line 107 drops the renamed table anyway.

Concrete sequence:

1. Two service processes start against the same v1 volume. Process B executes lines 78-84 and is descheduled with `older=True`, before line 87.
2. Process A acquires the lock, migrates, and starts serving. It writes a current liability for a just-signed `createDeal`; that durable row has an `intent_id`/transaction hash but no deal id because the receipt lookup has not returned yet (`wrasse/executor.py:1020-1045`).
3. Process B resumes, gets `BEGIN IMMEDIATE`, renames A’s current table as if it were v1, ignores A’s `deal_id=NULL` row during the copy, passes the incomplete count, and drops `open_deals_v1`.
4. If that create transaction is or becomes successful, its buyer deposit is on the escrow but no durable row remains for boot recovery to resolve. A later refund cannot close or collect it.

I reproduced the essential SQLite interleaving locally: a stale `older=True` decision after another connection completed the migration re-ran the rename and copy. Adding a current null-deal row causes `insert or ignore` to skip it and the `NOT IN` count to return zero before the old table is dropped. The reviewed tests cover a one-process interrupted old-table state and a forced failed copy (`tests/test_liabilities.py:122-184`); neither opens two connections across the decision/lock boundary or includes a null `deal_id` in the renamed source.

Acquire `BEGIN IMMEDIATE` *before* inspecting either table or columns, and derive `stale`/`older` inside that transaction. Do not use `INSERT OR IGNORE` plus a deal-id-only count as the proof of preservation: require an exact, non-null source-to-destination mapping (or refuse) for every source row. Add a two-connection test that pauses one migration after preflight, lets the other migrate and write an unnamed v2 liability, then resumes the first.

## Everything else

### MINOR — failed-copy cleanup still hides a cleanup failure

**OUTSIDE section 4’s stated concerns. Root: inside this range.**

`wrasse/sessions.py:186-193` now attempts to remove a partly copied session, but `shutil.rmtree(directory, ignore_errors=True)` at line 190 suppresses an I/O or permissions failure. The reservation is returned even when the unregistered directory remains. Repeated source-copy failures combined with an undeletable partial directory can still consume the persistent session volume until visitors cannot create sessions.

No on-chain value can have moved in this path, because the `Session` was never registered or issued. It is appropriate for `KNOWN-LIMITS.md` if time is gone, but the implementation should at least log a cleanup failure rather than claim the directory was released. The new test checks normal cleanup, not an `rmtree` failure (`tests/test_sessions.py:330-361`).

## Round-five blocker closure table

| Round-five blocker | Status | Round-six assessment |
|---|---|---|
| An interrupted old-schema migration makes an open deal invisible | PARTIAL | One-process statement-boundary interruption is now covered by a real SQLite transaction and `open_deals_v1` recovery. The concurrent stale-preflight interleaving above can still delete an unnamed current liability. |
| An unbroadcast/reverted signed hash poisons reclaim | CLOSED | `_name_unresolved()` branches on ledger status rather than hash presence (`wrasse/executor.py:733-760`). `unbroadcast`, included/confirmed reverts, and nonce-consumed/replaced are discarded; only included/confirmed success is resolved to a deal. |

## Status sweep and invariant checks

The status set in `wrasse/chain.py:43-79` is fully covered by the new recovery branches:

| Ledger status | Recovery action |
|---|---|
| `signed`, `send_attempted`, `pending`, `reorged`, `nonce_conflict_pending`, `stuck` | retain the liability for a later boot; no deal is assumed |
| `included_success`, `confirmed_success` | retain, attach the hash, then resolve its deal id |
| `unbroadcast`, `included_reverted`, `confirmed_reverted`, `nonce_consumed_or_replaced` | discard the unnamed liability |

Under the project’s stated two-RPC/non-Byzantine-RPC model, the final group is appropriate: `nonce_consumed_or_replaced` is reached only after neither RPC has the receipt and both safe nonces are past the slot (`wrasse/chain.py:1087-1164`). An unrecognised status is retained by the `status not in _MADE_DEAL_STATUSES` branch rather than discarded.

The unnamed invariant missing from the migration design is: **the schema shape used to decide a migration must be read under the same exclusive transaction as the migration itself.** Atomic DDL is insufficient if the branch into that DDL was chosen from an obsolete schema.

## Verification and U8 limitations

`UV_CACHE_DIR=/tmp/wrasse-uv-cache uv run pytest -q tests/test_liabilities.py tests/test_executor.py tests/test_sessions.py` passed: **74 tests**.

I did not run the advertised full suite, Anvil/Forge, Docker/Railway deployment, the production check, or live Base transactions. I could not inspect Railway’s volume, process count, logs, or secrets, so the claimed live audit is not independently verified. I also did not run a true multi-process migration race; the local SQLite reproduction executed the stale preflight decision and later write transaction deterministically in two connections. Those areas are not cleared by this review.

## U7 and decision

INSIDE section 4’s stated concerns: **1 finding**.

OUTSIDE section 4’s stated concerns: **1 finding**.

Fix the CRITICAL migration race tonight. It can erase the only recovery route to a live deposit and therefore belongs in the first list regardless of the deadline. The MINOR cleanup issue can be recorded and shipped.

VERDICT: do not ship

---
owner: joshua
last_verified: 2026-09-10
verified_by: every item below re-checked against the running build on 2026-09-10; the two blocking items were closed and are recorded as closed rather than deleted
hop_ids: [J2, J3, J4]
---

# 8. Known unknowns and tolerated breakage

The section that makes the rest trustworthy. Read this one before shipping and before judging.

## Open and blocking

**Nothing, as of 2026-09-10.** Both items this section carried on 6 September have been closed,
and they are recorded here rather than deleted, because a section that empties itself silently
is one nobody can audit.

**Closed: the resend gate ignored the no-deadline sentinel.** Deal actions are signed with
`accept_by = 0`, and the rebroadcast gate compared `row.accept_by <= send_time` with no
exemption, so `tx-resolve --rebroadcast` put a recoverable row straight back to `stuck`. The
only in-project recovery path for a lost deal-action receipt did not work. Fixed at `2c740f8`,
whose message is *the second gate, which my own commit message said was already fixed*.
`wrasse/cli.py` now carries `chain.NO_DEADLINE < row.accept_by <= send_time`, mirroring the
verdict gate.

**Closed: the memory-off frozen payload did not exist.** `docs/examples/` now holds
`policy.cold.json` beside `policy.schema3.json`, so the page's central comparison is real on
both halves. Landed at `b559db6`.

**Where the current list lives.** `KNOWN-LIMITS.md`, seven sections, maintained as the build
moves. This page keeps the reasoning; that file keeps the register.

## Tolerated, with reasons

**Chain time is read once per resolver run, not per row.** A run over many rows uses an
observation that ages. The last-moment recheck before a resend is in place, which is the case
that can lose money. Per-row staleness affects reporting only.

**The ledger schema has no migration.** `CREATE TABLE IF NOT EXISTS` with no version stamp. The
recorded reason was "nothing has been deployed", which `deployments/base-sepolia.json` now
contradicts. Failure mode on an older file is a fail-closed `IntegrityError`, not money loss. A
`PRAGMA user_version` stamp and a refusal on mismatch is about ten lines.

**Risk is a clamped sum with no stored state.** A counterparty two receipts deep and one ten
deep both display 1.0000. A reader cannot tell how far from recovery either is.

**All three buyer risk weights clamp to 1.0 on the live data**, so `risk_weight` is currently
invisible and the profiles differ only by their sensitivity constants.

**The three-quarters concession share is a stated policy, not a derivation.** It places the
refusal boundary between profiles.

**A hand-maintained membership list cannot prove completeness.** Four constants were found
outside the version digest across the review rounds, each by a different question. The
structural answer is a call-graph inventory with every leaf classified and mutation-tested.
Recorded, not built. **Not a four-day problem.**

## Untested, and known to be

- **A reorg after `confirmed_success`.** The reconciler refuses a re-included receipt and tells
  you to run `tx-resolve`, but `tx-resolve` skips terminal rows, so that instruction cannot be
  followed. Unlikely on Base Sepolia, never exercised.
- **Two concurrent `wrasse` processes against real `flock` files.** The locking is written for
  it and has not been run under multi-process contention.
- **web3's provider-level retry of `eth_sendRawTransaction`.** Retries are on by default. The
  bytes are identical so it cannot double-fund, but every derived budget assumes one 20s call
  per attempt and the provider does not honour that premise.
- **Load-balanced public RPC backends disagreeing.** All divergence reasoning is theoretical.
- **The full anvil rehearsal at this HEAD**, from here. Not run in this environment.

## Assumptions about third parties nobody has verified

- That `sepolia.base.org` implements the `safe` tag correctly rather than aliasing it to
  `latest`. `safe_head_policy` trusts one node for this, while abandoning a nonce requires two.
  The asymmetry runs toward the answer that becomes memory.
- That OpenRouter's constrained decoding actually constrains. A malformed dimension is caught
  by validation; a well-formed wrong one is not.

## Things a reader would reasonably assume and should not

- **That two agents on one host are isolated.** They are not, at the filesystem level. The
  separation is protocol-level: the negotiation logic gives neither side a write path into the
  other's store, and every import is gated on a confirmed receipt. One `chmod` or one process
  running as that user collapses it. The stated trust model claims more than the deployment
  supports; the narrower claim is the true one and is worth stating in those words.
- **That "confirmed" means final.** It means the ledger's block was canonical at the safe head
  when last checked, from one node.

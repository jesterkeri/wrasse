---
owner: joshua
last_verified: 2026-09-10
verified_by: table re-checked against the running build on 2026-09-10; the open-deal index was added after this page was written and is now listed
hop_ids: [J4, J5]
---

# 6. State and data

## Where everything lives

| State | Store | Writer | Readers | Truth or derived | Retention | Crosses a boundary |
|---|---|---|---|---|---|---|
| `store_identity` | each memory | `store.open` on adoption | every command | truth | forever | no |
| `persona_commitment` | provider memory | store creation | quote, signing | truth, digest-bound | forever | no |
| `chain_event` | each memory | `reconcile` | quote | **truth** | forever | yes, from chain |
| `behavior_dimension` | each memory | `learn-dimension` | quote | **truth, not reconstructible** | forever, retired not deleted | yes, from a model |
| `counterparty_index` | each memory | ingest, repair | recall | **derived** | forever | no |
| `chain_event_journalled` | each memory | ingest | repair | derived | forever | no |
| `pending_ingestion` | each memory | ingest | memory gate | marker | cleared on success | no |
| transaction rows | `transactions.db` | chain orchestrator | resolve, reconcile | truth for settlement | forever | no |
| open deals | `liabilities.db` | executor, before signing | boot recovery, refund | **truth for what is owed** | row removed when the deal closes | no |
| keystores | files | one-time | signing | truth | forever | no |

**Why the open-deal index exists at all.** The transaction ledger records a transaction; it does not parse the log that carries the deal id, so it cannot answer "which deals are open". Without that answer a settlement whose reply was lost leaves a deposit nobody collects. The index is written before signing and removed when the deal closes, which is what makes the money claim checkable rather than aspirational.

**The asymmetry that governs recovery.** A missing canonical entity (`chain_event`,
`behavior_dimension`) is fatal and unrecoverable. A missing projection (`counterparty_index`,
`chain_event_journalled`) is repairable, and repair refuses rather than truncating when it
cannot see the whole enumeration. This is correct and it is the reason the backup covers the
memory databases rather than only the receipts.

**What a restore recovers.** The two memory databases restore every category above. The ledger
does not, and is not backed up. So a restore gives you two memories that know what settled, and
no record of any transaction that was in flight at the moment of loss. For a demo that is the
right trade.

**What is not reconstructible, stated precisely.** `chain_event` rows can in principle be
re-derived by replaying Base receipts, though no scanner exists to do it. `behavior_dimension`
rows cannot: they are the output of a constrained model call, and nothing on chain records what
the model said. Reproducing a quote needs the persona, the receipts and the dimensions.

## Onchain surface

`WrasseEscrow` at `0x5525653f05990DA1479578893b5a624183AFa22E`, chain 84532, immutable.

| Function | State written | Who may call |
|---|---|---|
| `createDeal` | new deal, funded | buyer, bound to the recovered signer |
| `acceptDeal` | accepted | provider |
| `markDelivered` | delivered | provider |
| `releaseDeal` | released | buyer |
| `claimPayment` | paid out | provider, after the payout delay |
| `claimTimeout` | refunded | buyer, after the window |
| `cancelUnaccepted` | cancelled | buyer, before acceptance |
| `withdraw` | balance moved | the holder |

**Verified how.** This table is read from the calldata builders in `escrow.py` and the integrity
checks in `chain.py`, which decode the signed bytes and check action, deal id, role and
payability before anything acts on a row. It has **not** been generated from a static analyser
and checked against the contract source, because the contract is out of scope at this HEAD.
Treat it as `verified_by: code read`, not as an auth audit.

**Events.** Every state change emits one, and `reconcile` is the only path that turns an event
into memory. There is no indexer and no monitor: if `reconcile` is not run, nothing notices.

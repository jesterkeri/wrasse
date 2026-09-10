---
owner: joshua
last_verified: 2026-09-06
verified_by: review record read
hop_ids: []
---

# 9. Decisions

Append-only. Superseded, never edited to reverse. The full reasoning for each lives in
`docs/orchestrator-review.md` and `docs/contract-review.md`; this is the index.

| # | Decision | Consequence | Door |
|---|---|---|---|
| 1 | Two independently held memories rather than one shared store | A provider reading the buyer's dimensions would not be an independent memory. Costs a whole class of locking. | one-way |
| 2 | Base receipts are the only import path into memory | Nothing enters memory that a stranger cannot verify. Requires `reconcile` to be run, which nothing enforces. | one-way |
| 3 | The document is a display artifact; the memories are the authority | `create-deal` rebuilds both halves and refuses what it cannot reproduce. Internal consistency is not provenance. | one-way |
| 4 | Immutable contract, no proxy, no pause | No upgrade risk, no admin key risk, no pause available. A bug is a redeploy. | one-way |
| 5 | Per-command broadcast opt-in rather than a config flag | A signed transaction moves funds without decrypting the key again, so the opt-in must not live in a file. | two-way |
| 6 | Abandoning a nonce requires two RPC endpoints to agree | Prevents a lying node freeing a live nonce. Makes the fallback RPC mandatory for recovery. | two-way |
| 7 | Walk-aways published as well as derived | A reader without Wrasse's rules gets the number; a reader with them gets a proof. | two-way |
| 8 | Three-quarters concession share as stated policy | Places the refusal boundary between profiles. Not derived. | two-way |
| 9 | Memory backed up to a private repo, memories only | Keystores, password and ledger deliberately excluded. A restore gives memories and no in-flight settlement state. | two-way |
| 10 | Hosted quote service is read-only, holds no key | Deletes serialisation, session isolation, funding and wallet-death from the hosted path. Settlement stays operator-driven. | two-way |

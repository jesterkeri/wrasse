# Known limits

What this build does not do, written down rather than left to be discovered. Everything here
is accepted for judging. A review that re-reports one of these is reporting a decision, not a
defect.

## The memory subsystem

**The quote path does not verify index completeness.** A lost counterparty-index entry prices
as a cold start, and a cold start is exactly what a genuinely unknown counterparty looks like,
so the failure reads as a correct answer. `wrasse store-repair` detects and repairs it, and
nothing runs it automatically. The cross-store comparison in `_bilateral_quote` catches a
one-sided loss only; a loss both stores share is undetected, because both agree on the empty
set and the quote proceeds cold and confident.

**`pending_ingestions` has no full-page guard**, unlike `repair_index` and `load_dimensions`.
Benign where it is read today, since a truncated list is still non-empty and still refuses. It
would matter to a repair that reported itself complete after a thousand.

**`indexed_event_ids` documents a role it does not have.** Its docstring says it compares the
two sides before a bilateral quote. The comparison uses `recall().evidence`. It has no
production caller.

**`commit_persona` proves precedence over receipts, not over learned ontology.** Its emptiness
check reads only `chain_event` rows, so a store holding a learned dimension and no receipts can
still commit a persona. `WrasseStore.open`'s adoption check is stricter.

**Full history is not reconstructed from the chain.** Supplied receipts can be reverified and
replayed. There is no log scanner, and the learned dimensions were produced by a model and
cannot be rebuilt from a log at all.

## The hosted quote service

**The memory databases cannot be mounted read-only.** The Sibyl client opens SQLite read-write
and sets write-ahead logging, and the store's lock file is created in write mode, so a
genuinely read-only file does not open. The source is mounted read-only and copied to a
writable working directory at startup instead. The deployed artifact cannot be mutated and the
working copy is recreated on every restart.

**The service writes metadata at startup, and nothing while serving.** A store that does not
exist gets its identity record written on first open, and the persona commitment is written
when it is absent. Both happen before the first request, so the serving path writes no receipt,
no learned dimension and no outcome. The module used to claim it wrote nothing at all, which
was false on a cold deployment.

**The service holds no keys and signs nothing.** Settlement runs on the operator's machine.
That is a design constraint rather than a limitation, and it is recorded here because removing
it reintroduces the wallet serialisation and wallet-death problems it was chosen to delete.

## The transaction orchestrator

**Five findings from the Gate 4 review are deferred**, with the reasoning in
`docs/orchestrator-review.md`. That layer never reached a passing verdict and must not be
described as review-clean.

**One slow RPC blocks every other ledger writer for up to 102 seconds.** The ledger's write
transaction is held across three chain reads inside the signing callback. Deliberate, and the
budgets agree, but it is worth knowing before running two processes in front of judges.

## The commitment

**The engine version covers the parameters, not the procedure.** Every constant and table that
parameterises a settlement is inside the digest. The comparison operators, the inclusive
boundary, the gap arithmetic and the integer division are code that no digest here
fingerprints. Hashing the source would make a comment edit invalidate every document ever
written. The procedure is checked a different way: every document publishes all twelve numbers
per profile, so a reader recomputes the settlement by hand and a drifted build disagrees with
that arithmetic visibly.

**A frozen sample proves its own arithmetic, not its own history.** The risk values came from
the two receipts on Base, and a third party cannot derive that from this repository alone,
because the learned dimensions and both memory stores are deliberately not part of it.

**Executability cannot prove who observed a block.** A live quote establishes that the document
names a recent canonical Base block matching its own reference. Re-querying a public fact later
says nothing about who read it first.

## Scope, stated once

Local-machine compromise. Byzantine RPC comparison. Wallet identity resets. Arbitrary
contract-wallet recipients beyond the credit ledger's guarantee. Access isolation between the
two stores: the demo uses separate stores representing independently held memories, and
production isolation is outside this MVP.

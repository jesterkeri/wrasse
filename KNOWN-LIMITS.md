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

**The quote path writes metadata at startup and nothing while serving.** A store that does not
exist gets its identity record written on first open, and the persona commitment is written
when it is absent. Both happen before the first request, so quoting writes no receipt, no
learned dimension and no outcome. `/api/health` reports this as
`quote_writes_receipts_or_outcomes`. The module used to claim it wrote nothing at all, which
was false on a cold deployment.

**Two deployments, and only one of them signs.** With `WRASSE_ENABLE_EXECUTION` unset the
service holds no keys and sends nothing. With it set the service holds both keystores, signs
on behalf of both wallets, and writes receipts into the visitor's own copy of the two
memories. `/api/health` reports `signs` and `holds_keys` from the same flag, so which one is
running is a fact a reader can check rather than a claim in a document. An earlier version of
this file said the service never signs, which stopped being true the moment a judge could
perform a settlement.

**The executing deployment shares two wallets between every visitor.** One worker runs one
settlement at a time, which is what stops two sends taking the same nonce, and it means the
third visitor in a queue waits for the two ahead of them. The bounds are
`WRASSE_RUNS_PER_SESSION` and `WRASSE_TOTAL_RUN_CEILING`, and neither makes an empty wallet
safe.

**A restart loses every run and session; the money is recovered from a separate index.** The
queue, the run objects and the session registry are held in process memory. Three things are
not: the transaction ledger, the session databases, and `liabilities.db`, which records every
deal the moment its id exists and forgets it when it is closed. On boot the worker resolves the
ledger, refuses to sign at all if any row still holds a wallet, then closes every deal the
index still lists and collects what they released. That is why the money claim is checkable
rather than aspirational: the index is the durable answer to "which deals are open", and the
ledger cannot give it, because a ledger row records a transaction and the deal id lives in a
log it does not parse.

What a visitor loses is their run id and their session. `/api/run/{id}` returns 404, the page
notices, discards the dead identifier and starts a new session rather than reusing it forever.
The private history that session had built is gone; the two receipts everything starts from are
not. Persisting the run procedure itself would keep the link too and was not built, because
losing a link is an inconvenience and losing a deposit is not.

**A session directory that cannot be deleted is not reported.** When copying a session's
memories fails, the half-copied directory is removed with errors ignored, so a deletion that
itself fails leaves a directory nothing will open and nothing will evict. It costs disk on a
volume with five gigabytes and no path to a wrong answer, which is why it is written down here
rather than fixed on the last day.

**A refund is complete at inclusion, not at the safe head.** Every other place in this build
that turns a transaction into a durable fact waits for confirmation first, and a withdrawal
does not: the run reports success once the collection is in a block. A reorg of that block
would put the credit back in the escrow while the session reports itself refunded. The credit
is not lost, because the next reclaim or refund on this deployment collects whatever the escrow
is holding for either wallet, but that session's own completion claim would be wrong until then.
Waiting for the safe head would add about two and a half minutes to the end of every session.

**A refund collects the shared escrow credit, not this session's share of it.** The escrow
aggregates credits per wallet across every deal and has no notion of a session, so one visitor
pressing finish collects whatever the two wallets are owed at that moment, including another
visitor's. No value leaves the pair, and the pair is the demo's own two wallets, so this is an
accounting artefact rather than a loss. It does mean the refund figure a visitor is shown can
include a settlement that was not theirs.

**An ending that waits is bounded by `WRASSE_WAIT_TIMEOUT`, not by the contract.** Two of the
three endings are produced by letting a deadline actually pass, so the run has to stay alive
for the whole of it. A settled duration longer than that budget is refused at the quote, before
anything is signed, naming the number to lower. The contract would happily enforce a thirty day
window; this service will not sit through one.

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

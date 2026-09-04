# Transaction orchestrator: review record and known limitations

Two adversarial review rounds on `wrasse/chain.py` and the commands around it. Round one
raised twelve findings, round two nine. This records what was fixed, and what was
deliberately deferred with the reasoning, so nothing here is discovered later as a surprise.

## Fixed

Round one, all twelve: stable action identity independent of the moving terms; the intent
recheck inside the write lock; per-wallet serialisation; nonce allocation from pending state;
`unknown` separated from "never sent"; a pending nonce kept non-terminal; inclusion separated
from confirmation with the block hash rechecked; the broadcast opt-in extended to replay;
`policy.json` validated as untrusted input; ledger integrity by decoding the signed bytes;
deployment identity by runtime bytecode hash; and a disclosed failpoint in place of racing a
kill signal.

Round two, six of nine: nonce consumption established at the safe head rather than the tip,
with a distinct fallback endpoint required; ledger integrity checked before any confirmation,
because a confirmed row is what becomes memory; the buyer bound to the recovered signer, since
it never appears in the calldata; deployment identity and a fresh deadline check made
preconditions of replay, not only of the first send; and the nonce release moved out of the
caller into a ledger operation that refuses anything but a first attempt.

Every one of these was mutation-checked: the property was removed and the suite was confirmed
to fail.

## Deferred, with reasons

These are real. They are not fixed because the entry has six days left and the memory work
they compete with carries twice the rubric weight. Each is written here rather than left to be
found.

**Chain time is read once per resolver run, not per row.** A run over many rows uses an
observation that ages. The last-moment recheck immediately before a resend is in place, which
is the case that can lose money; the per-row staleness only affects reporting.

**~~`policy.json` can still misdescribe itself.~~ CORRECTED, and it was worse than written.**
This entry claimed that no substitution changes what is funded, because the displayed terms
are bound to the terms the signature commits to. That was wrong, and the error was in the
reasoning rather than in the code: the binding is between fields of one document, and the
commitment those fields produce is a public unkeyed hash of them. An editor who changes a
price everywhere it appears and recomputes the hash gets a file in which nothing disagrees
with anything, and the wallet signs the new number. Consistency is not provenance, and a
validator that only reads the document cannot tell the difference.

Fixed in the Gates 5-6 round two fold: `create-deal` derives both sides' terms again from the
two identified memories and refuses any document it cannot reproduce, bodies of recalled
evidence included. The baselines it derives against are its own arguments, not fields of the
document, because a baseline read out of the file would be one more number the editor gets to
choose. The document is now a display artifact; the memories are the authority.

**The ledger schema has no migration.** `CREATE TABLE IF NOT EXISTS` leaves an older database
with a `CHECK` that predates `unbroadcast` and a non-partial nonce index. No such database
exists, because nothing has been deployed. Before there is a live ledger this needs a version
stamp and an explicit migration, or a refusal with a reset instruction.

**~~`Retry-After` is honoured without an upper bound.~~ FIXED.** Capped at
`READ_MAX_DELAY_SECONDS`, and the whole operation now runs against a monotonic deadline rather
than a sum of the sleeps, so a call that stalls on a socket spends the budget it actually
spends.

## Not deferred, and not negotiable

The properties that stop money being lost are in place and tested: one logical action can
never produce two funded offers, a crash between broadcast and record is recovered without
rebuilding or re-signing, a nonce is never skipped or reused, an unproven consumption never
abandons a payload, and nothing is broadcast without a deliberate per-command opt-in.


## Gates 5 and 6, reviewed once

One CRITICAL, five MAJOR, two MINOR. Seven are fixed; the eighth is narrowed and recorded here.

**Fixed.** The counterparty index could omit a record after a crash and still authorise a
quote, which falsified the claim that terms reflect a complete history. An ingest now marks
the event id before writing the record and clears the mark only once the index agrees, any
outstanding mark stops quoting, index writes are serialised, repair refuses rather than
truncating when it cannot see past the enumeration limit, and a bilateral quote requires the
two memories to agree on which receipts exist.

`used_evidence_ids` claimed to name the receipts that moved a number and actually named the
ones that contributed to a sum. A contribution can clamp, round away, cancel against another,
or land on a cap, leaving every committed term identical to a cold start. It is now a minimal
causal set: removing any member changes a committed output, and the set alone reproduces the
same terms as the whole history.

An unowned database could be adopted as either side by configuration alone. Only a
demonstrably empty store is adopted now. `empty_store` and `no_match` are distinguished again.
Chain reads cap a server-supplied `Retry-After` and bound the whole operation; model calls
retry only what a retry could fix.

The model no longer decides which way an outcome points, and no longer sees a stored record.
Both changes came from a live call that read a provider's non-delivery as positive.

**Narrowed and deferred to Gate 7.** Schema 2 checks that the displayed economic terms match
the committed ones, that used evidence is held, that the persona commitment is a digest, that
risk is a bounded decimal, and that a side's verdict, cold-start flag and evidence agree. It
does **not** yet verify each recalled evidence body against the store it came from. An edited
document could therefore keep the genuine ids and the real signed preimage while showing an
invented description of a receipt. Funds still follow the committed terms, so this cannot
misdirect money; it can misdescribe a reason. Gate 7 rebuilds this document for the negotiation
receipt and folds body verification then. **Until it does, schema 2 should be described as
validating the terms rather than the whole explanation.**


## Gates 5 and 6, round two

Two CRITICAL, four MAJOR, two MINOR. All eight fixed, and the deferral above was retracted
rather than restated.

**A document that agrees with itself is not a document that came from here.** The second
CRITICAL is the one recorded above: internal consistency was mistaken for origin. A
consistently edited `policy.json` authorised a ninefold price. `create-deal` now rebuilds the
quote from the two memories and refuses anything it cannot reproduce.

**Repair was a writer without a lock.** The first CRITICAL had two halves. `repair_index`
performed an unlocked read-modify-write over the entry `ingest` writes, so a repair that read
before a concurrent ingest and wrote after put back its own stale copy, dropping an id while
the ingest had already cleared its marker. And the bilateral agreement check compared the two
indexes *before* recalling from them, which is a different question from what the terms were
computed from: an ingest finishing in between left the two sides pricing on different
histories. Repair now holds the writer's lock, recall takes a coherent snapshot, and the
comparison is over what each side actually recalled.

**The minimal causal set was minimal after one pass, not at a fixed point.** Removing a later
receipt can make an earlier retained one redundant, and a single pass never reconsidered it,
so two offsetting receipts left one named as having moved a number it did not move. Deletion
now runs to a fixed point in a fixed order, so the same evidence always produces the same set.

**Adoption checked three categories, and emptiness is a property of the file.** A store
holding only a learned dimension is economically active, and it could be adopted as either
side by configuration alone. The check is now an uncategorised enumeration, the identity row
must be `verified` and exactly the shape this build writes, and initialisation happens under
the store lock.

**Two active dimensions for one outcome scored the same receipt twice** while the evidence
hash named it once, so a bond could move for a reason the document could not show. Loading now
refuses a duplicated or truncated ontology, and learning is serialised.

**A confirmation belongs to a fork, not to a transaction.** `confirmed_success` is terminal, so
it survived a reorg that re-included the transaction in a block nothing had waited on.
Reconciliation now requires the live receipt to be in the block the ledger confirmed, and that
block to still be canonical. The reconciler tests had made this vacuous by giving their fake
row no block metadata at all; they now carry it.

**The persona precedence check ran outside the lock its claim depends on**, so a receipt
landing between the check and the write would leave a record claiming a precedence it did not
have. Now under the same lock, with the existing entity's status and shape validated.

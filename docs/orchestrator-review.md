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

**~~Narrowed and deferred to Gate 7.~~ SUPERSEDED, see round two and round three below.**
Schema 2's own checks are all internal: displayed terms against committed ones, used evidence
against recalled, a digest-shaped persona, a bounded risk, agreement between verdict,
cold-start flag and evidence. None of that establishes where any of it came from, which is the
question that matters. It is now settled at the signing boundary instead, by rebuilding both
halves of the document from the two memories, so nothing here is deferred to Gate 7 any more.


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


## Gates 5 and 6, round three

Two CRITICAL, four MAJOR, two MINOR. All eight fixed. Two of them were defects I had already
claimed to fix, which is the useful part of the round.

**Comparing two snapshots is not taking one.** The bilateral agreement check read the buyer,
released its lock, read the provider, and compared. That catches a receipt landing in the
provider in between and misses the same receipt landing in the buyer, because both returned
sets are then the old one and they agree. The test written for it injected into the side not
yet read, so reversing the injection made the property vanish without failing the test. Both
memories are now held, in a fixed order by lock path, across both recalls and the comparison.

**Provenance at one instant is not a binding.** The rebuild-from-memory check ran, and then the
command decrypted a keystore, read the deployment, observed chain time, estimated gas and read
fees. A `reconcile` landing a newly confirmed receipt in that window meant the signature
committed terms the memories no longer produced, and the operator saw a success rather than
the promised refusal. The check now runs again inside the signing callback.

Round four found that this was still a snapshot: the check returned, and two chain-time
reads, an on-chain hash call and transaction construction happened before the signature, so
the same reconciliation simply landed a moment later. Both memories are now **held** for the
whole callback, until the signed bytes exist. "Immediately before signing" has to mean the
memories cannot move in between.

**A lock on the object is not a lock for the caller.** `_exclusive` counted depth on the store,
so while one thread held the file lock a second thread read that counter as its own
re-entrancy and walked in. The round-two repair and ingest interleaving came straight back
through it. Threads are now excluded by an `RLock` held across the whole section, and the file
lock is taken on the outermost entry that thread makes. The lock path is resolved first, so
two names for one database cannot take two different locks.

**Learning was neither serialised nor validated.** The lock was released before the model call
and the write took a fresh one without re-reading, so two learners could leave two active rows
and `load_dimensions` would then refuse every quote: the command meant to make a store
quoteable was able to stop it quoting. `--relearn` retired only the first of them, so it could
not repair what it caused. And the event it learned from was fetched raw from whichever store
answered first, without status, id recomputation or participants checked, so an unverified row
could choose the template sent to the model. All active rows are now retired and re-checked
under the write lock, and the receipt goes through the same fail-closed validation the pricing
path uses, from both memories.

**The rebuild covered the terms, not the explanation.** Risk scores, the persona block and the
profiles nobody selected were outside it, so an invented reason could still stand beside a
genuine transaction. Both halves of the document are now rebuilt whole from memory and
compared, including the exact set of profiles offered.

**The identity validator did not require the field that dates it.** And the read budget was 15
seconds where three twenty second attempts were permitted, a number describing an intention
rather than the code. It is derived now, and no attempt is begun without room to pay for it.


## Gates 5 and 6, round four

Two CRITICAL, two MAJOR, one MINOR. All five fixed. Both criticals were holes opened by the
previous round's fixes rather than survivals of the original defects.

**The narrowed recheck dropped every profile, including the selected one.** The reasoning
written for it was that any memory movement shows up in the recalled evidence, so the profiles
could be skipped at signing time. That is false for the ontology: `learn-dimension --relearn`
changes what a receipt is worth without changing which receipts exist. Every field being
compared stayed identical while the bond and the window moved, and the old terms were signed.
The narrowed mode now compares the selected profile in full and skips only the profiles that
cannot reach a signature.

**The provenance check was still a snapshot, one boundary later.** See above.

**The two-store lock was also released too early inside the quote.** The ontology was read
after it, so a relearn between the recall and the term production would produce a quote whose
receipts came from one moment and whose readings came from another. Dimensions are read inside
the snapshot now.

**Comparison was Python equality, which is looser than JSON's types.** `True == 1`, so a
receipt whose `deal_id` was rewritten from `1` to `true` compared equal to the body actually
held. Both halves are compared by canonical encoding.

**`executability` is the one part of the document memory cannot rebuild.** It records what a
past run observed, so nothing in the stores can confirm it, and a supplied-reference fixture
relabelled as a live Base quote passed every check. The nested chain fields are now bounded,
the note is a closed set of the two sentences this build writes, and at signing the claimed
block is read from the chain and its timestamp compared. A quote that says it was judged
against Base is now checked against Base.

**The reconciler suite patched the integrity gate away for every test**, so deleting the
production call changed nothing. The patch is opt-out now, and one test runs with the real
verifier and a row whose bytes do not match its claims.

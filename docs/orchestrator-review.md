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

**`policy.json` can still misdescribe itself.** The economic terms a person reads are bound to
the terms the signature commits to, and the calldata is built from the validated preimage, so
no substitution changes what is funded. But the executability label, the evidence bodies and
the profiles that were not selected are shape-checked rather than semantically validated. A
tampered local file could therefore show a false *reason* beside a genuine transaction. The
threat model here is an attacker who already has write access to the machine, which the trust
model places out of scope. It still weakens the explainable-receipt claim, and the claim
should be stated as covering the committed terms rather than the whole document.

**The ledger schema has no migration.** `CREATE TABLE IF NOT EXISTS` leaves an older database
with a `CHECK` that predates `unbroadcast` and a non-partial nonce index. No such database
exists, because nothing has been deployed. Before there is a live ledger this needs a version
stamp and an explicit migration, or a refusal with a reset instruction.

**`Retry-After` is honoured without an upper bound.** A hostile or misconfigured endpoint
returning a very large value would stall a command, and the retry budget bounds attempts
rather than total time. The RPC endpoint is ours and is configured locally.

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

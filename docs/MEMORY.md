# Memory implementation note

What Wrasse persists, what it recalls, and what that recall is allowed to decide.

Two agents negotiate a job. Each has its own memory, a separate store on the Sibyl memory
client over SQLite, and each may only cite what the OTHER party did. **They are separate
stores representing independently held memories, not access-isolated processes:** on the
live demo one quote coordinator opens both and reads both, which is how it produces a
bilateral quote in one pass. Enforcing that boundary at the process or credential level is
out of scope for this MVP and is recorded as such in `KNOWN-LIMITS.md`. Their terms move between negotiations because each one prices
the risk the other has already demonstrated. Take either memory away and there are no terms at
all, which you can check yourself in about three seconds: see **Checking it** at the end.

## What is persisted

Seven categories. Only the first is evidence; the rest exist so the store can say honestly what
it holds.

| Category | What it is |
| --- | --- |
| `chain_event` | A settlement outcome, verified against Base Sepolia and keyed by its own identity |
| `chain_event_journalled` | A marker that the readable journal already carries that event |
| `behavior_dimension` | A learned reading of what an outcome type means for future pricing |
| `counterparty_index` | Which counterparty each event is evidence about |
| `store_identity` | Which of the two stores this is, written once on first open |
| `persona_commitment` | The digest of the provider persona this store was opened against |
| `pending_ingestion` | An ingest that started and has not finished |

A `chain_event` is identified by `keccak(chainId, contractAddress, txHash, logIndex)`. The deal
id is deliberately not part of that: a deal id is assigned by the contract and is not what makes
a receipt unique. Storing the same event twice is a no-op; storing a different body under an
existing identity is refused rather than merged.

`pending_ingestion` is written before the record and cleared only once the record and the index
agree. An outstanding marker means an ingest died in the middle, so the store cannot say what it
holds, and a store that cannot say what it holds does not price anything.

## What is recalled, and when

At quote time each side asks its own store for the events it holds about the *other* party, by
counterparty address. Nobody may cite their own conduct. The recall returns the events, a
verdict, and the ids actually used.

Those ids are hashed into the policy commitment, so the signed document says which receipts
moved which numbers. A commitment over everything the store happened to hold would describe the
reading rather than the reasoning.

## What the recall decides

Four terms, and every movement in them traces to a receipt or to a named constant:

- the price the buyer pays
- the stake the seller locks up
- how long the seller has to deliver
- how long the seller waits to be paid

An outcome is not scored by a hardcoded table. `behavior_dimension` entries are learned readings
of what each outcome type implies, produced outside the pricing path and constrained: an outcome
neither store has learned to read is **refused by name**, never scored as harmless. That is the
difference between a memory and a lookup table, and it is why an unfamiliar event stops a quote
instead of quietly pricing as if nothing happened.

## What may never be written

- **Nothing becomes memory from an intention.** Only a receipt this build has verified against
  the chain, so an agent cannot be slandered with an outcome that never executed.
- **Nothing becomes memory at inclusion.** Inclusion in a block frees a wallet, about ten
  seconds in. Memory may only be written once Base can no longer reverse the transaction, about
  two minutes later. A memory that could be withdrawn would leave every deal signed in between
  committed to a history that no longer exists.
- **A receipt is never rewritten.** Verified chain events are append-only. A better record
  earns better terms; a worse one is not edited away, and no command removes one.
- **A reading of an outcome can be replaced, deliberately and on the record.**
  `learn-dimension --relearn` retires the dimension currently held for an outcome type and
  asks for another, which changes what an unchanged receipt is worth. Nothing is erased:
  the retired definition stays in the store, marked retired, so a bad reading can be
  corrected without pretending it was never held. Saying "nothing is rewritten" without
  this qualification would be false.
- **Serving writes nothing at all.** The identity record and the persona commitment are written
  once at startup, before anything is served, so every request afterwards is a read.

## Fail closed, in four ways

A memory that is missing, unreadable, altered, mid-ingest, or holding an outcome it has not
learned to read does not degrade to a cold start. It stops.

The distinction that matters most is between an **absent** store and an **empty** one. An empty
store is present, has been asked, and holds nothing: an honest cold start, and it is the control
the whole comparison rests on. An absent store is a question nobody answered. Conflating the two
is exactly the wrapper behaviour the eligibility test is looking for, so a missing store refuses
and a present empty one quotes.

## Checking it

Press **Take the memory away** on the live page, or run:

```
uv run python scripts/delete-the-memory.py
```

Both call the same four cases in `wrasse/prove.py`, against a throwaway copy of the memories the
deployment is actually serving, so the terminal and the browser cannot disagree about whether
this passes:

| Case | Result |
| --- | --- |
| both memories intact | terms |
| both memory files deleted | refused |
| a memory that will not open | refused |
| one character of a transaction hash changed | refused |

The control matters as much as the three failures. Three refusals alone would only show that
this build refuses; the intact run is what shows it refuses *because* the memory is gone.

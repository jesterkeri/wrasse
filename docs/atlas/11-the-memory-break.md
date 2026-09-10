---
owner: joshua
last_verified: 2026-09-10
verified_by: all four cases executed against the running build on 2026-09-10, from both the terminal and the hosted page
hop_ids: []
---

# 11. The memory break

The page a judge should open first. Everything else in this atlas describes how the system
works; this one answers whether it needs its memory at all.

## The rule, quoted

> Delete the memory layer. If your project still does what it claims, it is a wrapper and does
> not qualify. If the core function breaks, memory is load-bearing.

It is a claim about behaviour, so it is answered by behaviour rather than by a paragraph.

## Run it yourself, two ways, one implementation

```bash
uv run python scripts/delete-the-memory.py
```

Or press **Take the memory away** on the live page, which is step two of three. Both call the
four cases in `wrasse/prove.py`, so the terminal and the browser cannot drift apart and
disagree about whether this passes.

Everything happens on a throwaway copy in a temporary directory, taken through SQLite's own
backup from a read-only connection, and destroyed afterwards. The real memories are read to be
copied and never written, which is asserted by file hash in `tests/test_service.py` rather than
by reading the code.

## What each case does, and what came back

| The memory is | What is done to it | Result |
|---|---|---|
| intact | nothing | **terms**: 0.000118 ETH, a 24.8% seller stake, delivered in 5 minutes |
| deleted | both files removed | **refused**: *the buyer memory does not exist. Terms are produced from what these two remember, so there is nothing to produce them from.* |
| unreadable | one file overwritten with a page of noise | **refused**: *file is not a database* |
| altered | one character of one transaction hash changed | **refused**: *`0x90c366fa…` recomputes to `0x8cf8acce…`; its contents changed after it was stored* |

## Why the control row matters as much as the three failures

Three refusals on their own would only show that this build refuses. The intact run is what
shows it refuses **because** the memory is gone, rather than because it refuses everything.
A test with no passing case proves nothing about the thing it is testing.

## Why the last row is the one worth reading

Nothing is missing and nothing errors. The store opens, answers, and hands back a receipt whose
stored contents no longer produce its own name. A project that was only displaying its memory
would price that deal and never notice.

A receipt's identity is `keccak(chainId, contract, txHash, logIndex)`. Recall recomputes it, and
a row that no longer produces the id it is filed under is not evidence of anything. That is the
difference between memory and decoration, and it is the only one of the four cases a wrapper
could not survive by accident.

## The part that is easy to leave out

**This test failed the first time it was run.** Deleting both memories produced baseline terms
and called it a cold start. That is precisely the wrapper behaviour the rule is looking for, and
it shipped green through five review rounds because every test provisioned a store before
asking.

The cause was a conflation. An empty store and an absent store are not the same fact. An empty
store is present, has been asked, and holds nothing, which is an honest cold start and is the
control the whole comparison rests on. An absent store is a question nobody answered, and
answering it with terms is the thing that disqualifies. Fixed at `84d95a5`; `tests/test_cli.py`
holds the refusal so it cannot come back.

## What this does not prove

That memory improves the outcome. It proves the outcome does not exist without it. Whether the
terms it produces are *good* is a different question, argued in `docs/orchestrator-review.md`
over nine rounds and not settled here.

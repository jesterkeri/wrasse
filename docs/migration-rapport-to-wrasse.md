# Rename: `rapport` to `wrasse`

Commit `c3c621e` renamed the package, the contract and the state directory. It is
described as a rename, but two of its effects are semantic, and both are recorded
here so neither is mistaken later for a bug.

## Two deterministic outputs changed

**The engine version string.** `wrasse/0.1.0` replaces the old value, and that
string is hashed into every policy commitment. The same terms therefore produce a
different `policyHash` before and after the rename. Solidity recomputes the new
fixture from its own `abi.encode`, so cross-language agreement is proven rather
than copied.

**~~The seeded bid stream.~~ HISTORICAL, and superseded at Gate 7.** This paragraph
described `wrasse/providers.py` namespacing a random seed with the package name so
that identical provider and seed inputs generated a different bid, pinned by
`tests/test_providers.py`. Neither exists any more. Gate 7 deleted that path: it
implemented a midpoint counteroffer, which is not the settlement this build uses,
and a function advertising a negotiation the build does not perform is a false
claim sitting in the source.

What makes a provider's terms reproducible now is not a retained seed. It is that
the persona is committed to git before its store holds a receipt, its SHA-256 is
recorded in that store, and every number it produces is a deterministic function
of the persona, the receipts, the operator's baselines and the stored dimensions
those receipts are scored against. The last two are inputs as much as the first
two are, and the earlier wording named only the first two.

## Why neither is a compatibility problem

No contract had been deployed when the rename landed, so no commitment existed
onchain to disagree with. No policy had been signed. No memory database existed,
so the migration reset described in the build plan was moot on the day, though
the rule stands for when one does exist.

The engine-version statement is load-bearing and still true. If it stops being
true, the rename becomes a breaking change and must be treated as one. The second
statement is history: read it as a record of what the rename did on the day, not
as a description of the code.

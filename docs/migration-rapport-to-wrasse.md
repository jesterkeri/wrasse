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

**The seeded bid stream.** `wrasse/providers.py` namespaces its random seed with
the package name, so identical provider and seed inputs now generate a different
bid. `tests/test_providers.py` pins one value, which turns any future change to
that namespace into a visible test failure instead of a silent drift.

## Why neither is a compatibility problem

No contract had been deployed when the rename landed, so no commitment existed
onchain to disagree with. No policy had been signed. No memory database existed,
so the migration reset described in the build plan was moot on the day, though
the rule stands for when one does exist.

Both statements are load-bearing. If either stops being true, the rename becomes
a breaking change and must be treated as one.

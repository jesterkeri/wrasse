---
owner: joshua
last_verified: 2026-09-10
verified_by: every row re-checked against the running build on 2026-09-10; the suite, the deletion test and both live quotes were executed from here
hop_ids: []
---

# 10. Claims and evidence

The judging page. A claim with an empty evidence column is a claim you cannot make in public.

| Claim | Evidence |
|---|---|
| **Deleting the memory stops this project doing what it claims** | Four cases, run from the live page or `scripts/delete-the-memory.py`, against a throwaway copy of the memories the deployment is serving. Intact quotes; both files deleted refuses; a file that will not open refuses; changing one character of one transaction hash refuses. Executed 2026-09-10. **This failed the first time it was written**: deleting both memories produced baseline terms and called it a cold start. Fixed at `84d95a5`. |
| An absent memory and an empty one are different facts | An empty store is present, has been asked, and holds nothing, so it quotes as an honest cold start. An absent store is refused. Conflating them is the wrapper behaviour the eligibility test looks for. `tests/test_cli.py` holds the refusal. |
| One logical action can never produce two funded offers | Stable action identity independent of the moving terms; intent recheck inside the write lock; mutation-checked |
| A crash between broadcast and record is recovered without rebuilding or re-signing | The ledger holds the signed bytes; J3. **Holds for `create-deal`. Does not currently hold for deal actions, see section 8.** |
| A nonce is never skipped or reused | `BEGIN IMMEDIATE` serialises allocation; nonce read from pending state at the safe head |
| Nothing is broadcast without a deliberate per-command opt-in | `WRASSE_ALLOW_BROADCAST` absent from `.env`; opt-in checked before `record_signed` at d364896; end-to-end test asserts the ledger is untouched and the *next* attempt succeeds |
| A document that agrees with itself is not a document that came from here | `create-deal` rebuilds both halves from the two memories and refuses what it cannot reproduce |
| Terms reflect a complete history | Marker before write, quoting refuses on an outstanding marker, bilateral agreement over what each side actually recalled |
| `used_evidence_ids` names the receipts that moved a number | Minimal causal set, deleted to a fixed point in a fixed order |
| The provider did not tune itself to this counterparty | Persona committed to git before any receipt; its hash written into the store at creation; changing it after evidence is refused |
| The buyer's price ceiling cannot move with the counterparty's misconduct | No risk term in the limit, by construction. `PriorityProfile` docstring states the reasoning. |
| Exactly one of four limits is memory-driven | `limit_kinds`: `provider_bond_bps: memory`, the other three `rule` |
| The refusal is entirely the provider's memory | Read from the live service on 2026-09-10: with no history `budget` agrees; with two receipts the buyer's ceiling stays 10500 and the seller's floor rises to 11350, refusing by 850. Same job, same engine, same starting numbers. |
| Comparison is by canonical encoding, not Python equality | Canonical encoding both halves; duplicate JSON member refused at decode |
| The memories are backed up and the backup is real | Private repo, re-cloned, SHA-256 compared against the originals, both copies opened and integrity-checked, contents audited for key-shaped strings |
| The suite passes | 744 Python tests, collected and executed here on 2026-09-10. The Solidity suite runs in CI, green on `main` at this commit; it was not executed from this environment, so no count is claimed for it. |
| Every load-bearing property is mutation-checked | Stated in the review record for rounds one and two, and for the d364896 fixes. Not independently reproduced here. |

**One row is deliberately weaker than the others.** The mutation-checking row is the author's
claim, recorded as such. Everything above it was read in the code or executed from here.

**Where this page can go stale.** It is a snapshot with a date at the top. `KNOWN-LIMITS.md` is
the maintained register and wins wherever the two disagree.

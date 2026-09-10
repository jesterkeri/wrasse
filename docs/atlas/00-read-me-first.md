---
owner: joshua
last_verified: 2026-09-10
verified_by: pages 0, 6, 8, 10 and 11 re-verified against the running build on 2026-09-10; pages 1-5, 7 and 9 last read at d364896 and not re-verified since
hop_ids: []
---

# Wrasse atlas

**What this covers.** What the parts are, what happens when you run each command, and where it
breaks. Written for the operator, which at 3am is you.

**What it does not cover.** The settlement rule's arithmetic (nine review rounds, closed, see
`docs/orchestrator-review.md`), the contract internals (immutable, see `docs/contract-review.md`),
and anything the frontend does.

**State of this atlas, stated before anything else.** It was written on 6 September at
`d364896`. The build moved a long way after that: the eligibility test, the deletion proof, the
hosted settlement panel and seven adversarial review rounds all landed later. On 10 September
pages 0, 6, 8 and 10 were re-verified line by line against the running build and corrected,
and page 11 was written from scratch. Pages
1 to 5, 7 and 9 describe the system at `d364896` and have **not** been re-read since, so
treat their detail as a snapshot rather than as current. `KNOWN-LIMITS.md` and `docs/MEMORY.md`
are the current authority where they disagree with anything here.

**Honesty note.** This is a representation of the system, not the system. Every page states how
it was verified. `not verified` appears where it is true, because a confident page with no
verification is worse than an honest gap. Section 8 lists what is known to be untested.

**How to update it.** After every incident, and after every upgrade. The postmortem template
asks which atlas page was wrong; that field is what keeps this true.

## Reading paths

| You are | Start at |
|---|---|
| Recovering a stuck wallet at 3am | 3, journey J3 |
| New to the codebase | 1, then 2, then 3 |
| An auditor or judge | **11 first**, then 6, 8 and 10 |
| Asking whether the memory is load-bearing | 11 |
| Deciding whether to ship | 8 |

## What is unusual about this system, stated once

- **There is no automation.** No keeper, no cron, no scheduled job, no daemon. Every state change
  is a command a human ran. This removes an entire class of failure and it means the control
  structure in section 4 has exactly one controller.
- **The contract is immutable.** No proxy, no upgrade path, no pause. Recovery from a contract
  bug is a redeploy and a fresh review, not an edit.
- **Nothing is reconstructible from chain alone.** The receipts are, the learned dimensions are
  not. Section 6 says exactly what a restore recovers.
- **Two agents run on one host.** The trust model's separation is protocol-level, not
  filesystem-level. Section 5 states what that does and does not buy.

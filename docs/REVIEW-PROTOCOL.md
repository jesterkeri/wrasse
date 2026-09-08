# Adversarial review protocol — the standing rules, including the ones for what the author cannot see

Every Wrasse review prompt is written by the person who wrote the code. That has a structural consequence worth
stating plainly at the top of every round:

> **A prompt can only point at KNOWN unknowns.** Everything the author lists is something the author already
> suspects. The findings that matter most are, by definition, absent from the list — and a reviewer working
> through a numbered list spends its attention where the author pointed, which is away from them.

The rules below exist to produce findings the author could not have asked for. They are procedures with outputs,
not exhortations to be thorough. **Cite this file from every review prompt and follow it in addition to whatever
that prompt asks.**

This file is carried over from the author's other repositories unchanged except for the project's name and the
examples below, which are that project's defects rather than this one's. The rules are general; the anecdotes
are not, and a reviewer should read them as illustrations of a shape rather than as history here.

---

## U1. Cold read first — AND THE MEASUREMENT IS NOT ENFORCEABLE IN ONE PROMPT

Read the diff and form your own list of findings BEFORE reading the prompt's questions. Then read the questions.
Anything they name that you did not independently find is a measurement of how much the framing steered you.

**Admitted defect in this rule, found by the reviewer it was written for (r18 MINOR 4).** "Do not read section 4
yet" is an instruction, not an information boundary. When the questions, the author's invariants and the design
rationale are all tokens in the same context window, the reviewer has already been exposed to them. The
resulting INSIDE/OUTSIDE count (U7) is therefore INDICATIVE, NOT RIGOROUS, and must be reported as such.

A genuine measurement needs three stages and two messages:

  1. Send ONLY the repository, the commit range, the unit boundaries, and this protocol. No questions, no
     invariants, no rationale.
  2. The reviewer returns its cold findings — ideally a hash or a committed list, so it cannot be revised.
  3. THEN send the questions and invariants. Anything named in step 3 that is absent from step 2 is the
     steering effect, measured rather than asserted.

Until a round is run that way, treat U7's counts as a weak signal. Do not report a single-prompt round as a
blind review, and do not let a low INSIDE count be read as evidence the framing was neutral.

## U2. Sweep every completeness claim

Search the changed code, its comments, and the commit message for universal quantifiers: *all, every, both,
only, never, always, none, the two places, N paths, exhaustive, cannot*. **Each one is a claim about a set.**
Enumerate the real set from the source and compare.

This is not a stylistic note. The author's most recent defect was exactly this shape: a commit message said
"both places reconciliation grants a stronger claim" and there were three. The third was the primary horizon of
the entire subsystem, and it shipped green.

## U3. Derive the invariants yourself, then diff

Work out what SHOULD hold from what the code is for, without reading the author's account of it. Then compare
against the invariants the author states. **Report any invariant the author never named** — an unnamed invariant
is one nobody is checking, and the author cannot ask you about it.

## U4. Follow every changed value to its consumers

For each value the diff changes or gates, find everything downstream: other computed fields, what is persisted,
what is rendered, what is exported. A control placed on a decision while a value DERIVED from that decision goes
ungated is the exact shape of the defect above — the status was gated, the number beside it was not, and the two
would have rendered contradicting each other.

## U5. Look hardest where there are no tests

Test files map the author's attention. A behaviour that changed with no test covering it is, almost by
definition, somewhere the author was not looking. List those.

## U6. Attack the premise, not only the implementation

The prompt argues FOR the design; that argument is not evidence. Ask whether the approach is right at all, not
just whether it was built correctly. If the honest answer is "this should not exist" or "this should be refused
rather than gated", say so — the author has usually stopped asking that question by the time they write it up.

## U7. Classify every finding INSIDE or OUTSIDE the prompt's stated concerns

Label each finding. End the review with the count of each. **A round where every finding is INSIDE is a warning
sign about the review, not a clean bill for the code** — it means the framing did the choosing.

## U8. Name what you could NOT check

Files you did not open, paths you could not execute, claims you took on trust. The absence of a finding in an
area is not a clean bill for that area, and silence reads as clearance unless it is labelled.

## U9. The scope line is the author's guess about where the bugs are

Prompts scope narrowly for efficiency, and that is the strongest steer in the whole document. If a finding's
ROOT lies outside the stated range, say so rather than staying inside the boundary. Periodically the author owes
you an unscoped sweep; if it has been many rounds since the last one, say that too.

---

## For the author, not the reviewer

- **Do not list your own known weaknesses in the prompt.** Naming them destroys the only measurement you have of
  whether the reviewer found them or read them back to you. Hold them until the review returns, then compare.
- **Do not present the design as settled.** State what it does and what it must guarantee; leave the argument
  for it out, or U6 has nothing to bite on.
- **Apply U2 to the prompt itself.** The r18 prompt said "SIX independent units" and then listed A through G,
  which is seven; it said the live-test plan had 24 items when the tables held 30. Both were caught by the
  reviewer, in a document whose own U2 rule is "every count is a claim about a set — enumerate the real set".
  Count before writing a count, including in the sentence telling someone else to count.
- **Do not list known weaknesses — and notice when the prompt does it anyway.** The r18 prompt carried a
  "known-open and out of scope" section. Scoping is legitimate, but it is one edit away from becoming the
  weakness list this file forbids. If a section exists so the reviewer will not waste effort, say only WHAT is
  deferred, never where you suspect it is weak.

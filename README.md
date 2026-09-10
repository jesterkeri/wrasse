# Wrasse

Wrasse is a hackathon prototype in which agents remember how a counterparty
behaved, use that evidence to produce the terms of the next negotiation, execute
accepted terms on Base, and write the verified outcome back to Sibyl Memory.

> Memory changes real economic behaviour across sessions, and the outcome comes
> back as verified evidence.

## Try it, and check it

| | |
|---|---|
| Live demo | **https://wrasse.vercel.app** |
| Escrow contract | [`0x5525653f05990DA1479578893b5a624183AFa22E`](https://sepolia.basescan.org/address/0x5525653f05990DA1479578893b5a624183AFa22E) on Base Sepolia, chain 84532 |
| Deployed from | commit `7e37c8e`, solc 0.8.30, optimizer on at 200 runs, recorded in `deployments/base-sepolia.json` |
| Settled through it so far | 25 deals: 16 delivered and paid, 8 never delivered, 1 cancelled |

Nothing on that page is a recording. The quote is computed when you ask for it, the simulator
runs the same engine over a history you choose, and the settle button performs a real deal on
Base Sepolia and shows you its transactions while they are being mined. Every number it prints
appears in a transaction you can open on the explorer above.

Two things are worth knowing before you press anything. Both wallets are shared and funded from
a faucet, so one worker settles one deal at a time and the page tells you your place in the
queue. And the endings that fail take longer on purpose: a deal that is never delivered cannot
be claimed until the delivery window has actually run out, which the contract enforces in block
time, so it is produced rather than declared.

## Why memory is load-bearing

The hackathon's test is exact, so this repository answers it exactly:

> Delete the memory layer. If your project still does what it claims, it is a wrapper and does
> not qualify. If the core function breaks, memory is load-bearing.

```bash
uv run python scripts/delete-the-memory.py
```

That takes the memory away four ways, on throwaway copies that are destroyed afterwards, and
reports what the project does each time. Terms come back only in the first case:

| what is done to the memory | what the project does |
|---|---|
| nothing, both intact | produces terms |
| both files deleted | refuses, and says the memory does not exist |
| a file that will not open | refuses, and says it is not a database |
| a receipt edited in place | refuses, and names the row that no longer proves its own identity |

The first row prints the terms it produced, so the contrast is on the same screen: `urgent settles at
0.000118 ETH, stake 24.8%, deliver within 5 minutes`, which are the numbers the live quote shows.
The last row prints both hashes, the one the receipt is filed under and the one its contents now
produce.

The last one is the one worth reading. Nothing is missing and nothing errors: the store opens,
answers, and hands back a receipt whose transaction hash has been changed. A project that was
only displaying its memory would price that deal and never notice. A receipt's identity is
`keccak(chainId, contract, txHash, logIndex)`, recall recomputes it, and a row that no longer
produces its own name is not evidence of anything.

**This test failed when it was first written, and that is why it exists.** Deleting both
memories produced baseline terms and called it a cold start. The two are not the same fact: an
empty store is present, has been asked and holds nothing about this counterparty, while a
missing store is a question nobody answered. Answering the second with terms is exactly what a
wrapper does. It is refused now, and `tests/test_cli.py` holds that refusal so it cannot come
back.


The pricing path reads verified counterparty evidence through
`WrasseStore.recall` in `wrasse/store.py`: an exact index of canonical event
ids, resolved one lookup at a time. Fuzzy search is for display and never sets
a price, because it can miss a matching row without reaching its limit, so
validating what it returns proves nothing about what it left out.
`wrasse/memory_gate.py` remains for the display-side `recall` command.

If Sibyl raises an error, Wrasse raises `MemoryRequired` and produces no terms.
A reachable store with no matching counterparty is an explicit cold start, and
a store holding history about other counterparties is reported as no match
rather than as empty.

Verified Base outcomes are written in `wrasse/evidence.py`, after the receipt,
chain, contract, event signature, deal, provider, and final state are checked in
`wrasse/reconciler.py`. The WARM `chain_event/<event_id>` entity is the
canonical idempotency record; its event ID is also journaled as COLD history.

Remove or break the memory calls and the next deal cannot be priced. That is the
critical path, not an activity log attached after the decision.

## Current scope

- One service type and three profiles: urgent, budget, and sensitive.
- One simulated provider agent, `atlas`, whose persona is committed to git and whose
  SHA-256 is written into its store before that store holds any receipt.
- One negotiation round over four terms. Each side proposes two and publishes limits on the
  other two; the settlement meets at the accepting side's limit, and where the two sides leave
  no overlap it refuses and says which term and by how much.
- One escrow contract on Base Sepolia.
- One constrained OpenRouter call when a new behavioural dimension is first
  created. Stored dimensions are reused; the LLM is never called in the policy
  path.

The provider agent is a simulation. What makes its terms reproducible is not a retained
random seed, which no longer exists: it is that the persona is committed to git before the
store holds a receipt, its SHA-256 is recorded in that store, and every number it produces is
a deterministic function of four things a reader can be handed. The persona. The receipts. The
operator's baseline price, bond, window and delay, which are command-line flags and appear in
every document under `baseline`. And the stored dimensions, which carry the severity and
confidence a receipt is scored with. Receipts alone are not enough, and saying they were left
a judge following the recipe unable to reproduce the numbers.

## The hosted demo, and what a visitor can actually do

Everything below this section is the command line, which is where the work happens. This
section is what somebody with a link gets, because a settlement nobody can perform is a claim
rather than a demonstration.

The page has three surfaces and they answer different questions.

**The quote** is memory and arithmetic, and it is instant. It shows what the two sides settle
on today, what each one proposed, the limits each published, and which receipt, counterparty
limit or named rule moved every number that moved. A switch turns memory off, and the point of
it is the profile that changes answer: with the two receipts in memory the cost-sensitive buyer
is refused, because this buyer's own payment record raised the seller's price floor above what
that profile will pay. Turn memory off and the same buyer is quoted a deal.

**The simulator** runs the real engine over a history you choose rather than one that happened.
Nothing is written to any store and no wallet is touched. It exists because the live memories
hold two receipts and always will, and the argument this project is making is about what
happens across ten. Set two failures and watch the terms move; add kept promises and watch them
come back. They come back to a different place than they started, because nothing is rewritten
and the failures are still in the history at the end of it. An outcome the ontology has never
learned is refused by name rather than scored as zero.

**The settlement** is the one that signs. A visitor picks the four starting numbers, which of
the three profiles negotiates, and how the deal ends, then performs it on Base Sepolia. The
ending is produced rather than declared: a deal that is never delivered has to sit out the
delivery window before the contract will allow the claim, and one whose payment is made to wait
has to sit out the payment delay. That is why two of the three endings take longer, and it is
why the receipt at the end is evidence rather than a label. Each run teaches both memories, so
five runs build a history and the terms move underneath the visitor as they go. At the end the
escrow is emptied back into the two wallets.

Two wallets are shared by everyone, both funded from a faucet, and one worker runs one
settlement at a time. That is not a limitation to engineer around: the transaction ledger
permits one unresolved transaction per wallet, which is what stops two sends taking the same
nonce, and a pool of workers would spend its life contending for the same two nonces. What it
costs is that the third visitor in a queue waits, and the page says so. The quote and the
simulator are unqueued, so the memory argument lands even while the chain is busy.

`/api/health` reports whether the deployment you are talking to signs at all, because the same
code runs read-only without keys and there should be no need to take a document's word for it.
`docs/DEPLOY.md` is the runbook, and `KNOWN-LIMITS.md` is what this build does not do.

## Setup and tests

```bash
uv sync --extra dev
uv run ruff check wrasse tests
uv run pytest
cd contracts
forge test -vv
```

`.github/workflows/ci.yml` runs exactly those commands, plus a wheel build and an
install check. It also fails the job if the anvil orchestrator suite *skips*,
because those tests skip themselves when anvil or the compiled artifact is
missing and a silent skip in CI is a tick nobody earned. `ruff` is configured
narrowly, to catch dead code and stale imports rather than to have opinions
about style.

The terminal interface currently exposes:

```bash
uv run wrasse recall <provider-address>
uv run wrasse reconcile --tx <tx-hash>
uv run wrasse learn-dimension <event-id>
uv run wrasse policy <provider-address> \
  --buyer <buyer-address> \
  --accept-window 3600 \
  --output policy.json
```

`--buyer` is required because the contract commits to it. The acceptance
deadline is given either way round, and the choice decides what the run is:

- `--accept-window <seconds>` reads the latest Base block and derives the
  absolute deadline from it. A live quote.
- `--accept-by <unix-deadline>` alone also reads the chain, and checks the
  supplied deadline against it. This is the form a deadline agreed during
  negotiation arrives in. Also a live quote.
- `--accept-by` together with `--reference-timestamp <unix-time>` reads no chain
  at all and judges the terms against the supplied time. The result is
  reproducible and is labelled **not executable**, because a supplied time is
  not the time Base will enforce.

Supplying neither deadline form, or both, is an error rather than a default.

Every deadline is enforced by the block that mines the transaction, so a live
quote is checked against observed chain time plus an inclusion margin
(`--inclusion-margin`, 120 seconds by default). The margin also absorbs how far
behind the read block already was, because a lagging node and a pending
transaction are the same distance from the real chain tip. Allowing both
independently is what would let an already expired deadline look comfortable.

The local clock is never the authority. It appears only as a bound on how far
the observed block may sit from now, and as lag spent out of that margin. Both
can refuse a quote; neither can approve one. Each `policy.json` carries an
`executability` block naming the basis, the block observed, the margin and the
lag, so a consumer never has to infer what the check was worth.

## The negotiation

Four terms, and neither side sets any of them alone. Each side proposes two and publishes
limits on the other two, and a settlement resolves the pair:

| term | proposed by | limit published by |
|---|---|---|
| `provider_bond_bps` | buyer | provider, the most it will post |
| `service_window` | buyer | provider, the least it needs |
| `price_wei` | provider | buyer, the most it will pay |
| `payout_delay` | provider | buyer, the least it will accept |

Where the opposer publishes a ceiling the settlement is `min(proposal, ceiling)`; where it
publishes a floor it is `max(proposal, floor)`. A side concedes back toward the baseline it
would have quoted a stranger, never past it, and never past a number it proposed itself. When
the two leave no value either would accept, the profile **refuses**, naming the term and the
distance. That is an outcome rather than an error, and it is better than quoting terms one
side has already declined.

**A limit may move with its publisher's memory only if the movement is monotone against the
party whose conduct moved it.** Two do: the provider's bond ceiling and its price floor, both
driven by its record of *this buyer*. The buyer's price ceiling does not, because a ceiling
that fell as the provider misbehaved made the buyer pay less as it was wronged more and then
refused outright, punishing the party that suffered rather than the one that caused it.
Willingness to pay is a fact about the job, not about the counterparty.

The claim is component-wise and it has one stated exception. A worse provider record cannot
lower the price it is paid, cannot lower the bond it must post, and cannot turn a deal into a
refusal. It *can* lengthen the service window: `budget` and `sensitive` carry positive window
buffers because a cost- or quality-sensitive buyer that has been let down may rationally grant
a longer realistic deadline instead of a tighter one. That is better for a provider whose own
limit on the term is a minimum, and it is a choice rather than an oversight.

**A number moves for one of three reasons and the document never confuses them.** A memory
adjustment cites receipts. A concession cites the counterparty's published limit. A fixed
limit binding cites the rule by name. "A receipt behind every number that moved" would be
false: a constant window floor can lift a proposal with no receipt involved at all.

The constants that decide a term are covered by the same commitment as the term.
`ENGINE_VERSION` is derived from a digest of every one of them rather than typed, so editing
one changes the version, the `engineVersionHash`, and every `policyHash` built from it. The
manifest is published in the document so a reader recomputes the digest instead of trusting
it.

That covers every table as well as every number: which way each limit points, the order the
terms settle in, what each limit is called and whether it moves with memory, how far each side
concedes, which receipts count against which side, and the bounds on all of it. Those hold no
numbers, so they read like structure rather than like constants, and two review rounds found
them outside the digest. Flipping `price_bps` from a ceiling to a floor inverts every price
this build produces and used to leave the version untouched.

**It does not cover the procedure, and the claim stops there deliberately.** The comparison
operators, the gap arithmetic and the integer division live in code that no digest here
fingerprints. Hashing the source instead would make a comment edit invalidate every document
ever written, and it still would not help a reader who cannot run this build.

What helps that reader is the document. Every term publishes its proposal, the limit against
it, and the walk-away the comparison is stated over, so the settlement can be recomputed by
hand from the file. A build whose procedure had drifted would disagree with that arithmetic in
the open rather than in private, which is the property worth having.

### A document to read

[`docs/examples/policy.schema3.json`](docs/examples/policy.schema3.json) is a real quote at
schema 3, produced from the two receipts already on Base Sepolia. It carries all three
profiles: two that agree and one that refuses, naming the term and the distance.

It is tracked rather than generated, so a fresh checkout can check it. `policy.json` at the
repository root is ignored, because it is operational output that changes on every run, and an
artifact a release commit does not contain is not something that release can freeze.

It is judged against a supplied reference time rather than a live block, which is why it is
labelled `executable: false`. That is honest rather than weaker: a live quote stops being
executable minutes after it is written, and no tracked file can keep that claim. The settled
terms are identical either way, because the settlement does not depend on a clock.

Six tests hold it to the build. It must be tracked, checked through git rather than by looking
on disk. Its `engine_version` must be this build's and its published manifest must recompute to
the digest inside that version. Both agreed profiles must load through the production validator
at the terms it prints, and the refusal must stay structurally unsignable. Its persona
commitment must equal the hash of the tracked persona file. And the whole document is pinned by
a digest over its canonical bytes, so a field that explains rather than binds cannot drift
either. Change a constant and those fail until the sample is regenerated.

**What it proves, and what it does not.** It proves the schema, the published arithmetic, the
commitment consistency and this example's outcomes. It does not prove its own history. The risk
values came from the two receipts on Base, and a third party cannot derive that from this
repository alone, because the learned dimensions and both memory stores are deliberately not
part of the artifact. Reproducing the numbers needs the persona, the receipts, the operator
baselines and those dimensions, which is the same list the reproducibility note above gives.

### Two memories, one process, and how to check that yourself

The claim is that each side's numbers come only from that side's memory. It is worth stating
what that does and does not mean, because the easy overstatement is one this project does not
make.

The settlement runs in one process. `_bilateral_quote` opens both stores under one lock,
because comparing two sequential snapshots cannot establish a common one, and a receipt landing
between two reads would leave both sides holding sets that never coexisted. Inside that lock the
two are kept apart: the buyer recalls the provider from the buyer's store, the provider recalls
the buyer from its own, each side's learned dimensions are loaded from its own store, and each
side's terms are produced from its own evidence and its own dimensions. Nothing shares a
scoring input. What the two do share is a set of public Base receipts, which is not the same as
sharing a database.

So the honest phrasing is **two independently held memories on one host**, never two independent
parties, and the page and this file both keep to it. There is no network between them and
nothing in the demo pretends otherwise.

You can check the separation instead of taking it on trust. `agent-positions` publishes one
side's numbers from a process that opens exactly one store:

```bash
BASE="--base-price-wei 100000000000000 --base-bond-bps 500 --service-window 600 --payout-delay 1800"
uv run wrasse agent-positions --role buyer    $BASE
uv run wrasse agent-positions --role provider $BASE
```

Pass the same four baselines to both, because a proposal is a function of the baseline as well
as the memory, and two sides quoted against different baselines settle to nothing meaningful.
Those are the numbers the page starts with.

Each prints that side's proposals, that side's published limits, and the ids of the receipts it
holds; the buyer's are given per profile, since the profile is a buyer-side choice. On the two
receipts currently on Base the urgent pair reads:

| | buyer publishes | provider publishes |
|---|---|---|
| proposes | bond 3500 bps, window 300s | price 11800 bps, delay 893s |
| limits | pay at most 12000 bps, delay at least 900s | post at most 2480 bps, needs at least 300s |

Settle those eight numbers by hand with the rules in the table above: bond `min(3500, 2480)`,
window `max(300, 300)`, price `min(11800, 12000)`, delay `max(893, 900)`. That gives bond 2480,
window 300s, price 11800 bps which is 118000000000000 wei, and delay 900s, which is exactly what
the live quote returns for `urgent`. The receipt ids are published for the same reason: a gap between what
the two sides hold becomes an exchanged fact rather than an assertion one of them makes about
itself. Neither side publishes its ontology, because a provider reading the buyer's dimensions
would not be an independently held memory.

## Sending transactions

Four commands sit between a quote and a settled deal.

```bash
uv run wrasse deploy-check [--require-fresh]
WRASSE_ALLOW_BROADCAST=1 uv run wrasse create-deal --policy policy.json --profile urgent
uv run wrasse tx-status
uv run wrasse tx-resolve [--rebroadcast]
```

`deploy-check` proves the address in `.env` is this build. Identity is the hash of the
deployed runtime bytecode compared against the compiled artifact and the record in
`deployments/base-sepolia.json`, because matching constants can be imitated by different
bytecode. It also runs the canonical policy fixture through the deployed contract, which
moves the cross-language check from a test to the address the demo will use.

`create-deal` refuses to send value unless the code at the configured address hashes to
the artifact this build compiled and to the recorded deployment. `deploy-check` reports the
same comparison, but reporting is not enough on the path that moves money: a contract can
implement one matching pure function and still make `createDeal` do something else.

It also **derives both sides' terms again, from the two memories, and refuses a document it
cannot reproduce**, with both memories held from that check until the signed bytes exist. `policy.json` is checked against itself first, and everything in it agrees
with everything else by construction: the displayed price matches the preimage, the preimage
hashes to the quoted commitment, and that commitment is what the deployed contract would
compute. None of that says where the numbers came from. The commitment is a public unkeyed
hash of the document's own fields, so an edit applied consistently and re-hashed produces a
file in which nothing disagrees with anything, funding a price no memory ever produced.
Consistency is not provenance. The quote is therefore rebuilt from the stores at signing time
and the document is accepted only if this machine reaches the same numbers, recalled evidence
included. The one part that cannot be rebuilt is `executability`, which records what the
quoting run observed rather than anything memory holds: that is checked against the chain
instead, by reading the block it names and comparing the timestamp, so a fixture cannot be
relabelled as a live quote.

The baselines it rebuilds against are its own arguments rather than fields of the document,
because a baseline read out of the file would be one more number an editor gets to choose. So
**a non-default baseline has to be passed to both commands**, or set once in `.env` as
`WRASSE_BASE_PRICE_WEI`, `WRASSE_BASE_BOND_BPS`, `WRASSE_SERVICE_WINDOW` and
`WRASSE_PAYOUT_DELAY`, where both read it:

```bash
uv run wrasse policy <provider> --buyer <buyer> --accept-window 600 \
    --service-window 600 --payout-delay 60 --output policy.json
WRASSE_ALLOW_BROADCAST=1 uv run wrasse create-deal --policy policy.json --profile urgent \
    --service-window 600 --payout-delay 60
```

It looks up the quote's stable identity first, before reading the chain or touching the
keystore. `policy.json` carries a `request_id` minted before any transaction
exists, and the execution identity is that id paired with the explicitly chosen profile.
Nothing about it depends on the terms, which is what makes a retry a retry: the acceptance
deadline is re-derived from chain time immediately before signing, so the committed hash
differs from the quoted one on every attempt. The output names both and says which field
moved.

**Broadcasting requires `WRASSE_ALLOW_BROADCAST=1` on the command that sends.** The barrier
is at the send rather than the signature, because a signed transaction already moves funds
without the key being decrypted again. The same gate covers resending identical bytes. Never
set it in `.env`.

`tx-status` is read-only. It may query the chain, but it never writes a row and never sends.
`tx-resolve` owns every persisted transition, and `--rebroadcast` is the only path that can
resend, and only ever the identical recorded bytes.

The document is validated as untrusted input, because between being written and being
signed against it is a file anything can edit. The half a person reads and the half a
signature commits to are separate objects in it, so the displayed price, bond and window are
each checked against the committed ones. A file that showed a small price beside a commitment
funding a large one would be an explainable receipt for a deal that never happened.

Settlement records live in `.wrasse/transactions.db`, deliberately separate from the Sibyl
memory store. A row is a claim about some signed bytes; the bytes are the authority, so every
load decodes them and checks the sender, chain, nonce, destination, calldata, value, fees,
the acceptance deadline and every committed term against the row before anything acts on it.

### Nonces, and the one that comes back

A wallet holds one transaction at a time. Anything unresolved holds it, including a
transaction we cannot account for, because skipping a nonce strands every later transaction
behind the gap.

There is one exception. A node that refuses a transaction during pre-validation, for
insufficient funds or too little intrinsic gas, never admitted it anywhere. Nothing can mine
at that nonce, so it is released and offered again. That only applies to a first attempt: once
bytes have been accepted somewhere, a later refusal proves nothing and the nonce is held.

Reads are retried a bounded number of times with jitter, honouring a server that names its own
delay. A broadcast never is. Uncertainty after sending goes to the resolver, which can only
resend the identical bytes.

### Inclusion, confirmation and what `safe` means

A receipt means a transaction was included in a block. It does not mean that block is
permanent. A row only reaches `confirmed_success` or `confirmed_reverted` when its recorded
block hash is still canonical at that height and the block is at or below the chain's `safe`
head. On Base, `safe` is the point past which a reorg would require a fault on the underlying
L1, which is a much stronger statement than inclusion and a weaker one than finality. If a
node cannot report a safe head at all, confirmation fails closed rather than quietly falling
back to the latest block.

Local chains have no meaningful safe head. Anvil pins it at block zero forever, so the
rehearsal passes `--local-confirmation-blocks` to count blocks instead. That is a rehearsal
affordance, not a finality claim.

A printed warning would not be a boundary, and the chain id cannot be one either, because the
rehearsal deliberately runs on the production chain id so that the chain-id checks are
exercised. The option therefore requires two independent facts that a real network cannot
satisfy: the endpoint is loopback, and the node identifies itself as local development
software. It is named in every result it produces.

### The deliberate crash

`WRASSE_FAILPOINT=crash-after-send-i-mean-it` makes the process exit after a broadcast
returns and before the ledger records that it went. That is the window a second funded offer
used to appear in. It is read before any `.env` file is loaded, so a value left in a dotfile
cannot arm it, and arming it prints a warning. A fresh process must then resolve the same
intent without rebuilding or re-signing anything.

Copy `.env.example` to the gitignored `.env` and add a project-specific
`OPENROUTER_API_KEY`. `WRASSE_LLM_MODEL` is configurable and defaults to
`openai/gpt-oss-20b`.

## Base contract

`WrasseEscrow` derives `policyHash` onchain. The preimage is exactly this
tuple, in this order, ABI-encoded and hashed:

```text
address buyer                 address provider
uint256 price                 uint256 providerBondBps
uint64  acceptBy              uint64  serviceWindow
uint64  payoutDelay
bytes32 engineVersionHash
bytes32 buyerEvidenceHash     bytes32 providerEvidenceHash
```

Every contract-enforced deal parameter is included, alongside the engine-version
and evidence commitments, which the contract fixes but does not interpret. Every
field is present in the creation receipt: `DealCreated` carries the economics and `DealCommitment`
carries the memory half, both emitted in the same transaction and correlated by
deal id and by the commitment itself. `providerBondBps` is emitted alongside the
rounded absolute bond because integer division makes the rate unrecoverable from
the amount. A test rebuilds the commitment using only values decoded from those
two logs.

`buyerEvidenceHash` commits to the receipts the buyer recalled about the
provider; `providerEvidenceHash` commits to the reverse. Both are opaque. The
contract fixes what each side committed to and proves neither side changed it
afterwards. It does not prove either side assigned a truthful set to its role.

The Python and Solidity suites share an exact fixture to prevent encoding drift.
They also share one file of policy vectors: Solidity runs each through
`createDeal` and Python runs the same through `validate_creatable`, so the claim
that the producer mirrors the contract is tested rather than asserted.

The provider asserts delivery before the deadline. The buyer may release early.
Otherwise the provider may claim payment after the payout delay elapses.
Dispute arbitration is outside the scope of this MVP. `payoutDelay` is not
presented as a review or dispute period.

Settling a deal assigns the proceeds; it never sends them. Each party then calls
`withdraw` to collect, nominating any destination it likes. No state transition
makes an external call, so a participant that refuses ETH can strand its own
credit and nothing belonging to the other side.

See `docs/contract-review.md` for the pre-deployment review and known
limitations.

## Development wallets

Wrasse uses dedicated encrypted Foundry keystores. Their public addresses are
listed in `.env.example`; the keystores and their generated password file live
under the gitignored `.wrasse/` directory. No private key belongs in `.env`.

The Sepolia deployer/buyer address is:

```text
0x30C95B7eb3E08F83992E803Be2A5AB0E0af93d22
```

It must receive Base Sepolia ETH before deployment. Only public addresses are
committed; private keys and the generated keystore password never are.

After funding, deploy with the encrypted keystore:

```bash
cd contracts
forge script script/Deploy.s.sol:DeployWrasseEscrow \
  --rpc-url https://sepolia.base.org \
  --broadcast \
  --keystore ../.wrasse/keystores/wrasse-deployer \
  --password-file ../.wrasse/keystore.password
```

Record the resulting contract address in the gitignored `.env`, then verify the
exact deployed commit on Basescan/Sourcify before any demo transaction.

## Prior Work

All code in this repository was written between 2026-09-03 and 2026-09-10,
inside the hackathon build window. No prior work is reused.

## License

MIT

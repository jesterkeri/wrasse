# Wrasse

Wrasse is a hackathon prototype in which agents remember how a counterparty
behaved, use that evidence to produce the terms of the next negotiation, execute
accepted terms on Base, and write the verified outcome back to Sibyl Memory.

> Memory changes real economic behaviour across sessions, and the outcome comes
> back as verified evidence.

## Why memory is load-bearing

The policy path reads verified counterparty evidence from Sibyl in
`wrasse/memory_gate.py`. If Sibyl raises an error, Wrasse raises
`MemoryRequired` and produces no terms. A reachable store with no matching
counterparty is an explicit cold start. The distinction between `EMPTY_STORE`
and `NO_MATCH` uses Sibyl's exported `refine_zero` verdict step.

Verified Base outcomes are written in `wrasse/evidence.py`, after the receipt,
chain, contract, event signature, deal, provider, and final state are checked in
`wrasse/reconciler.py`. The WARM `chain_event/<event_id>` entity is the
canonical idempotency record; its event ID is also journaled as COLD history.

Remove or break the memory calls and the next deal cannot be priced. That is the
critical path, not an activity log attached after the decision.

## Current scope

- One service type and three profiles: urgent, budget, and sensitive.
- Three seeded simulated providers and exactly one counteroffer.
- One escrow contract on Base Sepolia.
- One constrained OpenRouter call when a new behavioural dimension is first
  created. Stored dimensions are reused; the LLM is never called in the policy
  path.

The provider agents are simulations. Their seed is retained with every bid.

## Setup and tests

```bash
uv sync --extra dev
uv run pytest
cd contracts
forge test -vv
```

The terminal interface currently exposes:

```bash
uv run wrasse recall <provider-address>
uv run wrasse reconcile-timeout <tx-hash> --deal-id <id> --provider <address>
uv run wrasse learn-dimension <event-id>
uv run wrasse policy <provider-address> \
  --buyer <buyer-address> \
  --accept-window 3600 \
  --output policy.json
```

`--buyer` is required because the contract commits to it. The acceptance
deadline is given either way round, and the choice decides what the run is:

- `--accept-window <seconds>` reads the latest Base block and derives the
  absolute deadline from it. This is a live quote.
- `--accept-by <unix-deadline>` with `--reference-timestamp <unix-time>` judges
  the terms against a supplied time instead of reading the chain. The result is
  reproducible and is labelled **not executable**, because a supplied time is
  not the time Base will enforce.

Every deadline is enforced by the block that mines the transaction, so a live
quote is checked against observed chain time plus an inclusion margin
(`--inclusion-margin`, 120 seconds by default) and refuses a deadline too close
to survive being mined. The local clock is never the authority. It appears only
as a bound on how far the observed block may be from now, which can refuse a
reading but never approve one. Each `policy.json` carries an `executability`
block stating which basis was used and what it is worth.

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

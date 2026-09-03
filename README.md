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
uv run wrasse policy <provider-address> --output policy.json
```

Copy `.env.example` to the gitignored `.env` and add a project-specific
`OPENROUTER_API_KEY`. `WRASSE_LLM_MODEL` is configurable and defaults to
`openai/gpt-oss-20b`.

## Base contract

`WrasseEscrow` derives `policyHash` onchain from the provider, actual
`msg.value`, bond basis points, service window, payout delay, engine-version
hash, and evidence hash. The Python and Solidity suites share an exact fixture
to prevent encoding drift.

The provider asserts delivery before the deadline. The buyer may release early.
Otherwise the provider may claim payment after the payout delay elapses.
Dispute arbitration is outside the scope of this MVP. `payoutDelay` is not
presented as a review or dispute period.

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

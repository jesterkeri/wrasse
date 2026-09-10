---
owner: joshua
last_verified: 2026-09-06
verified_by: code read at d364896
hop_ids: []
---

# 1. Registry

Tier: **T0** the demo cannot proceed, **T1** degraded, **T2** cosmetic.

## Components

| Name | Kind | Type | Tier | Depends on | Notes |
|---|---|---|---|---|---|
| `cli` | component | command surface | T0 | every module | 9 commands + 6 deal actions |
| `negotiation` | component | pure logic | T0 | `constants` | Settlement rule. Nine review rounds, closed. |
| `engine` | component | pure logic | T0 | `dimensions`, `constants` | Produces terms from evidence |
| `store` | component | persistence | T0 | Sibyl memory client | Two identified stores, `fcntl` + `RLock` |
| `evidence` | component | ingest | T0 | `store` | Canonical ids, pending marker |
| `reconciler` | component | correlation | T0 | `store`, `chain` | Five-way check before any write |
| `chain` | component | orchestrator | T0 | RPC, ledger | **Signs transactions.** The least review-clean layer. |
| `escrow` | component | contract binding | T0 | `chain` | ABI and calldata |
| `policy_document` | component | display | T1 | `engine` | Document assembly |
| `policy_hash` | component | commitment | T0 | `constants` | Preimage hashing |
| `dimensions` | component | model boundary | T0 | OpenRouter | The only LLM call |
| `chain_time` | component | clock | T1 | RPC | Block timestamp observation |
| `memory_gate` | component | guard | T0 | `store` | Refuses quoting on an outstanding marker |

## Resources

| Name | Kind | Tier | Location | Backed up |
|---|---|---|---|---|
| `buyer-memory.db` | resource | **T0** | `.wrasse/` | Yes, private repo, SHA-256 verified |
| `provider-memory.db` | resource | **T0** | `.wrasse/` | Yes, same |
| `transactions.db` | resource | T0 | `.wrasse/` | **No.** Settlement state only. |
| `keystores/` | resource | T0 | `.wrasse/` | **No, and must not be.** |
| `keystore.password` | resource | T0 | `.wrasse/` | **No, and must not be.** |

## Contracts

| Name | Network | Address | Immutable |
|---|---|---|---|
| `WrasseEscrow` | Base Sepolia, 84532 | `0x5525653f05990DA1479578893b5a624183AFa22E` | Yes. No proxy, no pause, no upgrade. |

Deployed at block 46385173, commit `7e37c8e`, solc 0.8.30, optimizer on, 200 runs.
Runtime bytecode hash `0xbb318c30…62c45608` is checked at runtime before every send.

## External dependencies

| Name | Kind | Tier | Failure effect | Degraded mode | Substitute |
|---|---|---|---|---|---|
| Base Sepolia RPC (primary) | external | **T0** | No send, no resolve, no reconcile | Quoting still works with `--reference-timestamp` | Fallback RPC |
| Base Sepolia RPC (fallback) | external | **T0 for recovery** | Nonce abandonment cannot be proven, so a conflicted row never resolves | none | none |
| OpenRouter | external | T1 | `learn-dimension` fails | Existing dimensions keep working | none |
| Sibyl memory client | external | T0 | Stores unreadable | none | none |

**The fallback RPC is not optional.** Abandoning a nonce requires two endpoints to agree.
Without `BASE_SEPOLIA_FALLBACK_RPC_URL` set, a `nonce_conflict_pending` row has no exit.
The README does not currently say this.

## Configuration

`WRASSE_ESCROW_ADDRESS`, `WRASSE_BUYER_ADDRESS`, `WRASSE_PROVIDER_A_ADDRESS`,
`WRASSE_KEYSTORE`, `WRASSE_PROVIDER_A_KEYSTORE`, `WRASSE_KEYSTORE_PASSWORD_FILE`,
`WRASSE_TX_DB`, `WRASSE_DEPLOYMENT_RECORD`, `WRASSE_LLM_MODEL`, `WRASSE_PROVIDER_PERSONA`,
`BASE_SEPOLIA_RPC_URL`, `BASE_SEPOLIA_FALLBACK_RPC_URL`, `BASE_SEPOLIA_CHAIN_ID`.

`WRASSE_ALLOW_BROADCAST` is deliberately absent from `.env`. It is set on the single command
that should send. Leaving it on in a file defeats the opt-in.

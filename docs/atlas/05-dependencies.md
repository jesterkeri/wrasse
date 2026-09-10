---
owner: joshua
last_verified: 2026-09-06
verified_by: code read at d364896
hop_ids: []
---

# 5. Dependencies and blast radius

## Single points of failure for a T0 journey

Computed by asking which single loss breaks a demo, not eyeballed.

| SPOF | Breaks | Mitigated |
|---|---|---|
| `buyer-memory.db` or `provider-memory.db` | Everything. Not reconstructible from chain. | **Yes.** Private repo, SHA-256 verified, integrity checked on a re-clone. |
| Primary RPC | J2, J3, J4. J1 survives with `--reference-timestamp`. | Partly. Fallback exists but is only consulted for abandonment. |
| Fallback RPC not configured | J3 recovery from `nonce_conflict_pending` | **No.** Configure it. |
| The one laptop | Everything except the backed-up memories | Partly. Ledger and keystores are not backed up, deliberately for the keystores. |
| `keystore.password` | All signing | **No.** Loss means new addresses, and memory is bound to an address. |
| OpenRouter | J5 only | Existing dimensions keep working. T1. |
| `cli.py:1688` | J3 recovery for every deal action | **No.** Open. |

**On the keystore password.** Losing it is not recoverable and it is worse than it looks:
the memories are bound to the wallet addresses, so a new keystore is a cold start even with the
memories restored. It is not in the backup for good reasons. Know where the other copy is.

**Cook's caveat, which applies here.** A SPOF list is the floor of the analysis, not the
ceiling. Most real failures need several things to go wrong together. The list above is what a
single loss breaks; it is not a claim that nothing else can.

## Blast radius, three top events

**"The demo dies mid-judging."** Threats: a lost receipt on a deal action, an RPC hiccup, a tab
closed mid-action. Preventive controls: the two `accept_by` gates (one fixed, one open), the
opt-in ordering (fixed), `WalletBusy` refusing a second attempt. Recovery controls:
`tx-resolve --rebroadcast` (blocked for deal actions today), a second funded wallet (does not
exist), the frozen payloads on the page (memory-off payload outstanding).
**Human-dependent: all recovery.** There is no automation to fall back on.

**"The memories are lost."** Threats: disk failure, theft, `rm`. Preventive: none on the host.
Recovery: the private backup repo, verified. **This one is now well covered.**

**"A quote misrepresents what memory holds."** Threats: an edited `policy.json`, a stale
document, a relearn between quote and signature. Preventive: rebuild-from-memory at
`create-deal`, both memories held across the whole signing callback, canonical-encoding
comparison, duplicate-key refusal. **This is the best-defended path in the system**, having
taken five review rounds.

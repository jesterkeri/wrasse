---
owner: joshua
last_verified: 2026-09-06
verified_by: code read at d364896
hop_ids: []
---

# 2. Context and containers

```mermaid
flowchart TB
  OP([Operator, the only controller])
  subgraph HOST["One laptop. One OS user. No isolation between the two stores."]
    CLI[wrasse cli]
    BM[(buyer-memory.db)]
    PM[(provider-memory.db)]
    LG[(transactions.db<br/>ledger)]
    KS[/keystores + password/]
  end
  subgraph EXT["External"]
    RPC[Base Sepolia RPC<br/>primary + fallback]
    OR[OpenRouter]
  end
  CH{{WrasseEscrow<br/>0x5525…a22E<br/>immutable}}

  OP -->|every state change| CLI
  CLI --> BM
  CLI --> PM
  CLI --> LG
  CLI -->|decrypt, sign| KS
  CLI -->|read, send| RPC
  CLI -->|learn-dimension only| OR
  RPC --> CH
  CH -.->|events, via reconcile| CLI
```

**Trust boundaries.** There are three that matter, and only one is enforced by anything other
than the code:

| Boundary | Enforced by | Strength |
|---|---|---|
| Operator to chain | The contract, and the keystore password | Real. Cryptographic. |
| Wrasse to RPC | Nothing. RPC is assumed unreliable, not assumed honest. | The design handles a slow or absent node, and trusts a single node on the safe head. |
| Buyer memory to provider memory | **Code only.** Same host, same user, same filesystem. | Protocol-level. See section 5. |

The hosted quote service, when it exists, sits outside this diagram entirely: it reads copies of
the two memory databases and holds no key, no ledger and no write path.

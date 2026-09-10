---
owner: joshua
last_verified: 2026-09-06
verified_by: manual; nothing here is automated
hop_ids: []
---

# 7. Alerts, dashboards, runbooks

**There are no alerts. There is no dashboard. There is no monitoring.**

Stating that plainly is more useful than a table of things that do not exist. Every failure in
this system is discovered by a human running a command and reading the output.

## What stands in for monitoring

| Question | Command | Frequency |
|---|---|---|
| Is any wallet held? | `wrasse tx-status` | before every action, and after any failure |
| Did the chain settle it? | `wrasse tx-resolve` | after every send |
| Did memory learn it? | `wrasse policy` and check the receipt count rose | after every `reconcile` |
| Is the deployment the reviewed build? | `wrasse deploy-check` | before a demo |

## Runbook index

| Situation | Page | Section |
|---|---|---|
| A wallet will not accept a new action | J3 | 3 |
| Quoting refuses: memories disagree | J1-b | 3 |
| Quoting refuses: no dimension | J1-c, J5 | 3 |
| Quoting refuses: outstanding marker | J4 hop 8 | 3 |
| `create-deal` refuses the document | J2-b | 3 |
| RPC unavailable | J1-d, and `--reference-timestamp` | 3 |
| Memories lost | 6, restore from the private backup repo | 6 |

## The pre-demo checklist

Run in this order, and stop on the first failure:

```
wrasse deploy-check                 # the address holds the reviewed build
wrasse tx-status                    # no wallet is held
wrasse policy ... --reference-timestamp <t> --accept-by <t>   # quoting works with no RPC
echo $BASE_SEPOLIA_FALLBACK_RPC_URL # non-empty, or J3 recovery has no exit
```

**The last one is the one people skip**, and it is the difference between a recoverable mistake
and a dead wallet in front of a judge.

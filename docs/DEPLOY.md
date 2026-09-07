# Deploying the hosted demo

One service, two deployments, and the difference between them is a single variable. The
read-only one quotes and holds no keys. The executing one lets a visitor perform the
settlement on Base Sepolia. `/api/health` reports which one it is, so nobody has to take the
README's word for it.

Railway is the target. It gives a persistent process, a volume and secrets, which is the
minimum this needs. Serverless does not work here and the reason is worth stating once: the
transaction ledger is a single SQLite file whose whole job is to stop two sends taking the same
nonce on the two shared wallets, and a platform that runs concurrent copies with separate
filesystems would give each copy its own ledger and reintroduce the nonce gap the ledger exists
to prevent.

## What the image carries and why

The build compiles the contract as well as installing the package. `_require_deployment_identity`
compares the code at the configured address against the runtime bytecode this build produced,
and refuses to send if they differ. `contracts/out` is gitignored, correctly, because a
committed build output is a claim nobody checked. So the image compiles it in its first stage
and the check keeps meaning what it says.

## Seeding the volume

**The two memory databases are never committed.** They are gitignored, the repository is
public, and a deployment that read them from the repository would be publishing them.

Mount a volume at `/data`, then seed it once from your own machine:

```
railway volume add --mount-path /data
railway run --service wrasse -- sh -c 'mkdir -p /data/source'
railway ssh --service wrasse   # then copy the four files in, or:
```

The reliable route, because it does not depend on an interactive shell:

```
# From the repository root, with the service stopped.
tar -C .wrasse -czf - buyer-memory.db buyer-memory.db-wal \
                      provider-memory.db provider-memory.db-wal \
  | railway run --service wrasse -- sh -c 'mkdir -p /data/source && tar -C /data/source -xzf -'
```

**Copy the write-ahead logs, not just the `.db` files.** SQLite checkpoints on its own
schedule, so a store written a moment ago keeps its newest rows in `<name>-wal`. A copy without
them loses those rows silently and the service answers as a memory that remembers nothing,
which is indistinguishable from a cold start and is the more dangerous of the two because it
looks like a working system. `prepare_working_copies` refuses to serve an empty configured
source for exactly this reason, so the failure is loud rather than quiet, but the fix is to
copy the logs.

**Do not copy `keystores/`, `keystore.password` or `transactions.db`.** The first two are
secrets and belong in Railway's secret store, not on a volume that gets backed up. The third is
the live ledger for the two wallets and must be created fresh by the deployment; carrying a
ledger from another machine would tell the service that nonces are held which are not.

## Variables

Required by both deployments:

| variable | what it is |
|---|---|
| `WRASSE_ESCROW_ADDRESS` | `0x5525653f05990DA1479578893b5a624183AFa22E` |
| `WRASSE_BUYER_ADDRESS` | the buyer wallet, named in every commitment |
| `WRASSE_PROVIDER_A_ADDRESS` | the provider wallet, and the address the persona is bound to |
| `BASE_SEPOLIA_RPC_URL` | quoting works without it, nothing else does |
| `BASE_SEPOLIA_CHAIN_ID` | `84532` |
| `WRASSE_MEMORY_SOURCE_DIR` | `/data/source`, read at startup and copied |
| `WRASSE_BUYER_MEMORY_PATH` | `/data/work/buyer-memory.db` |
| `WRASSE_PROVIDER_MEMORY_PATH` | `/data/work/provider-memory.db` |
| `WRASSE_TX_DB` | `/data/transactions.db`, the ledger, on the volume |
| `WRASSE_SESSION_ROOT` | `/data/sessions` |

Only the executing deployment sets these:

| variable | what it is |
|---|---|
| `WRASSE_ENABLE_EXECUTION` | `1`. Anything else is the read-only deployment. |
| `WRASSE_BUYER_KEYSTORE_JSON` | the buyer's v3 keystore, as a secret, written to a file at boot |
| `WRASSE_PROVIDER_KEYSTORE_JSON` | the provider's, likewise |
| `WRASSE_KEYSTORE_PASSWORD` | the password for both, likewise |

The entrypoint writes those three into `/run/wrasse` at 0600, outside the volume, and unsets
the plaintext password so no subprocess inherits it. It never traces, because a trace would put
a keystore and its password into the platform's log, where they are retained and searchable.

Worth changing before opening the link, all with defaults that are already sane:

| variable | default | what it bounds |
|---|---|---|
| `WRASSE_DEMO_MAX_PRICE_WEI` | `200000000000000` | the largest settled price a single run may move |
| `WRASSE_DEMO_GAS_ALLOWANCE_WEI` | `20000000000000` | what a whole run is assumed to cost in gas |
| `WRASSE_RUNS_PER_SESSION` | `3` | how many settlements one visitor may perform |
| `WRASSE_TOTAL_RUN_CEILING` | `400` | how many this deployment will perform at all |
| `WRASSE_DEMO_ACCEPT_WINDOW` | `1800` | acceptance time a hosted run quotes |
| `WRASSE_SESSION_LIMIT` | `200` | session copies kept before the oldest is deleted |

## Why the price ceiling exists

Both wallets are faucet-funded and the page is public. A run moves the settled price from the
buyer into escrow and then to the provider, so at the demo's original baseline of 0.0001 ETH the
buyer wallet funds only single-digit runs before it is empty. The ceiling bounds the settled
price rather than the baseline, because the settled price is what actually leaves the wallet,
and a quote above it is refused before anything is signed.

The ceiling is sized against the demo's own front page: the page opens at a baseline of
0.0001 ETH, the urgent profile settles that at 0.000118 ETH, and a ceiling below that would
refuse every judge who pressed the button without changing anything. It guards against a
visitor typing a large baseline. It does not make a nearly empty wallet safe, and nothing does
except funding it.

**Fund both wallets before opening the link.** At the demo baseline a run moves 0.000118 ETH
out of the buyer, so a wallet holding 0.00089 ETH funds about seven runs, which is not enough
for asynchronous judging. `faucets.chain.link/base-sepolia` drips 0.5 ETH and works for these
wallets, which is several thousand runs. The service refuses to start a run neither wallet can
finish and says so in a sentence, rather than creating a deal and failing on the third
transaction, but that is a good error message and not a substitute for the faucet.

Check the two balances before opening the link:

```
cast balance $WRASSE_BUYER_ADDRESS --rpc-url $BASE_SEPOLIA_RPC_URL
cast balance $WRASSE_PROVIDER_A_ADDRESS --rpc-url $BASE_SEPOLIA_RPC_URL
```

Gas dominates the cost per run at Base Sepolia's fees, not the price, so the ceiling is about
bounding a single visitor rather than about the total.

## What a visitor experiences, and how long it takes

One worker, one run at a time. Serialising is correct for two shared wallets rather than a
limitation to engineer around, and the page reports queue position so a wait is legible.

| step | waits for | roughly |
|---|---|---|
| create, accept, deliver, release | inclusion | 10s each |
| confirm at the safe head | Base Sepolia's safe head | 66 to 90s |
| whole run | | under 2 minutes |

So the third visitor in a queue waits several minutes. The quote surface is unqueued and
instant, so the memory argument lands even while the chain is busy.

## After deploying

```
curl -s https://<the service>/api/health
```

`signs`, `holds_keys` and `execution_enabled` must agree with which deployment you meant to
create. `contract_address` must be the escrow above. `engine_version` carries the digest of
every constant that can move a term, so it changing means a term rule changed.

Then quote once, and execute once, before giving anybody the link.

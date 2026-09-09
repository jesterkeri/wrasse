# A Vercel address for a service Vercel cannot run

Wrasse needs a process that stays alive, a volume, and exactly one worker. The transaction
ledger is a single SQLite file whose whole job is to stop two sends taking the same nonce on the
two shared wallets, and a platform that runs concurrent copies with separate filesystems gives
each copy its own ledger and reintroduces the gap the ledger exists to prevent. So the service
runs on Railway and that is not a preference.

What this directory is for is the address. Everything here is a rewrite: Vercel answers the
name and forwards every path, including `/api/*`, to the Railway origin. No compute, no state,
no second copy of anything. If the Railway service is down this address is down with it, which
is the correct behaviour, because there is nothing else serving.

Every request the page makes is short. The long waits are the page polling every two seconds,
not one request being held open, so nothing here runs into a proxy's response limit.

## Deploying it

```
vercel login
cd deploy/vercel
vercel --prod
```

Vercel will ask what to link it to; a new project named `wrasse` is right. There is nothing to
build, so accept the defaults for the build and output settings.

## When the Railway address changes

The origin is written in `vercel.json` in one place. Change it and redeploy.

## What this address is not

It is not a second deployment and it holds no keys. `/api/health` answers through it exactly as
it answers directly, because it is the same process replying, so anything that page reports is
still true of the service that actually signs.

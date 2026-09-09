"""The whole visitor path, end to end, against a running service and the live chain.

Not a unit test and deliberately not in `tests/`. Everything here needs a service that is
already up, and the second half spends real Base Sepolia ETH from the two shared wallets, so it
must never be something `pytest` collects and runs by accident.

    uv run python scripts/live-demo-check.py            # everything, about twelve minutes
    E2E_SKIP_CHAIN=1 uv run python scripts/live-demo-check.py   # the free half, seconds

What it is for: the suite proves each piece in isolation against injected collaborators, which
is the only way to test the orderings, and that is exactly why it cannot notice a service whose
quote and whose executor disagree, a page that no longer loads, or a settlement that Base
refuses. This walks the surfaces a judge actually touches, in the order they touch them.

Two of its assertions were wrong the first time it ran, and both are worth keeping in mind
before adding another: a run reports the same transaction hash from three different steps, and
two failures need three kept promises to pay down rather than two. An assertion written from
what the design says happens is worth less than one written from what the service returns.
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("WRASSE_BASE_URL", "http://127.0.0.1:8099")
FAILS = []

def call(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw

def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)
    return ok

# ---------------------------------------------------------------- 1. the page and health
status, _ = call("GET", "/")
check("the page is served", status == 200)

status, health = call("GET", "/api/health")
check("health answers", status == 200 and health.get("ok") is True)
check("health admits it signs", health.get("signs") is True and health.get("holds_keys") is True)
check("health says quoting writes nothing",
      health.get("quote_writes_receipts_or_outcomes") is False)
ENGINE = health.get("engine_version")
print(f"      engine {ENGINE}  contract {health.get('contract_address')}")

# ---------------------------------------------------------------- 2. the quote, warm and cold
status, warm = call("GET", "/api/quote?memory=on")
check("warm quote", status == 200)
status, cold = call("GET", "/api/quote?memory=off")
check("cold quote", status == 200)

def settled(doc, profile):
    for p in doc["profiles"]:
        if p["id"] == profile:
            return p.get("terms")
    raise AssertionError(f"no profile {profile}")

w, c = settled(warm, "urgent"), settled(cold, "urgent")
check("memory moves the urgent price", w and c and w["price_wei"] != c["price_wei"],
      f"warm {w['price_wei']} vs cold {c['price_wei']}" if w and c else "")
check("budget refuses warm and agrees cold",
      settled(warm, "budget") is None and settled(cold, "budget") is not None)

# ---------------------------------------------------------------- 3. the simulator
status, none_yet = call("POST", "/api/simulate", {"history": []})
check("simulate with no history", status == 200)
# Two different grievances, and they must not move the same numbers. A seller that never
# delivered is the buyer's complaint; a buyer that made the seller wait is the seller's.
SELLER_FAILED = "timeout_claimed_without_delivery"
BUYER_FAILED = "delivered_and_claimed_after_delay"
KEPT = "delivered_and_released_by_buyer"

status, seller_bad = call("POST", "/api/simulate", {"history": [SELLER_FAILED] * 2})
check("simulate with two seller failures", status == 200)
status, two_bad = call("POST", "/api/simulate", {"history": [BUYER_FAILED] * 2})
check("simulate with two buyer failures", status == 200)
# Three, not two. Two failures take the provider's risk to 1.0000 and each kept promise pays
# down a fixed amount, so the number of good deals it takes to come back is a fact about the
# history rather than a constant. Asserting two would have been asserting the wrong story.
status, recovered = call("POST", "/api/simulate",
                         {"history": [BUYER_FAILED, BUYER_FAILED] + [KEPT] * 3})
check("simulate after recovery", status == 200)

# Nothing is rewritten, so the way back is monotone: each kept promise may only lower the
# risk, never raise it, and the two failures are still in the history at the end of it.
curve = []
for kept in range(0, 5):
    _, doc = call("POST", "/api/simulate",
                  {"history": [BUYER_FAILED, BUYER_FAILED] + [KEPT] * kept})
    curve.append(float(doc["memories"]["provider"]["risk"]))
check("keeping a promise never raises the risk it is paying down",
      all(b <= a for a, b in zip(curve, curve[1:])), str(curve))
check("the failures are still there after recovery",
      len(recovered["memories"]["provider"]["receipts"]) == 5)

# The asymmetry is the whole claim: each side's memory moves only the terms it owns.
base = settled(none_yet, "urgent")
seller = settled(seller_bad, "urgent")
buyer = settled(two_bad, "urgent")
check("a seller that never delivered raises the stake it must post",
      seller["provider_bond_bps"] > base["provider_bond_bps"],
      f"{base['provider_bond_bps']} to {seller['provider_bond_bps']}")
check("and does not raise the price the buyer pays",
      seller["price_wei"] == base["price_wei"])
check("a buyer that made the seller wait raises the price",
      buyer["price_wei"] > base["price_wei"],
      f"{base['price_wei']} to {buyer['price_wei']}")
check("and does not raise the stake the seller posts",
      buyer["provider_bond_bps"] == base["provider_bond_bps"])

def budget_agrees(doc):
    return settled(doc, "budget") is not None

check("two buyer failures make budget refuse", not budget_agrees(two_bad))
check("kept promises bring budget back", budget_agrees(recovered))
check("simulate is stateless: the empty history still agrees", budget_agrees(none_yet))

status, bad = call("POST", "/api/simulate", {"history": ["not_an_outcome"]})
check("an unlearned outcome is refused by name, not scored zero", status == 422)

# ---------------------------------------------------------------- 4. a real settlement
if os.environ.get("E2E_SKIP_CHAIN") == "1":
    print()
    print(f"{len(FAILS)} failed" if FAILS else "all passed (the chain half was skipped)")
    for f in FAILS:
        print("  -", f)
    sys.exit(1 if FAILS else 0)

status, session = call("POST", "/api/session")
check("a session opens", status == 200 and "session_id" in session)
SID = session["session_id"]
print(f"      session {SID}  {session['runs_left']} runs")

def run_to_completion(profile, outcome, label):
    status, started = call("POST", "/api/execute",
                           {"session_id": SID, "profile": profile, "outcome": outcome})
    if not check(f"{label}: queued", status == 200 and "run_id" in started, str(started)[:200]):
        return None
    rid = started["run_id"]
    began = time.time()
    seen, last = [], None
    while time.time() - began < 900:
        time.sleep(5)
        s, body = call("GET", f"/api/run/{rid}")
        if s != 200:
            continue
        for step in body.get("steps", []):
            key = (step["name"], step["status"])
            if key not in seen:
                seen.append(key)
                if step["status"] != "running":
                    print(f"        {step['name']:9s} {step['status']:9s} "
                          f"{step.get('tx_hash') or ''}")
        last = body
        if body["status"] in ("succeeded", "failed"):
            break
    took = int(time.time() - began)
    check(f"{label}: settled", last and last["status"] == "succeeded",
          f"{took}s  {last and last.get('error') or ''}")
    return last

first = run_to_completion("urgent", "released", "run 1 released")
if first:
    # Distinct, because confirm and teach both report the transaction they are reading rather
    # than one of their own. Counting steps that carry a hash counts the same send three times.
    hashes = {s.get("tx_hash") for s in first["steps"] if s.get("tx_hash")}
    check("run 1 produced four chain transactions", len(hashes) == 4, str(sorted(hashes)))
    check("run 1 taught both memories",
          any(s["name"] == "teach" and s["status"] == "done" for s in first["steps"]))

# the history the visitor just built must change the next quote in this session
status, after = call("POST", "/api/quote", {"session_id": SID})
check("the session's own quote sees the new receipt", status == 200)
if status == 200 and w:
    a = settled(after, "urgent")
    check("a kept promise softened the urgent price",
          a and a["price_wei"] <= w["price_wei"],
          f"was {w['price_wei']} now {a and a['price_wei']}")

second = run_to_completion("urgent", "timeout", "run 2 timeout")
if second:
    check("run 2 recorded a failure",
          any(s["name"] == "claim" and s["status"] == "done" for s in second["steps"]))

# This exercises the manual Finish button, not the automatic collection the worker does when a
# session spends its last run. Those are different mechanisms and this cannot tell them apart:
# it presses Finish itself, so removing the worker's own trigger would not make it fail. The
# automatic path is covered in tests/test_service.py, and covering it here would mean five real
# settlements and twenty minutes of chain time.
#
# Finishing is not finished. The withdrawal has been queued and nothing has been collected
# yet, and the distinction is what makes a failed refund retryable rather than permanent.
status, done = call("POST", "/api/finish", {"session_id": SID})
check("the session starts finishing",
      status == 200 and done.get("finishing") is True and done.get("refunded") is False,
      str(done)[:200])

status, again = call("POST", "/api/finish", {"session_id": SID})
check("a second press is the same refund, not a second withdrawal",
      status == 200 and again.get("run_id") == done.get("run_id"), str(again)[:200])

collected = None
began = time.time()
while time.time() - began < 600:
    time.sleep(5)
    s, body = call("GET", f"/api/run/{done['run_id']}")
    if s == 200 and body["status"] in ("succeeded", "failed"):
        collected = body
        break
check("the escrow is emptied back into the wallets",
      collected and collected["status"] == "succeeded",
      collected and (collected.get("error") or
                     f"{collected.get('refund_wei')} wei to the {collected.get('refund_to')}"))
if collected:
    check("and the session is only marked refunded once it has been",
          collected["session"]["refunded"] is True
          and collected["session"]["finishing"] is False)

status, gone = call("POST", "/api/execute",
                    {"session_id": "0" * 32, "profile": "urgent"})
check("an unknown session is refused", status == 404)

print()
print(f"{len(FAILS)} failed" if FAILS else "all passed")
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)

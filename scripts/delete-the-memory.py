"""The hackathon's eligibility test, run against this build rather than asserted about it.

    "Delete the memory layer. If your project still does what it claims, it is a wrapper and
     does not qualify. If the core function breaks, memory is load-bearing."

    uv run python scripts/delete-the-memory.py

The four cases live in `wrasse/prove.py`, which the page's proof button also calls, so the
terminal and the browser cannot drift apart and disagree about whether this project passes.
Every case runs on a throwaway copy that is destroyed afterwards; the real memories are read to
be copied and never written.
"""

from __future__ import annotations

from pathlib import Path

from wrasse import prove as proving

REPO = Path(__file__).resolve().parent.parent
REAL = REPO / ".wrasse"
BUYER = "0x30C95B7eb3E08F83992E803Be2A5AB0E0af93d22"
PROVIDER = "0x0b920573ADf657f45Fecd9f7e48e66B5535A90C0"


def eth(wei: int) -> str:
    digits = str(int(wei)).rjust(19, "0")
    whole, frac = digits[:-18], digits[-18:].rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def main() -> int:
    source = {role: REAL / f"{role}-memory.db" for role in ("buyer", "provider")}
    if not source["buyer"].is_file():
        print(f"no memories at {REAL}. This has to run where the two stores live.")
        return 2

    print(__doc__.strip().splitlines()[0])
    print()

    cases = proving.prove(source, buyer=BUYER, provider=PROVIDER)
    for case in cases:
        mark = "  " if case.passed else "!!"
        print(f"{mark} {case.name:32s} -> {case.outcome}")
        if case.terms:
            print(f"     urgent settles at {eth(case.terms['price_wei'])} ETH, "
                  f"stake {case.terms['provider_bond_bps'] / 100:g}%, "
                  f"deliver within {case.terms['service_window'] // 60} minutes")
        elif case.detail:
            print(f"     {case.detail}")

    failed = [case.name for case in cases if not case.passed]
    print()
    if failed:
        print("THE TEST DOES NOT PASS. These behaved as a wrapper would:")
        for name in failed:
            print("  -", name)
        return 1

    print("Terms are produced only when both memories are readable and every receipt still")
    print("proves its own identity. Take the memory away by any of those routes and this")
    print("project stops doing the thing it claims to do, which is the test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

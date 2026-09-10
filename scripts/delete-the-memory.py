"""The hackathon's eligibility test, run against this build rather than asserted about it.

    "Delete the memory layer. If your project still does what it claims, it is a wrapper and
     does not qualify. If the core function breaks, memory is load-bearing."

So this deletes it, four different ways, and shows what the project does afterwards. It never
touches the real memories: every case works on a throwaway copy under a temporary directory,
and the copy is destroyed when the run ends.

    uv run python scripts/delete-the-memory.py

The bar this is trying to clear is not "the program crashes". Anything crashes. The claim is
narrower and worth stating precisely: **this project refuses to produce terms it cannot
justify**, and it distinguishes a memory it cannot read from a memory that is legitimately
empty. A wrapper cannot tell those apart, because it was never using the memory to decide.

The four cases are the ones an operator can actually meet, and the last two are the interesting
ones. A deleted store is obvious. A store that opens, answers, and hands back a row that has
been edited is the case where a system that was only decorating itself with memory carries on
and prices the deal anyway.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REAL = REPO / ".wrasse"
BUYER = "0x30C95B7eb3E08F83992E803Be2A5AB0E0af93d22"
PROVIDER = "0x0b920573ADf657f45Fecd9f7e48e66B5535A90C0"


def quote(paths: dict[str, Path]) -> tuple[int, str]:
    """Ask for terms with the memories at these paths, and report what came back."""

    env = dict(os.environ)
    env["WRASSE_BUYER_MEMORY_PATH"] = str(paths["buyer"])
    env["WRASSE_PROVIDER_MEMORY_PATH"] = str(paths["provider"])
    env.pop("WRASSE_ALLOW_BROADCAST", None)
    done = subprocess.run(
        # The page's own starting numbers, so the terms here are the terms a reader has
        # already seen rather than the command line's defaults.
        [sys.executable, "-m", "wrasse.cli", "policy", PROVIDER,
         "--buyer", BUYER, "--reference-timestamp", "1788666320",
         "--accept-by", "1788666920", "--base-price-wei", "100000000000000",
         "--base-bond-bps", "500", "--service-window", "600", "--payout-delay", "1800"],
        capture_output=True, text=True, env=env, cwd=REPO, timeout=180,
    )
    return done.returncode, (done.stderr.strip() or done.stdout.strip())


def reason(text: str) -> str:
    """The sentence the refusal gave, without the module path in front of it."""

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or line.startswith(("Traceback", "During handling")) or line.startswith("  "):
            continue
        if ": " in line and line.split(":")[0].replace(".", "").replace("_", "").isalnum():
            line = line.split(": ", 1)[1]
        return line if len(line) <= 220 else line[:217].rsplit(" ", 1)[0] + "..."
    return text[:220]


def terms_of(output: str) -> str:
    """What the urgent profile settled at, so the contrast is on the screen."""

    import json

    try:
        document = json.loads(output)
        terms = document["buyer"]["profiles"]["urgent"]["terms"]
    except Exception:  # noqa: BLE001 - the caller prints the raw output instead
        return ""
    price = str(terms["price_wei"]).rjust(19, "0")
    eth = (price[:-18].lstrip("0") or "0") + "." + price[-18:].rstrip("0")
    return (f"urgent settles at {eth} ETH, stake {terms['provider_bond_bps'] / 100:g}%, "
            f"deliver within {terms['service_window'] // 60} minutes")


def copy_memories(into: Path) -> dict[str, Path]:
    paths = {}
    for role in ("buyer", "provider"):
        target = into / f"{role}-memory.db"
        for suffix in ("", "-wal", "-shm"):
            source = REAL / f"{role}-memory.db{suffix}"
            if source.is_file():
                shutil.copy(source, target.with_name(target.name + suffix))
                target.with_name(target.name + suffix).chmod(0o600)
        paths[role] = target
    return paths


# -- the four ways to take the memory away ------------------------------------------------

def intact(paths):
    """The control. Nothing is broken, so terms come back."""


def deleted(paths):
    """The literal reading of the test: the file is gone."""
    for role in ("buyer", "provider"):
        for suffix in ("", "-wal", "-shm"):
            paths[role].with_name(paths[role].name + suffix).unlink(missing_ok=True)


def unreadable(paths):
    """Present, and not a database. An unreachable Sibyl looks like this from here."""
    paths["buyer"].write_bytes(b"this is not a database")


def edited(paths):
    """The one that separates a memory from a decoration.

    The store opens, answers, and hands back a receipt whose body has been changed. Nothing is
    missing and nothing errors. A project that was only displaying its memory would price this
    deal and never notice.

    The edit is to the transaction hash, because a receipt's identity is
    `keccak(chainId, contract, txHash, logIndex)` and that is the thing recall recomputes. A
    row that no longer produces its own name is not evidence of anything, whatever it says.

    An earlier version of this case edited the deal id instead, and the quote carried on. That
    was correct and the case was wrong: the deal id is not part of the identity and does not
    move a term, so nothing had been broken. It is recorded here rather than quietly swapped,
    because "the check did not fire" and "there was nothing to fire at" look identical from
    the outside and only one of them is a defect.
    """
    connection = sqlite3.connect(paths["buyer"])
    try:
        before = connection.execute(
            "select body from entities where category = 'chain_event'"
        ).fetchall()
        # One character, keeping it a well-formed 32-byte hash. Parsing still succeeds; what
        # fails is the receipt reproducing its own name, which is the check being demonstrated.
        connection.execute(
            "update entities set body = replace(body, '\"tx_hash\":\"0xbe', "
            "'\"tx_hash\":\"0xbf') where category = 'chain_event'"
        )
        connection.commit()
        after = connection.execute(
            "select body from entities where category = 'chain_event'"
        ).fetchall()
        if before == after:
            raise RuntimeError(
                "nothing was edited, so this case proves nothing. The stored shape changed; "
                "rewrite the edit rather than letting it pass."
            )
    finally:
        connection.close()


CASES = [
    ("both memories intact", intact, "terms"),
    ("both memory files deleted", deleted, "refusal"),
    ("a memory that will not open", unreadable, "refusal"),
    ("a receipt edited in place", edited, "refusal"),
]


def main() -> int:
    if not (REAL / "buyer-memory.db").is_file():
        print(f"no memories at {REAL}. This has to run where the two stores live.")
        return 2

    print(__doc__.strip().splitlines()[0])
    print()
    failures = []

    for name, break_it, expected in CASES:
        with tempfile.TemporaryDirectory(prefix="wrasse-deletion-") as tmp:
            paths = copy_memories(Path(tmp))
            break_it(paths)
            code, output = quote(paths)
            got = "terms" if code == 0 else "refusal"
            ok = got == expected
            mark = "  " if ok else "!!"
            print(f"{mark} {name:32s} -> {got}")
            if got == "terms":
                detail = terms_of(output)
                if detail:
                    print(f"     {detail}")
            else:
                print(f"     {reason(output)}")
            if not ok:
                failures.append(name)

    print()
    if failures:
        print("THE TEST DOES NOT PASS. These behaved as a wrapper would:")
        for name in failures:
            print("  -", name)
        return 1

    print("Terms are produced only when both memories are readable and every receipt still")
    print("proves its own identity. Take the memory away by any of those routes and this")
    print("project stops doing the thing it claims to do, which is the test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

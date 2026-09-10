"""Take the memory away and report what this project does, for a caller that is not a terminal.

The hackathon's eligibility test is a claim about behaviour, so it is answered by behaviour:

    "Delete the memory layer. If your project still does what it claims, it is a wrapper and
     does not qualify. If the core function breaks, memory is load-bearing."

`scripts/delete-the-memory.py` and the page's proof button are the same four cases through this
module, so the terminal and the browser cannot drift apart and disagree about whether we pass.

Everything happens on a throwaway copy in a temporary directory, which is removed afterwards.
The real memories are opened read-only, to copy them, and never written.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: What one whole run may take. Four quotes at about four seconds each, with room for a slow
#: machine. A caller that cannot wait this long should not be calling this.
TIMEOUT = float(os.getenv("WRASSE_PROOF_TIMEOUT", "120"))


@dataclass
class Case:
    """One way of taking the memory away, and what came back."""

    name: str
    did: str
    expected: str
    outcome: str = ""
    detail: str = ""
    terms: dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.outcome == self.expected

    def view(self) -> dict:
        return {
            "name": self.name, "did": self.did, "expected": self.expected,
            "outcome": self.outcome, "detail": self.detail,
            "terms": self.terms or None, "passed": self.passed,
        }


def _copy(source: dict[str, Path], into: Path) -> dict[str, Path]:
    paths = {}
    for role, origin in source.items():
        target = into / f"{role}-memory.db"
        for suffix in ("", "-wal", "-shm"):
            sidecar = origin.with_name(origin.name + suffix)
            if sidecar.is_file():
                shutil.copy(sidecar, target.with_name(target.name + suffix))
                target.with_name(target.name + suffix).chmod(0o600)
        paths[role] = target
    return paths


# -- the four ways -------------------------------------------------------------------------

def _intact(paths: dict[str, Path]) -> None:
    """The control. Nothing is broken."""


def _deleted(paths: dict[str, Path]) -> None:
    """The literal reading of the test."""
    for path in paths.values():
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)


def _unreadable(paths: dict[str, Path]) -> None:
    """Present, and not a database. An unreachable memory service looks like this."""
    paths["buyer"].write_bytes(b"this is not a database")


def _edited(paths: dict[str, Path]) -> None:
    """The one that separates a memory from a decoration.

    One character of a transaction hash, so it stays a well-formed 32-byte value. Nothing is
    missing and nothing fails to parse: the store opens, answers, and hands back a receipt that
    no longer reproduces its own name.
    """
    connection = sqlite3.connect(paths["buyer"])
    try:
        # Read the hash out and flip a character of the one that is there, rather than matching
        # a literal. The first version replaced a hardcoded prefix, which meant the case passed
        # only against the two receipts this demo happened to hold: any other store, including
        # the suite's fixture, edited nothing and reported that the shape had changed. A case
        # that silently does nothing on an unfamiliar store is worse than no case at all, since
        # it would have read as a pass on a deployment it never actually tested.
        rows = connection.execute(
            "select rowid, body from entities where category = 'chain_event'"
        ).fetchall()
        if not rows:
            raise RuntimeError(
                "this memory holds no chain events, so there is no receipt to alter and this "
                "case proves nothing."
            )

        rowid, body = rows[0]
        event = json.loads(body)
        original = str(event["tx_hash"])
        # The last character, so it stays a well-formed 32-byte value. Nothing is missing and
        # nothing fails to parse: the store opens, answers, and hands back a receipt that no
        # longer reproduces its own name.
        event["tx_hash"] = original[:-1] + ("0" if original[-1] != "0" else "1")
        if event["tx_hash"] == original:
            raise RuntimeError("the hash did not change, so this case proves nothing.")

        connection.execute(
            "update entities set body = ? where rowid = ?", (json.dumps(event), rowid)
        )
        connection.commit()
    finally:
        connection.close()


CASES = (
    ("both memories intact", "nothing is touched", "terms", _intact),
    ("both memory files deleted", "the two files are removed", "refusal", _deleted),
    ("a memory that will not open", "one file is overwritten with junk", "refusal", _unreadable),
    ("a receipt edited in place", "one character of a transaction hash is changed",
     "refusal", _edited),
)


def _reason(text: str) -> str:
    """The sentence the refusal gave, without the module path in front of it."""

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or line.startswith(("Traceback", "During handling")) or line.startswith("  "):
            continue
        if ": " in line and line.split(":")[0].replace(".", "").replace("_", "").isalnum():
            line = line.split(": ", 1)[1]
        return line if len(line) <= 260 else line[:257].rsplit(" ", 1)[0] + "..."
    return text[:260]


def _quote(paths: dict[str, Path], buyer: str, provider: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["WRASSE_BUYER_MEMORY_PATH"] = str(paths["buyer"])
    env["WRASSE_PROVIDER_MEMORY_PATH"] = str(paths["provider"])
    env.pop("WRASSE_ALLOW_BROADCAST", None)
    done = subprocess.run(
        [sys.executable, "-m", "wrasse.cli", "policy", provider, "--buyer", buyer,
         "--reference-timestamp", "1788666320", "--accept-by", "1788666920",
         "--base-price-wei", "100000000000000", "--base-bond-bps", "500",
         "--service-window", "600", "--payout-delay", "1800"],
        capture_output=True, text=True, env=env, timeout=TIMEOUT,
    )
    return done.returncode, (done.stderr.strip() or done.stdout.strip())


def prove(source: dict[str, Path], *, buyer: str, provider: str) -> list[Case]:
    """Run every case against a throwaway copy of `source` and report what happened."""

    results = []
    for name, did, expected, break_it in CASES:
        case = Case(name=name, did=did, expected=expected)
        with tempfile.TemporaryDirectory(prefix="wrasse-proof-") as tmp:
            paths = _copy(source, Path(tmp))
            break_it(paths)
            code, output = _quote(paths, buyer, provider)
            case.outcome = "terms" if code == 0 else "refusal"
            if case.outcome == "terms":
                try:
                    case.terms = json.loads(output)["buyer"]["profiles"]["urgent"]["terms"]
                except Exception:  # noqa: BLE001 - reported as a missing detail, not a crash
                    case.detail = "terms were produced but could not be read back"
            else:
                case.detail = _reason(output)
        results.append(case)
    return results

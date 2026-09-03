"""Small, scriptable terminal surface for the Rapport demo."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from sibyl_memory_client import MemoryClient

from .memory_gate import recall_counterparty_evidence


def _memory() -> MemoryClient:
    path = Path(os.getenv("RAPPORT_MEMORY_PATH", ".rapport/memory.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return MemoryClient.local(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rapport")
    sub = parser.add_subparsers(dest="command", required=True)
    recall = sub.add_parser("recall", help="recall verified evidence for a provider")
    recall.add_argument("provider")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.command == "recall":
        recalled = recall_counterparty_evidence(_memory(), args.provider)
        print(json.dumps({
            "counterparty": recalled.counterparty,
            "cold_start": recalled.is_cold_start,
            "verdict": recalled.verdict,
            "evidence": list(recalled.evidence),
        }, indent=2, sort_keys=True))
        return 0
    raise AssertionError("unreachable")


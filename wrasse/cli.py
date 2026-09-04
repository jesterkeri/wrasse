"""Small, scriptable terminal surface for the Wrasse demo."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from sibyl_memory_client import MemoryClient
from web3 import HTTPProvider, Web3

from .dimensions import get_or_create_dimension, load_dimensions
from .engine import PROFILES, produce_terms
from .memory_gate import recall_counterparty_evidence
from .policy_hash import (
    EMPTY_EVIDENCE_HASH,
    PolicyPreimage,
    evidence_hash,
    policy_hash,
    validate_creatable,
)
from .reconciler import reconcile_timeout_claim


def _memory() -> MemoryClient:
    path = Path(os.getenv("WRASSE_MEMORY_PATH", ".wrasse/memory.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return MemoryClient.local(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wrasse")
    sub = parser.add_subparsers(dest="command", required=True)
    recall = sub.add_parser("recall", help="recall verified evidence for a provider")
    recall.add_argument("provider")
    learn = sub.add_parser("learn-dimension", help="learn or reuse a dimension for a verified event")
    learn.add_argument("event_id")
    policy = sub.add_parser("policy", help="produce profile-conditioned policy terms")
    policy.add_argument("provider")
    policy.add_argument("--buyer", required=True, help="buyer address; the commitment covers it")
    policy.add_argument("--base-price-wei", type=int, default=10**15)
    policy.add_argument("--base-bond-bps", type=int, default=500)
    policy.add_argument("--service-window", type=int, default=3_600)
    policy.add_argument("--payout-delay", type=int, default=1_800)
    policy.add_argument(
        "--accept-by",
        type=int,
        required=True,
        help=(
            "absolute unix deadline for provider acceptance. Required rather than derived "
            "from the clock: a commitment that moves on every run is not a commitment. "
            "The orchestrator will supply this immediately before signing."
        ),
    )
    policy.add_argument(
        "--reference-timestamp",
        type=int,
        default=None,
        help=(
            "unix time that acceptance is measured against when checking the terms are "
            "executable onchain. Defaults to now. Supply it to make a run reproducible."
        ),
    )
    policy.add_argument("--output", type=Path)
    reconcile = sub.add_parser("reconcile-timeout", help="verify a timeout receipt and persist it")
    reconcile.add_argument("tx_hash")
    reconcile.add_argument("--deal-id", type=int, required=True)
    reconcile.add_argument("--provider", required=True)
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
    if args.command == "learn-dimension":
        memory = _memory()
        event = memory.get_entity("chain_event", args.event_id)["body"]
        api_key = os.environ["OPENROUTER_API_KEY"]
        definition, created = get_or_create_dimension(
            memory,
            event,
            api_key=api_key,
            model=os.getenv("WRASSE_LLM_MODEL", "openai/gpt-oss-20b"),
        )
        print(json.dumps({"created": created, "dimension": definition.body()}, indent=2, sort_keys=True))
        return 0
    if args.command == "policy":
        memory = _memory()
        recalled = recall_counterparty_evidence(memory, args.provider)
        dimensions = load_dimensions(memory)
        missing = sorted({
            event["event_type"]
            for event in recalled.evidence
            if not any(item.source_event_type == event["event_type"] for item in dimensions)
        })
        if missing:
            raise RuntimeError(f"verified events need dimensions before policy generation: {missing}")
        reference_timestamp = (
            args.reference_timestamp if args.reference_timestamp is not None else int(time.time())
        )
        ids = tuple(str(item["event_id"]) for item in recalled.evidence)
        evidence_commitment = evidence_hash(ids)
        profiles = {}
        for name, profile in PROFILES.items():
            terms = produce_terms(
                evidence=recalled.evidence,
                dimensions=dimensions,
                profile=profile,
                base_price_wei=args.base_price_wei,
                base_bond_bps=args.base_bond_bps,
                base_service_window=args.service_window,
            )
            preimage = PolicyPreimage(
                buyer=args.buyer,
                provider=args.provider,
                price=terms.price_wei,
                bond_bps=terms.provider_bond_bps,
                accept_by=args.accept_by,
                service_window=terms.service_window,
                payout_delay=args.payout_delay,
                engine_version="wrasse/0.1.0",
                buyer_evidence_hash=evidence_commitment,
                # The provider recalls nothing about this buyer yet. Bilateral recall
                # lands at gate 6; until then this side is honestly empty rather than
                # borrowing the buyer's own evidence.
                provider_evidence_hash=EMPTY_EVIDENCE_HASH,
            )
            # A quote the chain would refuse is not a quote. Checking here, rather than at
            # broadcast, keeps the displayed policy and the executable policy the same thing.
            validate_creatable(preimage, reference_timestamp=reference_timestamp)
            profiles[name] = {
                "terms": {
                    "price_wei": terms.price_wei,
                    "provider_bond_bps": terms.provider_bond_bps,
                    "service_window": terms.service_window,
                    "risk": str(terms.risk),
                    "evidence_event_ids": list(terms.evidence_event_ids),
                },
                "policy_preimage": preimage.as_dict(),
                "policy_hash": policy_hash(preimage),
            }
        output = {
            "counterparty": recalled.counterparty,
            "memory_verdict": recalled.verdict,
            "cold_start": recalled.is_cold_start,
            "profiles": profiles,
            "evidence": list(recalled.evidence),
        }
        rendered = json.dumps(output, indent=2, sort_keys=True)
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0
    if args.command == "reconcile-timeout":
        contract = os.environ["WRASSE_ESCROW_ADDRESS"]
        chain_id = int(os.getenv("BASE_SEPOLIA_CHAIN_ID", "84532"))
        web3 = Web3(HTTPProvider(os.getenv("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org")))
        result = reconcile_timeout_claim(
            _memory(),
            web3,
            tx_hash=args.tx_hash,
            expected_chain_id=chain_id,
            expected_contract=contract,
            expected_deal_id=args.deal_id,
            expected_provider=args.provider,
        )
        print(json.dumps({"created": result.created, "entity": result.entity}, indent=2, sort_keys=True))
        return 0
    raise AssertionError("unreachable")

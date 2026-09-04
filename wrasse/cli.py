"""Small, scriptable terminal surface for the Wrasse demo."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv
from sibyl_memory_client import MemoryClient
from web3 import HTTPProvider, Web3

from .chain_time import ChainObservation, observe_chain_time, past_lag, require_recent
from .dimensions import get_or_create_dimension, load_dimensions
from .engine import PROFILES, produce_terms
from .memory_gate import recall_counterparty_evidence
from .policy_hash import (
    DEFAULT_INCLUSION_MARGIN_SECONDS,
    EMPTY_EVIDENCE_HASH,
    PolicyPreimage,
    evidence_hash,
    policy_hash,
    require_inclusion_margin,
    validate_creatable,
)
from .reconciler import reconcile_timeout_claim


def _web3() -> Web3:
    return Web3(HTTPProvider(os.getenv("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org")))


def _chain_id() -> int:
    return int(os.getenv("BASE_SEPOLIA_CHAIN_ID", "84532"))


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
    policy.add_argument(
        "--base-price-wei",
        type=int,
        default=10**14,
        help=(
            "starting price in wei before profile adjustment. Defaults to 0.0001 ETH, sized "
            "so a faucet-funded testnet wallet can run the whole loop several times over."
        ),
    )
    policy.add_argument("--base-bond-bps", type=int, default=500)
    policy.add_argument("--service-window", type=int, default=3_600)
    policy.add_argument("--payout-delay", type=int, default=1_800)
    policy.add_argument(
        "--accept-by",
        type=int,
        default=None,
        help=(
            "absolute unix deadline for provider acceptance. Fixed rather than derived from "
            "a clock at hash time: a commitment that moves on every run is not a commitment."
        ),
    )
    policy.add_argument(
        "--accept-window",
        type=int,
        default=None,
        help=(
            "seconds of acceptance time, converted to an absolute deadline using the "
            "observed chain time. Live quotes only; there is no chain time to derive from "
            "when a reference timestamp is supplied."
        ),
    )
    policy.add_argument(
        "--reference-timestamp",
        type=int,
        default=None,
        help=(
            "judge the terms against this unix time instead of reading the chain. Produces a "
            "reproducible fixture, NOT a live quote: the result is labelled not executable, "
            "because a supplied time is not the time Base will enforce."
        ),
    )
    policy.add_argument(
        "--inclusion-margin",
        type=int,
        default=DEFAULT_INCLUSION_MARGIN_SECONDS,
        help="seconds a live quote must still have left after the observed chain time",
    )
    policy.add_argument("--output", type=Path)
    reconcile = sub.add_parser("reconcile-timeout", help="verify a timeout receipt and persist it")
    reconcile.add_argument("tx_hash")
    reconcile.add_argument("--deal-id", type=int, required=True)
    reconcile.add_argument("--provider", required=True)
    return parser


class TimeBasis(NamedTuple):
    """What "now" meant for one run, and how much it is worth."""

    reference: int
    accept_by: int
    observation: ChainObservation | None
    observed_lag: int


def _resolve_time_basis(args) -> TimeBasis:
    """Decide what "now" means for this run, and refuse to guess.

    Three explicit forms, no implicit fourth:

    * ``--accept-window`` alone reads the chain and derives the deadline from it.
    * ``--accept-by`` alone reads the chain and checks the supplied deadline against it.
      This is the form a negotiated deadline arrives in.
    * ``--accept-by`` with ``--reference-timestamp`` reads nothing and produces a fixture.

    The local clock is never the authority. It appears only as a bound on how far the read
    block may sit from now, and as lag spent out of the inclusion margin. Both can refuse a
    quote; neither can approve one.
    """

    parser = build_parser()
    if (args.accept_by is None) == (args.accept_window is None):
        parser.error("supply exactly one of --accept-by or --accept-window")

    if args.reference_timestamp is not None:
        if args.accept_window is not None:
            parser.error("--accept-window needs chain time; use --accept-by with --reference-timestamp")
        return TimeBasis(args.reference_timestamp, args.accept_by, None, 0)

    observation = observe_chain_time(_web3(), expected_chain_id=_chain_id())
    local_now = int(time.time())
    require_recent(observation, local_now=local_now)
    lag = past_lag(observation, local_now=local_now)
    accept_by = (
        args.accept_by if args.accept_by is not None else observation.timestamp + args.accept_window
    )
    return TimeBasis(observation.timestamp, accept_by, observation, lag)


def _executability(args, basis: TimeBasis) -> dict:
    """State plainly what the executability check was actually worth."""

    if basis.observation is None:
        return {
            "basis": "supplied-reference",
            "reference_timestamp": basis.reference,
            "chain": None,
            "inclusion_margin_seconds": None,
            "observed_lag_seconds": None,
            "executable": False,
            "note": (
                "Judged against a supplied time, not against Base. Reproducible, but not a "
                "live quote: re-derive the deadline from chain time before signing."
            ),
        }
    return {
        "basis": "chain-observation",
        "reference_timestamp": basis.reference,
        "chain": basis.observation.as_dict(),
        "inclusion_margin_seconds": args.inclusion_margin,
        "observed_lag_seconds": basis.observed_lag,
        "executable": True,
        "note": (
            "Judged against the latest observed Base block, with an inclusion margin that "
            "already absorbs the node's observed lag. Re-validate immediately before "
            "signing; inclusion time is not guaranteed."
        ),
    }


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
        basis = _resolve_time_basis(args)
        reference_timestamp, accept_by, observation = basis.reference, basis.accept_by, basis.observation
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
                accept_by=accept_by,
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
            if observation is not None:
                require_inclusion_margin(
                    preimage,
                    chain_timestamp=observation.timestamp,
                    observed_lag_seconds=basis.observed_lag,
                    margin_seconds=args.inclusion_margin,
                )
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
            "executability": _executability(args, basis),
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
        result = reconcile_timeout_claim(
            _memory(),
            _web3(),
            tx_hash=args.tx_hash,
            expected_chain_id=_chain_id(),
            expected_contract=contract,
            expected_deal_id=args.deal_id,
            expected_provider=args.provider,
        )
        print(json.dumps({"created": result.created, "entity": result.entity}, indent=2, sort_keys=True))
        return 0
    raise AssertionError("unreachable")

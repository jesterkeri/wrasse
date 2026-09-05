"""Small, scriptable terminal surface for the Wrasse demo."""

from __future__ import annotations

import argparse
import json
import os
import sys
import secrets
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from dotenv import load_dotenv
from sibyl_memory_client import MemoryClient, NotFoundError
from web3 import HTTPProvider, Web3

from . import chain, escrow
from .chain_time import ChainObservation, observe_chain_time, past_lag, require_recent
from .dimensions import (
    DIMENSION_CATEGORY,
    ENUMERATION_LIMIT as DIMENSION_ENUMERATION_LIMIT,
    DimensionDefinition,
    create_dimension,
    load_dimensions,
)
from .engine import PROFILES, produce_provider_terms, produce_terms
from .memory_gate import recall_counterparty_evidence
from .policy_document import PolicyDocumentError, ValidatedPolicy, load_policy
from .providers import ProviderPersona
from .store import WrasseStore, persona_digest
from .policy_hash import (
    BPS_DENOMINATOR,
    ENGINE_VERSION,
    DEFAULT_INCLUSION_MARGIN_SECONDS,
    EMPTY_EVIDENCE_HASH,
    MAX_DURATION,
    MAX_PROVIDER_BOND_BPS,
    PolicyPreimage,
    evidence_hash,
    policy_hash,
    require_inclusion_margin,
    validate_creatable,
)
from .reconciler import reconcile


#: Bumped whenever the shape of policy.json changes. A consumer that does not recognise the
#: version must refuse the document rather than guess which fields it is looking at.
POLICY_SCHEMA_VERSION = 2

def _write_atomic(path: Path, text: str) -> None:
    """Replace a file in one step, or not at all.

    A policy document is read later by something that will sign against it. A half-written
    one must never be parseable, so the content lands under a temporary name and is moved
    into place only once it is complete and flushed.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _escrow_address() -> str | None:
    """The deployment this document is bound to, or None before there is one."""

    configured = (os.getenv("WRASSE_ESCROW_ADDRESS") or "").strip()
    return Web3.to_checksum_address(configured) if configured else None


#: Every RPC call is bounded. A hung read must fail rather than sit inside a write lock or
#: quietly eat the inclusion margin.
#: One place, so the retry budget can reserve a call's worth before starting another attempt.
#: Two copies of this number would let the budget under-reserve the moment one of them moved.
RPC_TIMEOUT_SECONDS = chain.READ_CALL_TIMEOUT_SECONDS


def _provider(url: str) -> HTTPProvider:
    return HTTPProvider(url, request_kwargs={"timeout": RPC_TIMEOUT_SECONDS})


def _rpc_url() -> str:
    return os.getenv("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org")


def _web3() -> Web3:
    return Web3(_provider(_rpc_url()))


def _chain_id() -> int:
    return int(os.getenv("BASE_SEPOLIA_CHAIN_ID", "84532"))


def _ledger() -> chain.TransactionLedger:
    return chain.TransactionLedger(os.getenv("WRASSE_TX_DB", ".wrasse/transactions.db"))


def _fallback_web3() -> Web3 | None:
    """A second opinion, used only before abandoning a transaction as replaced."""

    url = (os.getenv("BASE_SEPOLIA_FALLBACK_RPC_URL") or "").strip()
    if not url:
        return None
    if url.rstrip("/") == _rpc_url().rstrip("/"):
        raise RuntimeError(
            "the fallback RPC is the same endpoint as the primary; one node asked twice is "
            "not a second opinion"
        )
    return Web3(_provider(url))


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def _deployment_record() -> dict:
    path = Path(os.getenv("WRASSE_DEPLOYMENT_RECORD", "deployments/base-sepolia.json"))
    if not path.exists():
        raise RuntimeError(
            f"{path} is missing. A deployment is not finished until it is recorded, or an "
            "interrupted run leaves two addresses and no way to say which one the demo used."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _store_path(role: str) -> Path:
    default = f".wrasse/{role}-memory.db"
    return Path(os.getenv(f"WRASSE_{role.upper()}_MEMORY_PATH", default))


def _stores() -> dict[str, MemoryClient]:
    """One memory per side, and never the same file twice.

    Two paths that resolve to one store would make every bilateral claim in this project false
    while appearing to work, and because both sides receive the same receipts the collision
    would be invisible from the outside.
    """

    paths = {role: _store_path(role) for role in ("buyer", "provider")}
    resolved = {role: path.resolve() for role, path in paths.items()}
    if resolved["buyer"] == resolved["provider"]:
        raise RuntimeError(
            f"both memory paths resolve to {resolved['buyer']}; one store cannot hold two "
            "independently held memories"
        )
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    return {role: MemoryClient.local(path) for role, path in paths.items()}


def _persona_path() -> Path:
    """Read at call time, not at import.

    A module-level `getenv` is fixed before `load_dotenv` runs, so the `.env` file could never
    have reached it. The same shape of bug as the failpoint, and worth fixing wherever it
    appears rather than only where it was noticed.
    """

    return Path(os.getenv("WRASSE_PROVIDER_PERSONA", "personas/provider-a.json"))


def _open_stores(*, buyer: str, provider: str) -> dict[str, WrasseStore]:
    """One identified memory per side, and never the same file twice."""

    paths = {role: _store_path(role) for role in ("buyer", "provider")}
    if paths["buyer"].resolve() == paths["provider"].resolve():
        raise RuntimeError(
            f"both memory paths resolve to {paths['buyer'].resolve()}; one store cannot hold "
            "two independently held memories"
        )
    owners = {"buyer": buyer, "provider": provider}
    escrow_address = _required_env("WRASSE_ESCROW_ADDRESS")
    return {
        role: WrasseStore.open(
            paths[role], role=role, owner_address=owners[role],
            chain_id=_chain_id(), escrow_address=escrow_address,
        )
        for role in ("buyer", "provider")
    }


def _provider_persona(store: WrasseStore) -> ProviderPersona:
    """Load the committed persona and bind it to this store, once and for all."""

    path = _persona_path()
    digest, document = persona_digest(path)
    persona = ProviderPersona.from_document(document)
    if not _same_address(persona.address, store.identity.owner_address):
        raise RuntimeError(
            f"{path} describes {persona.address}, but this provider store belongs to "
            f"{store.identity.owner_address}"
        )
    store.commit_persona(name=persona.name, digest=digest)
    return persona


def _baseline(name: str, fallback: int) -> int:
    """A quoting baseline, from the environment so both commands read the same one.

    `policy` writes these numbers into a document and `create-deal` derives them again to
    check that document. They therefore have to come from somewhere outside the document,
    which is the whole point, and from one place rather than two, which is what keeps an
    ordinary run from needing four flags twice.
    """

    raw = os.getenv(name)
    if raw is None:
        return fallback
    try:
        return int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name}={raw!r} is not an integer") from error


def _same_address(left: str, right: str) -> bool:
    return Web3.to_checksum_address(left) == Web3.to_checksum_address(right)


def _document_halves(
    quote: "BilateralQuote",
    *,
    buyer: str,
    provider: str,
    accept_by: int,
    commitment: str,
) -> dict[str, Any]:
    """The two halves of the document, derived from memory alone.

    Written by `policy` and rebuilt by `create-deal`, from this one function, so the thing
    that is checked and the thing that was produced cannot drift apart. Everything here is a
    function of the two stores, the committed persona and one deadline: nothing is read back
    out of the document it is being compared against.
    """

    provider_terms = quote.provider_terms
    provider_commitment = evidence_hash(provider_terms.used_evidence_ids)

    profiles = {}
    for name, terms in quote.buyer_terms.items():
        # The bilateral moment. Bond and window come from what the buyer remembers; price and
        # payout delay from what the provider remembers. Negotiating between the two is gate 7;
        # here each side simply opens where its own memory puts it.
        preimage = PolicyPreimage(
            buyer=buyer,
            provider=provider,
            price=provider_terms.price_wei,
            bond_bps=terms.provider_bond_bps,
            accept_by=accept_by,
            service_window=terms.service_window,
            payout_delay=provider_terms.payout_delay,
            engine_version=ENGINE_VERSION,
            # Each hash covers the receipts that moved that side's numbers, not everything it
            # happens to hold. A commitment over unused evidence would describe the reading
            # rather than the reasoning.
            buyer_evidence_hash=evidence_hash(terms.used_evidence_ids),
            provider_evidence_hash=provider_commitment,
        )
        profiles[name] = {
            "terms": {
                "price_wei": preimage.price,
                "provider_bond_bps": terms.provider_bond_bps,
                "service_window": terms.service_window,
                "payout_delay": preimage.payout_delay,
                "risk": str(terms.risk),
                "used_evidence_ids": list(terms.used_evidence_ids),
            },
            "policy_preimage": preimage.as_dict(),
            "policy_hash": policy_hash(preimage),
        }

    return {
        "buyer": {
            "address": buyer,
            "counterparty": provider,
            "verdict": quote.recall["buyer"].verdict,
            "cold_start": quote.recall["buyer"].is_cold_start,
            "recalled_evidence": list(quote.recall["buyer"].evidence),
            "profiles": profiles,
        },
        "provider": {
            "address": provider,
            "counterparty": buyer,
            "verdict": quote.recall["provider"].verdict,
            "cold_start": quote.recall["provider"].is_cold_start,
            "recalled_evidence": list(quote.recall["provider"].evidence),
            "persona": {
                "name": quote.persona.name,
                "cashflow_sensitivity": str(quote.persona.cashflow_sensitivity),
                "commitment": commitment,
            },
            "terms": {
                "price_wei": provider_terms.price_wei,
                "payout_delay": provider_terms.payout_delay,
                "risk": str(provider_terms.risk),
                "used_evidence_ids": list(provider_terms.used_evidence_ids),
            },
        },
    }


@contextmanager
def _both_locked(stores: dict[str, WrasseStore]) -> Iterator[None]:
    """Hold both memories at once, in a fixed order, so a quote reads one moment.

    The order is by resolved lock path rather than by role, because two processes taking the
    two locks in opposite orders would deadlock, and role order is only stable while every
    caller happens to spell it the same way.
    """

    ordered = sorted(stores.values(), key=lambda store: str(store._lock_path))
    with ExitStack() as stack:
        for store in ordered:
            stack.enter_context(store.lock())
        yield


@dataclass(frozen=True)
class BilateralQuote:
    """What the two memories say, and what each side's engine makes of it."""

    recall: dict[str, Any]
    persona: ProviderPersona
    provider_terms: Any
    buyer_terms: dict[str, Any]


def _bilateral_quote(
    stores: dict[str, WrasseStore],
    *,
    buyer: str,
    provider: str,
    base_price_wei: int,
    base_bond_bps: int,
    base_service_window: int,
    base_payout_delay: int,
    profiles: tuple[str, ...] | None = None,
) -> BilateralQuote:
    """Derive both sides' opening terms from the two identified memories.

    The one place the economics are produced. `policy` writes the result into a document and
    `create-deal` runs it again to check the document it was handed came from here, so the two
    must be the same computation rather than two that happen to agree today.
    """

    persona = _provider_persona(stores["provider"])

    # Both stores held at once, for both recalls and the comparison between them.
    #
    # Comparing two sequential snapshots cannot establish a common one. Reading the buyer,
    # releasing, then reading the provider catches a receipt that lands in the provider in
    # between, and misses the same receipt landing in the buyer: both returned sets are then
    # the old one, they agree, and the quote proceeds on a history the code already knows has
    # reached only one memory. One ordering of a race is not a check.
    with _both_locked(stores):
        # Each side recalls the other. Both stores hold the same receipts; what differs is who
        # each one is reading them about, and what it concludes.
        recall = {
            "buyer": stores["buyer"].recall(provider),
            "provider": stores["provider"].recall(buyer),
        }

        # Both sides receive every receipt, so they must agree on which ones exist. A
        # disagreement means a delivery landed in one memory and not the other, and quoting
        # across that gap would price one side on a history the other cannot see.
        held = {
            side: {str(row["event_id"]) for row in recalled.evidence}
            for side, recalled in recall.items()
        }
        if held["buyer"] != held["provider"]:
            only_buyer = sorted(held["buyer"] - held["provider"])
            only_provider = sorted(held["provider"] - held["buyer"])
            raise RuntimeError(
                "the two memories disagree about what happened between these parties. "
                f"Only the buyer holds {only_buyer or 'nothing extra'}; only the provider "
                f"holds {only_provider or 'nothing extra'}. Replay the missing receipts "
                "before quoting rather than pricing across the gap."
            )

    # Each side reads its own ontology. They tend to agree, because both were shown the same
    # public receipts, and that is different from sharing one database. A provider reading the
    # buyer's dimensions would not be an independently held memory.
    dimensions = {side: load_dimensions(stores[side].memory) for side in recall}
    for side, recalled in recall.items():
        missing = sorted({
            event["event_type"]
            for event in recalled.evidence
            if not any(d.source_event_type == event["event_type"] for d in dimensions[side])
        })
        if missing:
            raise RuntimeError(
                f"the {side} holds verified events with no dimension yet: {missing}"
            )

    provider_terms = produce_provider_terms(
        evidence=recall["provider"].evidence,
        dimensions=dimensions["provider"],
        persona=persona,
        base_price_wei=base_price_wei,
        base_payout_delay=base_payout_delay,
    )
    # The signing path needs one profile and the quoting path needs all of them. The causal
    # set is quadratic in the evidence, so re-deriving three profiles at the last moment
    # before a signature would spend time the deadline check has already accounted for.
    wanted = PROFILES if profiles is None else {
        name: PROFILES[name] for name in profiles if name in PROFILES
    }
    buyer_terms = {
        name: produce_terms(
            evidence=recall["buyer"].evidence,
            dimensions=dimensions["buyer"],
            profile=profile,
            base_price_wei=base_price_wei,
            base_bond_bps=base_bond_bps,
            base_service_window=base_service_window,
        )
        for name, profile in wanted.items()
    }
    return BilateralQuote(recall, persona, provider_terms, buyer_terms)


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
    learn.add_argument(
        "--relearn",
        action="store_true",
        help="retire the dimension currently held for this outcome and ask again. Nothing is "
        "erased: the old definition stays in the store, marked retired, so a bad reading is "
        "visible rather than quietly replaced.",
    )
    policy = sub.add_parser("policy", help="produce profile-conditioned policy terms")
    policy.add_argument("provider")
    policy.add_argument("--buyer", required=True, help="buyer address; the commitment covers it")
    policy.add_argument(
        "--base-price-wei",
        type=int,
        default=_baseline("WRASSE_BASE_PRICE_WEI", 10**14),
        help=(
            "starting price in wei before profile adjustment. Defaults to 0.0001 ETH, sized "
            "so a faucet-funded testnet wallet can run the whole loop several times over."
        ),
    )
    policy.add_argument("--base-bond-bps", type=int, default=_baseline("WRASSE_BASE_BOND_BPS", 500))
    policy.add_argument("--service-window", type=int, default=_baseline("WRASSE_SERVICE_WINDOW", 3_600))
    policy.add_argument("--payout-delay", type=int, default=_baseline("WRASSE_PAYOUT_DELAY", 1_800))
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
    ingest = sub.add_parser(
        "reconcile", help="turn a confirmed receipt into evidence in both memories"
    )
    ingest.add_argument("--tx", required=True, dest="tx_hash")

    check = sub.add_parser("deploy-check", help="prove the configured address is this build")
    check.add_argument(
        "--require-fresh",
        action="store_true",
        help="also require that no deal has been created yet; off by default so the identity "
        "check keeps working after the first deal",
    )

    create = sub.add_parser("create-deal", help="sign and broadcast one createDeal")
    create.add_argument("--policy", type=Path, required=True)
    create.add_argument("--profile", required=True, help="chosen explicitly, never inferred")
    create.add_argument("--accept-window", type=int, default=3_600)
    create.add_argument("--inclusion-margin", type=int, default=DEFAULT_INCLUSION_MARGIN_SECONDS)
    # The same baselines `policy` quoted from. They are arguments rather than document fields
    # on purpose: the document is the thing being checked, so a baseline read out of it would
    # let an editor pick the answer the check compares against. Pass whatever was passed to
    # `policy`; the defaults match, so an ordinary run needs neither.
    create.add_argument("--base-price-wei", type=int, default=_baseline("WRASSE_BASE_PRICE_WEI", 10**14))
    create.add_argument("--base-bond-bps", type=int, default=_baseline("WRASSE_BASE_BOND_BPS", 500))
    create.add_argument("--service-window", type=int, default=_baseline("WRASSE_SERVICE_WINDOW", 3_600))
    create.add_argument("--payout-delay", type=int, default=_baseline("WRASSE_PAYOUT_DELAY", 1_800))

    status = sub.add_parser("tx-status", help="read-only view of the transaction ledger")
    status.add_argument("--intent", help="limit to one intent id")

    resolve_cmd = sub.add_parser("tx-resolve", help="apply the resolved state; the only writer")
    resolve_cmd.add_argument("--intent", help="limit to one intent id")
    resolve_cmd.add_argument(
        "--local-confirmation-blocks",
        type=int,
        default=None,
        help="REHEARSAL ONLY. Count blocks instead of reading the chain's safe head, for a "
        "local chain that has none. Not a finality claim, and named in every result it "
        "produces. Never pass this against a real network.",
    )
    resolve_cmd.add_argument(
        "--rebroadcast",
        action="store_true",
        help="resend the identical recorded bytes when the chain has no record of them; "
        f"needs {chain.BROADCAST_ENV}=1",
    )

    for action, meta in escrow.DEAL_ACTIONS.items():
        command = sub.add_parser(
            _COMMAND_NAMES[action],
            help=f"{meta['role']} action; the deal must be {meta['expects']}",
        )
        command.add_argument("--deal-id", type=int, required=True)
        command.set_defaults(deal_action=action)

    collect = sub.add_parser("withdraw", help="collect everything this role is owed")
    collect.add_argument("--role", choices=("buyer", "provider"), required=True)
    collect.add_argument("--to", required=True, help="destination; chosen at collection time")

    rebind = sub.add_parser("rebind-policy", help="bind a pre-deployment quote to a deployment")
    rebind.add_argument("--policy", type=Path, required=True)
    rebind.add_argument("--output", type=Path, required=True)
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


#: The same preimage the Solidity suite pins. Running it through the deployed contract moves
#: the cross-language check from a test fixture to the address the demo will actually use.
CANONICAL_FIXTURE = PolicyPreimage(
    buyer="0x4444444444444444444444444444444444444444",
    provider="0x3333333333333333333333333333333333333333",
    price=10**18,
    bond_bps=2_000,
    accept_by=1_700_000_000,
    service_window=7_200,
    payout_delay=1_800,
    engine_version=ENGINE_VERSION,
    buyer_evidence_hash=evidence_hash(["0x" + "11" * 32, "0x" + "22" * 32]),
    provider_evidence_hash=EMPTY_EVIDENCE_HASH,
)


def _deploy_check(args) -> int:
    """Prove the address in `.env` is the contract this build compiled and reviewed."""

    record = _deployment_record()
    address = Web3.to_checksum_address(_required_env("WRASSE_ESCROW_ADDRESS"))
    chain_id = _chain_id()
    web3 = _web3()
    checks: list[dict] = []

    def check(name: str, produce, expected) -> None:
        """Every check reports; none of them aborts the report.

        A wrong address makes several of these fail at once, and seeing all of them is what
        tells you whether you are looking at the wrong contract or the wrong build.
        """

        try:
            actual = produce()
        except Exception as error:  # noqa: BLE001 - a failing call IS the finding
            actual = f"error: {error}"
        checks.append({"check": name, "ok": actual == expected, "expected": expected, "actual": actual})

    check("record names this chain", lambda: int(record["chain_id"]), chain_id)
    check("record names this address", lambda: Web3.to_checksum_address(record["address"]), address)
    check("node reports this chain", lambda: int(web3.eth.chain_id), chain_id)

    # Constants and one pure function can be imitated. The bytecode hash cannot.
    check(
        "runtime bytecode is this build",
        lambda: escrow.deployed_runtime_hash(web3, address),
        escrow.artifact_runtime_hash(),
    )
    check(
        "runtime bytecode matches the record",
        lambda: escrow.deployed_runtime_hash(web3, address),
        record["runtime_bytecode_hash"],
    )

    deployed_contract = escrow.contract(web3, address)
    check("MAX_DURATION", lambda: int(deployed_contract.functions.MAX_DURATION().call()), MAX_DURATION)
    check(
        "BPS_DENOMINATOR",
        lambda: int(deployed_contract.functions.BPS_DENOMINATOR().call()),
        BPS_DENOMINATOR,
    )
    check(
        "MAX_PROVIDER_BOND_BPS",
        lambda: int(deployed_contract.functions.MAX_PROVIDER_BOND_BPS().call()),
        MAX_PROVIDER_BOND_BPS,
    )
    check(
        "the deployed contract reproduces the canonical fixture",
        lambda: escrow.compute_policy_hash_onchain(web3, address, CANONICAL_FIXTURE),
        policy_hash(CANONICAL_FIXTURE),
    )

    if args.require_fresh:
        check(
            "no deal has been created yet",
            lambda: int(deployed_contract.functions.nextDealId().call()),
            0,
        )

    ok = all(item["ok"] for item in checks)
    print(json.dumps({"address": address, "ok": ok, "checks": checks}, indent=2, sort_keys=True))
    return 0 if ok else 1


def _require_deployment_identity(web3: Web3, address: str, record: dict) -> None:
    """The bytecode at this address must be the build that was reviewed.

    `deploy-check` reports; this refuses. It sits on the path that moves value, because a
    contract implementing one matching pure function can still make `createDeal` do something
    else entirely.
    """

    if int(web3.eth.chain_id) != _chain_id():
        raise RuntimeError(f"the node reports chain {web3.eth.chain_id}, configuration says {_chain_id()}")
    if Web3.to_checksum_address(record["address"]) != Web3.to_checksum_address(address):
        raise RuntimeError(f"the deployment record names {record['address']}, configuration names {address}")

    deployed = escrow.deployed_runtime_hash(web3, address)
    if deployed != escrow.artifact_runtime_hash():
        raise RuntimeError(
            f"the code at {address} hashes to {deployed}, which is not the artifact this build "
            "compiled; refusing to send value to a contract that was not reviewed"
        )
    if deployed != record["runtime_bytecode_hash"]:
        raise RuntimeError(f"the code at {address} does not match the recorded deployment")

    # The recorded deployment block must still be the block it was recorded as.
    block = web3.eth.get_block(int(record["block_number"]))
    recorded_hash = str(record["block_hash"])
    if not recorded_hash.startswith("0x"):
        recorded_hash = "0x" + recorded_hash
    if "0x" + bytes(block["hash"]).hex() != recorded_hash.lower():
        raise RuntimeError(
            f"deployment block {record['block_number']} no longer has the recorded hash; "
            "the deployment record and this chain disagree"
        )


def _require_document_came_from_memory(
    policy: ValidatedPolicy,
    *,
    stores: dict[str, WrasseStore],
    buyer: str,
    provider: str,
    args,
    every_profile: bool = True,
) -> None:
    """Rebuild the document from the stores and refuse one that disagrees with it.

    This is what makes `policy.json` safe to sign against. Without it the file is trusted for
    the one thing it cannot be trusted for: where its contents came from. Every check inside
    the validator compares the document against itself, and the commitment those fields
    produce is a public unkeyed hash of them, so an edit applied consistently and re-hashed
    leaves nothing disagreeing with anything.

    Both halves are compared, not only the four numbers the signature covers. The money
    follows the committed terms, but the risk scores, the persona, the verdicts and the
    profiles nobody selected are what a judge reads as the reason for them, and an invented
    reason beside a genuine transaction is the failure this entry exists to rule out.

    `every_profile` is false only for the recheck immediately before signing, where the
    question is narrower: has memory moved since the document was written. Any movement
    changes the recalled evidence, which is compared either way.
    """

    wanted = None if every_profile else (policy.profile,)
    quote = _bilateral_quote(
        stores, buyer=buyer, provider=provider,
        base_price_wei=args.base_price_wei, base_bond_bps=args.base_bond_bps,
        base_service_window=args.service_window, base_payout_delay=args.payout_delay,
        profiles=wanted,
    )
    if policy.profile not in quote.buyer_terms:
        raise RuntimeError(f"{policy.profile!r} is not a profile this engine produces")

    commitment = stores["provider"].persona_commitment()
    rebuilt = _document_halves(
        quote, buyer=buyer, provider=provider,
        # The deadline is the one part of the document that memory cannot reproduce: it was
        # derived from chain time when the quote was written. Taking it from the document
        # makes every hash below reproducible, and it is not a term memory decides. What it
        # commits to is checked against chain time again, at signing.
        accept_by=policy.quoted_accept_by,
        commitment="" if commitment is None else commitment["sha256"],
    )

    for side in ("buyer", "provider"):
        written = dict(policy.document[side])
        derived = dict(rebuilt[side])
        if not every_profile:
            written.pop("profiles", None)
            derived.pop("profiles", None)
        elif side == "buyer" and set(written.get("profiles", {})) != set(derived["profiles"]):
            raise RuntimeError(
                f"{args.policy} offers profiles {sorted(written.get('profiles', {}))}, and "
                f"quoting from these two memories produces {sorted(derived['profiles'])}. A "
                "document may not add, drop or rename the choices a reader is given."
            )
        for field in sorted(derived):
            if written.get(field) != derived[field]:
                raise RuntimeError(
                    f"{args.policy} and the {side} memory disagree about {field!r}. Refusing "
                    "to sign against a document this build cannot reproduce. Either it was "
                    "edited, or a receipt has been reconciled since it was written, or the "
                    "baselines differ from the ones it was produced with: re-run `policy` "
                    "with the same arguments and use the document it writes."
                )


def _fee_fields(web3: Web3) -> tuple[int, int]:
    """A priority fee the chain will accept, and a ceiling that survives a fee rise."""

    latest = web3.eth.get_block("latest")
    base = int(latest.get("baseFeePerGas") or 0)
    try:
        priority = int(web3.eth.max_priority_fee)
    except Exception:  # noqa: BLE001 - not every node exposes it
        priority = 10**6
    priority = max(priority, 10**6)
    return priority, base * 2 + priority


def _create_deal(args) -> int:
    chain_id = _chain_id()
    address = Web3.to_checksum_address(_required_env("WRASSE_ESCROW_ADDRESS"))
    buyer = Web3.to_checksum_address(_required_env("WRASSE_BUYER_ADDRESS"))
    provider = Web3.to_checksum_address(_required_env("WRASSE_PROVIDER_A_ADDRESS"))
    ledger = _ledger()
    web3 = _web3()

    policy = load_policy(
        args.policy,
        profile=args.profile,
        chain_id=chain_id,
        contract_address=address,
        buyer=buyer,
        provider=provider,
    )

    # Look the intent up before touching the chain or the keystore. The terms move between
    # attempts; the identity does not, which is what makes this a retry rather than a new deal.
    existing = ledger.find(
        chain_id=chain_id, wallet=buyer, contract_address=address, intent_id=policy.intent_id
    )
    if existing is not None:
        chain.verify_row_integrity(existing)
        print(json.dumps({
            "intent_id": existing.intent_id,
            "already_signed": True,
            "tx_hash": existing.tx_hash,
            "status": existing.status,
            "note": "this quote and profile already produced a transaction. Nothing was built "
                    "or sent. Run tx-resolve to find out what became of it.",
        }, indent=2, sort_keys=True))
        return 0

    # The document is a display artifact, not an authority. Everything in it agrees with
    # itself by construction: the displayed terms match the preimage, the preimage hashes to
    # the quoted hash, and that hash is public and unkeyed. An editor who changes the price in
    # all four places and recomputes the hash produces a document that passes every internal
    # check and funds a number no memory ever produced. Consistency is not provenance.
    #
    # So the economics are derived again here, from the two identified stores, and the
    # document is only accepted if this machine reaches the same numbers. The baselines are
    # this command's own arguments rather than fields of the document, because a baseline read
    # out of the file would be one more number the editor gets to choose.
    stores = _open_stores(buyer=buyer, provider=provider)
    _require_document_came_from_memory(
        policy, stores=stores, buyer=buyer, provider=provider, args=args
    )

    _require_deployment_identity(web3, address, _deployment_record())

    account = chain.load_signer(
        _required_env("WRASSE_KEYSTORE"),
        _required_env("WRASSE_KEYSTORE_PASSWORD_FILE"),
        expected_address=buyer,
    )
    engine_version_hash = "0x" + bytes(Web3.keccak(text=policy.engine_version)).hex()

    def calldata_for(accept_by: int) -> str:
        return escrow.create_deal_calldata(
            web3,
            address,
            provider=policy.provider,
            bond_bps=policy.bond_bps,
            accept_by=accept_by,
            service_window=policy.service_window,
            payout_delay=policy.payout_delay,
            engine_version_hash=engine_version_hash,
            buyer_evidence_hash=policy.buyer_evidence_hash,
            provider_evidence_hash=policy.provider_evidence_hash,
        )

    # Everything expensive happens here, outside the write lock and before the deadline that
    # will actually be committed is chosen. Gas does not depend on the timestamp's value, so
    # an estimate taken against a provisional deadline is still the right estimate.
    provisional = observe_chain_time(web3, expected_chain_id=chain_id)
    require_recent(provisional, local_now=int(time.time()))
    estimate = web3.eth.estimate_gas({
        "from": buyer,
        "to": address,
        "value": policy.price_wei,
        "data": calldata_for(provisional.timestamp + args.accept_window),
    })
    gas_limit = chain.bounded_gas_limit(int(estimate))
    max_priority, max_fee = _fee_fields(web3)
    worst_case = chain.require_affordable(
        int(web3.eth.get_balance(buyer)),
        value_wei=policy.price_wei,
        gas_limit=gas_limit,
        max_fee_wei=max_fee,
    )
    committed: dict = {}

    def sign(nonce: int) -> chain.SignedIntent:
        """Runs inside the write lock, immediately before the signature.

        This is the last moment the deadline can be judged, so it is judged here rather than
        before gas estimation, which can take long enough to eat the margin.
        """

        # Provenance again, here rather than only above. The earlier call is a fast refusal
        # for an edited file; it is not a binding between the memories and the signature. A
        # `reconcile` running alongside this one can ingest a newly confirmed receipt into
        # both stores while this command decrypts a keystore and estimates gas, and the terms
        # would then be signed against a history that has already moved. Reconciliation and
        # sending are deliberately separate commands, so that gap is ordinary rather than
        # exotic.
        _require_document_came_from_memory(
            policy, stores=stores, buyer=buyer, provider=provider, args=args,
            every_profile=False,
        )

        final = observe_chain_time(web3, expected_chain_id=chain_id)
        local_now = int(time.time())
        require_recent(final, local_now=local_now)
        accept_by = final.timestamp + args.accept_window
        preimage = policy.preimage_for(accept_by)

        validate_creatable(preimage, reference_timestamp=final.timestamp)
        require_inclusion_margin(
            preimage,
            chain_timestamp=final.timestamp,
            observed_lag_seconds=past_lag(final, local_now=local_now),
            margin_seconds=args.inclusion_margin,
        )

        local_hash = policy_hash(preimage)
        onchain_hash = escrow.compute_policy_hash_onchain(web3, address, preimage)
        if local_hash != onchain_hash:
            raise RuntimeError(
                f"this build hashes the terms to {local_hash}, the deployed contract to "
                f"{onchain_hash}; refusing to commit to terms the chain reads differently"
            )

        # That call is an RPC and can stall, so the deadline is judged once more against the
        # newest block. `accept_by` is already fixed by the hash above, so this re-checks the
        # committed value rather than moving it.
        latest = observe_chain_time(web3, expected_chain_id=chain_id)
        local_now = int(time.time())
        require_recent(latest, local_now=local_now)
        validate_creatable(preimage, reference_timestamp=latest.timestamp)
        require_inclusion_margin(
            preimage,
            chain_timestamp=latest.timestamp,
            observed_lag_seconds=past_lag(latest, local_now=local_now),
            margin_seconds=args.inclusion_margin,
        )

        transaction = {
            "chainId": chain_id,
            "nonce": nonce,
            "to": address,
            "data": calldata_for(accept_by),
            "value": policy.price_wei,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority,
            "gas": gas_limit,
            "type": 2,
        }
        committed.update({
            "accept_by": accept_by,
            "policy_hash": local_hash,
            "observed_block": final.block_number,
        })
        return chain.with_intent_context(
            chain.sign_transaction(account, transaction),
            accept_by=accept_by,
            preimage=preimage.as_dict(),
        )

    row, created = ledger.record_signed(
        chain_id=chain_id,
        wallet=buyer,
        contract_address=address,
        intent_id=policy.intent_id,
        # Read inside the lock. A count taken before gas estimation is already old, and this
        # program's lock does not stop anything else using the same wallet.
        read_chain_nonce=lambda: int(web3.eth.get_transaction_count(buyer, "pending")),
        sign=sign,
    )
    if not created:
        print(json.dumps({"intent_id": row.intent_id, "already_signed": True,
                          "tx_hash": row.tx_hash, "status": row.status}, indent=2, sort_keys=True))
        return 0

    # Durably recorded before the RPC is called, so a crash here lands in the window the
    # resolver exists for rather than losing the transaction entirely.
    row = ledger.set_status(row, chain.SEND_ATTEMPTED, bump_attempts=True)
    try:
        outcome = chain.broadcast(web3, row)
    except chain.DeterministicRejection as error:
        # The node refused it during pre-validation, on the very first attempt, so these bytes
        # never entered a mempool and nothing can ever mine at this nonce. Releasing it is the
        # difference between one bounced send and a wallet that is stuck forever.
        ledger.mark_unbroadcast(row, str(error))
        print(json.dumps({
            "intent_id": row.intent_id,
            "status": chain.UNBROADCAST,
            "nonce_released": row.nonce,
            "error": str(error),
            "note": "the node refused this before admitting it, so nonce "
                    f"{row.nonce} was never used and is available again",
        }, indent=2, sort_keys=True))
        return 1

    row = ledger.set_status(
        row,
        outcome.status,
        last_error=None if outcome.status == chain.PENDING else outcome.detail,
    )
    print(json.dumps({
        "intent_id": row.intent_id,
        "status": row.status,
        "detail": outcome.detail,
        "tx_hash": row.tx_hash,
        "nonce": row.nonce,
        "quoted": {"accept_by": policy.quoted_accept_by, "policy_hash": policy.quoted_policy_hash},
        "signed": committed,
        "moved_fields": ["accept_by"],
        "gas": {"estimate": int(estimate), "limit": gas_limit, "max_fee_wei": max_fee,
                "max_priority_wei": max_priority, "worst_case_wei": worst_case},
    }, indent=2, sort_keys=True))
    return 0


def _chain_now(web3: Web3) -> int | None:
    """The chain's own clock, or None if it could not be read.

    Only the chain decides whether a deadline has passed. Not knowing the time is a reason to
    refuse a resend, never a reason to assume there is still time.
    """

    try:
        return observe_chain_time(web3, expected_chain_id=_chain_id()).timestamp
    except Exception:  # noqa: BLE001
        return None


_COMMAND_NAMES = {
    "acceptDeal": "accept-deal",
    "markDelivered": "mark-delivered",
    "releaseDeal": "release-deal",
    "claimPayment": "claim-payment",
    "claimTimeout": "claim-timeout",
    "cancelUnaccepted": "cancel-unaccepted",
}


def _role_signer(role: str) -> Any:
    """The wallet allowed to play this role, proved by deriving it from the key."""

    address = Web3.to_checksum_address(
        _required_env("WRASSE_BUYER_ADDRESS" if role == "buyer" else "WRASSE_PROVIDER_A_ADDRESS")
    )
    keystore = _required_env("WRASSE_KEYSTORE" if role == "buyer" else "WRASSE_PROVIDER_A_KEYSTORE")
    account = chain.load_signer(
        keystore, _required_env("WRASSE_KEYSTORE_PASSWORD_FILE"), expected_address=address
    )
    return account, address


def _next_attempt_intent(ledger, *, chain_id, wallet, contract_address, base: str) -> tuple[str, Any]:
    """The intent to use now, and the row already under it if there is one.

    A deal action's identity is permanent, which would make an action that mined and reverted
    impossible to ever attempt again. A confirmed revert, and only that, opens a fresh attempt
    under a numbered identity. Anything successful or still in flight keeps its uniqueness.
    """

    attempt = 1
    while True:
        intent = base if attempt == 1 else f"{base}#{attempt}"
        row = ledger.find(
            chain_id=chain_id, wallet=wallet, contract_address=contract_address, intent_id=intent
        )
        if row is None:
            return intent, None
        if row.status != chain.CONFIRMED_REVERTED:
            return intent, row
        attempt += 1


def _send(args, *, role, action, calldata, value_wei, preimage, intent_id, note=None) -> int:
    """One signed action, through the same orchestrator `create-deal` uses."""

    chain_id = _chain_id()
    address = Web3.to_checksum_address(_required_env("WRASSE_ESCROW_ADDRESS"))
    ledger = _ledger()
    web3 = _web3()
    _require_deployment_identity(web3, address, _deployment_record())
    account, wallet = _role_signer(role)

    existing = ledger.find(
        chain_id=chain_id, wallet=wallet, contract_address=address, intent_id=intent_id
    )
    if existing is not None:
        chain.verify_row_integrity(existing)
        print(json.dumps({"intent_id": intent_id, "already_signed": True,
                          "tx_hash": existing.tx_hash, "status": existing.status,
                          "note": "already sent. Nothing was built. Run tx-resolve."},
                         indent=2, sort_keys=True))
        return 0

    estimate = web3.eth.estimate_gas(
        {"from": wallet, "to": address, "value": value_wei, "data": calldata}
    )
    gas_limit = chain.bounded_gas_limit(int(estimate))
    max_priority, max_fee = _fee_fields(web3)
    chain.require_affordable(
        int(web3.eth.get_balance(wallet)),
        value_wei=value_wei, gas_limit=gas_limit, max_fee_wei=max_fee,
    )

    def sign(nonce: int) -> chain.SignedIntent:
        transaction = {
            "chainId": chain_id, "nonce": nonce, "to": address, "data": calldata,
            "value": value_wei, "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority, "gas": gas_limit, "type": 2,
        }
        return chain.with_intent_context(
            chain.sign_transaction(account, transaction), accept_by=0, preimage=preimage
        )

    row, created = ledger.record_signed(
        chain_id=chain_id, wallet=wallet, contract_address=address, intent_id=intent_id,
        read_chain_nonce=lambda: int(web3.eth.get_transaction_count(wallet, "pending")),
        sign=sign,
    )
    if not created:
        print(json.dumps({"intent_id": row.intent_id, "already_signed": True,
                          "tx_hash": row.tx_hash, "status": row.status}, indent=2, sort_keys=True))
        return 0

    row = ledger.set_status(row, chain.SEND_ATTEMPTED, bump_attempts=True)
    try:
        outcome = chain.broadcast(web3, row)
    except chain.DeterministicRejection as error:
        ledger.mark_unbroadcast(row, str(error))
        print(json.dumps({"intent_id": row.intent_id, "status": chain.UNBROADCAST,
                          "nonce_released": row.nonce, "error": str(error)},
                         indent=2, sort_keys=True))
        return 1

    row = ledger.set_status(
        row, outcome.status, last_error=None if outcome.status == chain.PENDING else outcome.detail
    )
    report = {"intent_id": row.intent_id, "action": action, "role": role, "status": row.status,
              "tx_hash": row.tx_hash, "nonce": row.nonce, "detail": outcome.detail}
    if note:
        report["note"] = note
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _deal_action(args) -> int:
    action = args.deal_action
    meta = escrow.DEAL_ACTIONS[action]
    role = meta["role"]
    chain_id = _chain_id()
    address = Web3.to_checksum_address(_required_env("WRASSE_ESCROW_ADDRESS"))
    web3 = _web3()
    ledger = _ledger()

    # Identity is resolved before anything else, and before the keystore is touched. A retry
    # after a crash has to return the transaction it already sent, not a complaint that the
    # deal has since moved on: the state check is for new actions, not for retries.
    wallet = Web3.to_checksum_address(
        _required_env("WRASSE_BUYER_ADDRESS" if role == "buyer" else "WRASSE_PROVIDER_A_ADDRESS")
    )
    base = f"deal:{chain_id}:{chain.canonical_address(address)}:{args.deal_id}:{action}"
    intent_id, existing = _next_attempt_intent(
        ledger, chain_id=chain_id, wallet=wallet, contract_address=address, base=base
    )
    if existing is not None:
        chain.verify_row_integrity(existing)
        print(json.dumps({"intent_id": existing.intent_id, "already_signed": True,
                          "tx_hash": existing.tx_hash, "status": existing.status,
                          "note": "already sent. Nothing was built. Run tx-resolve."},
                         indent=2, sort_keys=True))
        return 0

    # A signing-time precondition, not a binding. The state can change before inclusion and the
    # contract reverts safely if it does; checking here turns a wasted transaction into a
    # refusal that explains itself.
    deal = escrow.read_deal(web3, address, args.deal_id)
    if deal["state"] != meta["expects"]:
        raise RuntimeError(
            f"deal {args.deal_id} is {deal['state']}, and {action} needs it {meta['expects']}"
        )
    if chain.canonical_address(deal[role]) != chain.canonical_address(wallet):
        raise RuntimeError(
            f"deal {args.deal_id} names {deal[role]} as its {role}, this wallet is {wallet}"
        )

    # The bond is re-read here rather than carried from an earlier look. A bond that moved
    # between the two reads means this is not the deal we thought it was.
    value = deal["provider_bond"] if meta["payable"] else 0
    return _send(
        args, role=role, action=action,
        calldata=escrow.deal_action_calldata(web3, address, action, args.deal_id),
        value_wei=value,
        preimage={"action": action, "deal_id": args.deal_id, "role": role},
        intent_id=intent_id,
    )


def _withdraw(args) -> int:
    """Collect this role's credit.

    The amount is deliberately not part of the intent. `withdraw(recipient)` takes everything
    the caller is owed at execution time, the amount is not calldata, and another settlement can
    raise it between signing and mining. The observed credit is recorded as a snapshot for the
    audit trail and the collected amount may legitimately be larger.
    """

    chain_id = _chain_id()
    address = Web3.to_checksum_address(_required_env("WRASSE_ESCROW_ADDRESS"))
    recipient = Web3.to_checksum_address(args.to)
    web3 = _web3()
    _, wallet = _role_signer(args.role)
    ledger = _ledger()

    credit = int(escrow.contract(web3, address).functions.withdrawable(
        Web3.to_checksum_address(wallet)
    ).call())
    if credit == 0:
        raise RuntimeError(f"{wallet} has nothing to collect")

    # Identity persists in the ledger itself, which is the only durable atomic store here. A
    # crash before the row commits signed nothing, so a fresh id is harmless; a crash after it
    # commits finds this row and resolves it rather than starting a second withdrawal.
    intent_id = None
    for row in ledger.rows(chain_id=chain_id):
        preimage = row.preimage if isinstance(row.preimage, dict) else {}
        if (
            row.wallet == chain.canonical_address(wallet)
            and preimage.get("action") == "withdraw"
            and chain.canonical_address(preimage.get("recipient", chain.ZERO_ADDRESS))
            == chain.canonical_address(recipient)
            and row.status not in chain.TERMINAL_STATUSES
        ):
            intent_id = row.intent_id
            break
    intent_id = intent_id or f"withdraw:{chain_id}:{chain.canonical_address(address)}:{secrets.token_hex(8)}"

    return _send(
        args, role=args.role, action="withdraw",
        calldata=escrow.withdraw_calldata(web3, address, recipient),
        value_wei=0,
        preimage={"action": "withdraw", "role": args.role, "recipient": recipient,
                  "credit_snapshot_wei": str(credit)},
        intent_id=intent_id,
        note="the snapshot is an estimate; withdraw collects everything owed at execution, "
             "which may be more",
    )


def _rows_for(args, ledger: chain.TransactionLedger) -> list[chain.LedgerRow]:
    rows = ledger.rows(chain_id=_chain_id())
    intent = getattr(args, "intent", None)
    return [row for row in rows if intent is None or row.intent_id == intent]


def _tx_status(args) -> int:
    """Read-only. It may ask the chain; it never writes and never sends.

    The command you run after a crash should not be the command that can spend.
    """

    ledger = _ledger()
    web3 = _web3()
    fallback = _fallback_web3()
    chain_now = _chain_now(web3)
    report = []
    for row in _rows_for(args, ledger):
        entry = {"intent_id": row.intent_id, "status": row.status, "nonce": row.nonce,
                 "tx_hash": row.tx_hash, "attempts": row.attempts}
        if row.is_terminal:
            entry["verdict"] = {"status": row.status, "detail": "terminal"}
        else:
            try:
                verdict = chain.resolve(web3, row, fallback_web3=fallback, chain_now=chain_now)
                entry["verdict"] = {"status": verdict.status, "detail": verdict.detail,
                                    "may_rebroadcast": verdict.may_rebroadcast and chain_now is not None}
            except chain.RpcUnavailable as error:
                entry["verdict"] = {"status": "unreadable", "detail": str(error)}
        report.append(entry)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _tx_resolve(args) -> int:
    """The only writer. Applies what the chain says, and can resend identical bytes."""

    ledger = _ledger()
    web3 = _web3()
    fallback = _fallback_web3()
    report = []

    if args.local_confirmation_blocks is not None:
        # A warning is not a boundary. The rehearsal runs on the production chain id on
        # purpose, so the id cannot fence this; two independent local facts must.
        client = chain.require_local_chain(web3, _rpc_url())
        policy = chain.local_depth_policy(args.local_confirmation_blocks)
        print(
            f"!! confirming by {policy.name} against {client}, not by the chain's safe head. "
            "Rehearsal only.",
            file=sys.stderr,
        )
    else:
        policy = chain.safe_head_policy()

    chain_now = _chain_now(web3)
    record = _deployment_record()

    for row in _rows_for(args, ledger):
        if row.is_terminal:
            report.append({"intent_id": row.intent_id, "status": row.status, "action": "none"})
            continue

        # An included row is judged as included first. Asking the unmined resolver about it
        # would return a verdict the state graph cannot accept, and a reorg would raise instead
        # of being recorded.
        if row.status in (chain.INCLUDED_SUCCESS, chain.INCLUDED_REVERTED):
            settled = chain.confirm(web3, row, policy=policy)
            if settled.status != row.status:
                row = ledger.set_status(row, settled.status, block_number=settled.block_number,
                                        block_hash=settled.block_hash)
            report.append({"intent_id": row.intent_id, "status": row.status,
                           "confirmation_basis": policy.name,
                           "detail": settled.detail, "action": "resolved"})
            continue

        verdict = chain.resolve(web3, row, fallback_web3=fallback, chain_now=chain_now)

        if verdict.status == chain.UNKNOWN:
            if chain_now is None:
                report.append({"intent_id": row.intent_id, "status": row.status,
                               "verdict": verdict.status,
                               "action": "chain time unreadable; refusing to resend blind"})
                continue
            if not args.rebroadcast:
                report.append({"intent_id": row.intent_id, "status": row.status,
                               "verdict": verdict.status, "detail": verdict.detail,
                               "action": "pass --rebroadcast to resend the identical bytes"})
                continue
            # Replay is a send. It carries the same preconditions as the first one: the
            # code at the address is still the reviewed build, the row belongs to the
            # deployment this run is configured for, and the deadline is still live at the
            # moment of sending rather than at the moment the command started.
            if row.contract_address != chain.canonical_address(
                _required_env("WRASSE_ESCROW_ADDRESS")
            ):
                report.append({"intent_id": row.intent_id, "status": row.status,
                               "action": "belongs to another deployment; not resent"})
                continue
            _require_deployment_identity(web3, _required_env("WRASSE_ESCROW_ADDRESS"), record)

            send_time = _chain_now(web3)
            if send_time is None or row.accept_by <= send_time:
                row = ledger.set_status(row, chain.STUCK)
                report.append({"intent_id": row.intent_id, "status": row.status,
                               "action": "the deadline passed while resolving; not resent"})
                continue

            row = ledger.set_status(row, chain.SEND_ATTEMPTED, bump_attempts=True)
            try:
                outcome = chain.broadcast(web3, row)
            except chain.DeterministicRejection as error:
                # These bytes were accepted somewhere once, so unlike the first send this does
                # not prove the nonce is free. Hold the wallet and say so.
                row = ledger.set_status(row, chain.STUCK, last_error=str(error))
                report.append({"intent_id": row.intent_id, "status": row.status,
                               "action": "refused on resend; the nonce is held, not released",
                               "detail": str(error)})
                continue
            row = ledger.set_status(row, outcome.status,
                                    last_error=None if outcome.status == chain.PENDING else outcome.detail)
            report.append({"intent_id": row.intent_id, "status": row.status,
                           "action": "resent the identical recorded bytes"})
            continue

        if verdict.status != row.status:
            row = ledger.set_status(row, verdict.status, block_number=verdict.block_number,
                                    block_hash=verdict.block_hash, last_error=None)

        # A row that just became included is confirmed in the same pass, so one command takes
        # a transaction all the way rather than needing to be run twice.
        if row.status in (chain.INCLUDED_SUCCESS, chain.INCLUDED_REVERTED):
            settled = chain.confirm(web3, row, policy=policy)
            if settled.status != row.status:
                row = ledger.set_status(row, settled.status, block_number=settled.block_number,
                                        block_hash=settled.block_hash)
            report.append({"intent_id": row.intent_id, "status": row.status,
                           "confirmation_basis": policy.name,
                           "detail": settled.detail, "action": "resolved"})
            continue

        report.append({"intent_id": row.intent_id, "status": row.status,
                       "detail": verdict.detail, "action": "resolved"})

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _rebind_policy(args) -> int:
    """Bind a quote written before deployment to the deployment it will execute against.

    A new `request_id` is minted deliberately. A rebound quote is a different action, and
    reusing the old identity would let the pre-deployment document and the bound one both
    claim to be the same deal.
    """

    document = json.loads(args.policy.read_text(encoding="utf-8"))
    address = Web3.to_checksum_address(_required_env("WRASSE_ESCROW_ADDRESS"))
    previous = document.get("request_id")
    document["chain_id"] = _chain_id()
    document["contract_address"] = address
    document["request_id"] = secrets.token_hex(16)

    _write_atomic(args.output, json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "contract_address": address,
                      "previous_request_id": previous,
                      "request_id": document["request_id"]}, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    # Read before dotenv can populate the environment from a file. A crash simulation that a
    # stale `.env` could arm would eventually fire during a real run.
    chain.arm_failpoint(os.environ.get(chain.FAILPOINT_ENV))
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
        # One model call, then the same definition into each side's own store. They hold it
        # separately because they are separate memories, not because they disagree.
        stores = _open_stores(
            buyer=_required_env("WRASSE_BUYER_ADDRESS"),
            provider=_required_env("WRASSE_PROVIDER_A_ADDRESS"),
        )

        # Through the same fail-closed validation the pricing path uses, and from both
        # memories rather than whichever one answers first. Learning from a row the pricing
        # path would refuse is that same defect one step earlier: the definition is written
        # active and moves real terms as soon as a genuine receipt of its type is recalled. An
        # unverified row needs only a recognised `event_type` to choose which trusted template
        # goes to the model.
        bodies = {side: store.verified_event(args.event_id) for side, store in stores.items()}
        if bodies["buyer"] != bodies["provider"]:
            raise RuntimeError(
                f"the two memories hold different bodies for {args.event_id}. A receipt is one "
                "public fact; two readings of it are not something to learn from."
            )
        event = bodies["buyer"]

        def active_for(store, event_type: str) -> list[dict]:
            """Every active definition for one outcome, not merely the first.

            `next(...)` over the list was the bug behind the bug: with two active rows it saw
            one, so `--relearn` retired one and added another and left two again, and the
            divergence check compared one row per side while `load_dimensions` refused the
            store outright.
            """

            rows = store.memory.list_entities(
                DIMENSION_CATEGORY, status="active", limit=DIMENSION_ENUMERATION_LIMIT
            )
            return [row for row in rows if row["body"].get("source_event_type") == event_type]

        existing = {}
        for side, store in stores.items():
            with store._exclusive():
                rows = active_for(store, event["event_type"])
                if args.relearn:
                    for row in rows:  # all of them, or relearning cannot repair a duplicate
                        store.memory.set_entity(
                            DIMENSION_CATEGORY, row["name"], row["body"], status="retired"
                        )
                    rows = []
                if len(rows) > 1:
                    raise RuntimeError(
                        f"the {side} memory holds {len(rows)} active dimensions for "
                        f"{event['event_type']}: {sorted(row['name'] for row in rows)}. Every "
                        "receipt of that kind would be scored once per row. Re-run with "
                        "--relearn to retire all of them and learn one."
                    )
            existing[side] = rows[0] if rows else None

        # A disagreement between the two stores is not something to resolve by picking one.
        readings = {json.dumps(row["body"], sort_keys=True) for row in existing.values()
                    if row is not None}
        if len(readings) > 1:
            raise RuntimeError(
                f"the two memories hold different dimensions for {event['event_type']}. "
                "Re-run with --relearn rather than letting one side's reading stand in for "
                "the other's."
            )

        held = next((row for row in existing.values() if row is not None), None)
        definition = (
            DimensionDefinition.from_model(
                {k: held["body"][k] for k in
                 ("dimension_id", "signal_direction", "severity", "confidence", "applies_when")},
                held["body"]["source_event_type"],
            )
            if held is not None
            else None
        )
        if definition is None:
            definition = create_dimension(
                event,
                api_key=os.environ["OPENROUTER_API_KEY"],
                model=os.getenv("WRASSE_LLM_MODEL", "openai/gpt-oss-20b"),
            )

        learned = {}
        for side, store in stores.items():
            if existing[side] is not None:
                learned[side] = False
                continue
            # Re-read under the write lock. The model call is a network round trip and holding
            # a file lock across it would block the other side for its duration, so the lock is
            # released and the decision is taken again here. Two learners that both saw nothing
            # would otherwise each write a different model-supplied id, and `load_dimensions`
            # refuses that ambiguity: the command meant to make a store quoteable would be what
            # stopped it quoting.
            with store._exclusive():
                if active_for(store, event["event_type"]):
                    raise RuntimeError(
                        f"another learner wrote a dimension for {event['event_type']} into the "
                        f"{side} memory while this one was asking the model. Nothing was "
                        "written. Re-run to see what it decided."
                    )
                store.memory.set_entity(
                    DIMENSION_CATEGORY, definition.dimension_id, definition.body(),
                    status="active",
                )
            learned[side] = True

        print(json.dumps({
            "event_type": event["event_type"],
            "dimension": definition.body(),
            "model_called": all(value is None for value in existing.values()),
            "written_to": learned,
        }, indent=2, sort_keys=True))
        return 0
    if args.command == "policy":
        chain_id = _chain_id()
        escrow_address = _escrow_address()
        if escrow_address is None:
            raise RuntimeError(
                "WRASSE_ESCROW_ADDRESS is not set. A quote is bound to one deployment, and a "
                "document that names none cannot be executed against any."
            )
        buyer = Web3.to_checksum_address(args.buyer)
        provider = Web3.to_checksum_address(args.provider)

        stores = _open_stores(buyer=buyer, provider=provider)
        quote = _bilateral_quote(
            stores, buyer=buyer, provider=provider,
            base_price_wei=args.base_price_wei, base_bond_bps=args.base_bond_bps,
            base_service_window=args.service_window, base_payout_delay=args.payout_delay,
        )
        persona, recall = quote.persona, quote.recall

        basis = _resolve_time_basis(args)
        reference_timestamp, accept_by, observation = basis.reference, basis.accept_by, basis.observation

        halves = _document_halves(
            quote, buyer=buyer, provider=provider, accept_by=accept_by,
            commitment=stores["provider"].persona_commitment()["sha256"],
        )
        for name, profile in halves["buyer"]["profiles"].items():
            preimage = PolicyPreimage(**profile["policy_preimage"])
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

        output = {
            # Immutable identity, fixed before any transaction exists. `request_id` is what
            # makes a retry a retry: the committed terms move between attempts because the
            # deadline is re-derived from chain time, so action identity cannot come from them.
            "schema_version": POLICY_SCHEMA_VERSION,
            "request_id": secrets.token_hex(16),
            "chain_id": chain_id,
            "contract_address": escrow_address,
            "engine_version": ENGINE_VERSION,
            "executability": _executability(args, basis),
            "buyer": halves["buyer"],
            "provider": halves["provider"],
        }
        rendered = json.dumps(output, indent=2, sort_keys=True)
        if args.output:
            _write_atomic(args.output, rendered + "\n")
        print(rendered)
        return 0
    if getattr(args, "deal_action", None) is not None:
        return _deal_action(args)
    if args.command == "withdraw":
        return _withdraw(args)
    if args.command == "deploy-check":
        return _deploy_check(args)
    if args.command == "create-deal":
        return _create_deal(args)
    if args.command == "tx-status":
        return _tx_status(args)
    if args.command == "tx-resolve":
        return _tx_resolve(args)
    if args.command == "rebind-policy":
        return _rebind_policy(args)
    if args.command == "reconcile":
        results = reconcile(
            _open_stores(
                buyer=_required_env("WRASSE_BUYER_ADDRESS"),
                provider=_required_env("WRASSE_PROVIDER_A_ADDRESS"),
            ),
            _web3(),
            _ledger(),
            tx_hash=args.tx_hash,
            expected_chain_id=_chain_id(),
            expected_contract=_required_env("WRASSE_ESCROW_ADDRESS"),
        )
        sample = next(iter(results.values()))["body"]
        print(json.dumps({
            "event_id": sample["event_id"],
            "event_type": sample["event_type"],
            "deal_id": sample["deal_id"],
            "buyer": sample["buyer"],
            "provider": sample["provider"],
            "delivered_to": {
                name: {"stored": result["created"], "indexed_under": result["counterparty"]}
                for name, result in results.items()
            },
            "note": "both memories receive the same neutral fact; each draws its own conclusion",
        }, indent=2, sort_keys=True))
        return 0
    raise AssertionError("unreachable")

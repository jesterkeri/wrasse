"""Reading `policy.json` as untrusted input, even though we wrote it.

A document produced hours ago on one machine decides what a key signs on another. Between
those two moments it is a file on disk that anything can edit, so it is checked the way any
external input would be: bounded size, exact shape, no unknown fields, and every derived
value recomputed rather than believed.

It also carries the identity that makes a retry a retry. `request_id` is minted before any
transaction exists, and `intent_id` pairs it with the explicitly chosen profile. Neither
depends on the terms, which move between attempts because the acceptance deadline is
re-derived from chain time immediately before signing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web3 import Web3

from .engine import PROFILES
from .policy_hash import (
    ENGINE_VERSION,
    MAX_DURATION,
    MAX_PROVIDER_BOND_BPS,
    PolicyPreimage,
    canonical_event_id,
    evidence_hash,
    policy_hash,
)

#: This is a small document describing one quote. Anything larger is not one.
MAX_POLICY_BYTES = 1 << 20

SCHEMA_VERSION = 2

_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")

_TOP_LEVEL = {
    "schema_version",
    "request_id",
    "chain_id",
    "contract_address",
    "engine_version",
    "executability",
    "buyer",
    "provider",
}

_BUYER_KEYS = {"address", "counterparty", "verdict", "cold_start", "recalled_evidence", "profiles"}
_PROVIDER_KEYS = {
    "address", "counterparty", "verdict", "cold_start", "recalled_evidence", "persona", "terms",
}
_PROFILE_KEYS = {"terms", "policy_preimage", "policy_hash"}
_TERMS_KEYS = {
    "price_wei", "provider_bond_bps", "service_window", "payout_delay", "risk",
    "used_evidence_ids",
}
_PROVIDER_TERMS_KEYS = {"price_wei", "payout_delay", "risk", "used_evidence_ids"}
_PERSONA_KEYS = {"name", "cashflow_sensitivity", "commitment"}
_EXECUTABILITY_KEYS = {
    "basis", "reference_timestamp", "chain", "inclusion_margin_seconds",
    "observed_lag_seconds", "executable", "note",
}
_CHAIN_KEYS = {"chain_id", "block_number", "block_timestamp"}

_PREIMAGE_KEYS = {
    "buyer",
    "provider",
    "price",
    "bond_bps",
    "accept_by",
    "service_window",
    "payout_delay",
    "engine_version",
    "buyer_evidence_hash",
    "provider_evidence_hash",
}


class PolicyDocumentError(ValueError):
    """The document is not a policy this build is willing to sign against."""


class PolicyNotBound(PolicyDocumentError):
    """The document was written before there was a deployment to bind it to.

    Quoting terms does not require a contract; signing against them does. Binding is a
    deliberate act that mints a new `request_id`, because a rebound quote is a new action.
    """


@dataclass(frozen=True)
class ValidatedPolicy:
    """One profile of one document, checked and safe to build a transaction from."""

    request_id: str
    profile: str
    chain_id: int
    contract_address: str
    engine_version: str
    buyer: str
    provider: str
    price_wei: int
    bond_bps: int
    service_window: int
    payout_delay: int
    buyer_evidence_hash: str
    provider_evidence_hash: str
    quoted_accept_by: int
    quoted_policy_hash: str

    @property
    def intent_id(self) -> str:
        """Stable action identity: the quote, and the profile deliberately chosen from it."""

        return f"{self.request_id}:{self.profile}"

    def preimage_for(self, accept_by: int) -> PolicyPreimage:
        """The commitment as it would be with a freshly derived deadline."""

        return PolicyPreimage(
            buyer=self.buyer,
            provider=self.provider,
            price=self.price_wei,
            bond_bps=self.bond_bps,
            accept_by=accept_by,
            service_window=self.service_window,
            payout_delay=self.payout_delay,
            engine_version=self.engine_version,
            buyer_evidence_hash=self.buyer_evidence_hash,
            provider_evidence_hash=self.provider_evidence_hash,
        )


def _exact_keys(value: Any, expected: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyDocumentError(f"{where} is not an object")
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise PolicyDocumentError(f"{where} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise PolicyDocumentError(f"{where} is missing: {', '.join(missing)}")
    return value


def _bounded_int(value: Any, name: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyDocumentError(f"{name} is not an integer")
    if not low <= value <= high:
        raise PolicyDocumentError(f"{name} is outside {low}..{high}")
    return value


def _address(value: Any, name: str) -> str:
    if not isinstance(value, str) or not Web3.is_address(value):
        raise PolicyDocumentError(f"{name} is not an EVM address")
    if int(value, 16) == 0:
        raise PolicyDocumentError(f"{name} is the zero address")
    return Web3.to_checksum_address(value)


def _same_address(left: str, right: str) -> bool:
    return Web3.to_checksum_address(left) == Web3.to_checksum_address(right)


def load_policy(
    path: Path | str,
    *,
    profile: str,
    chain_id: int,
    contract_address: str,
    buyer: str,
    provider: str,
) -> ValidatedPolicy:
    """Validate a bilateral policy document against this deployment and this wallet."""

    path = Path(path)
    size = path.stat().st_size
    if size > MAX_POLICY_BYTES:
        raise PolicyDocumentError(f"{path} is {size} bytes, over the {MAX_POLICY_BYTES} cap")

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PolicyDocumentError(f"{path} is not valid JSON: {error}") from error

    document = _exact_keys(document, _TOP_LEVEL, "the policy document")

    if document["schema_version"] != SCHEMA_VERSION:
        raise PolicyDocumentError(
            f"schema_version {document['schema_version']!r} is not {SCHEMA_VERSION}; "
            "this build cannot know which fields it is reading"
        )

    request_id = document["request_id"]
    if not isinstance(request_id, str) or not _REQUEST_ID.match(request_id):
        raise PolicyDocumentError("request_id is not 32 lowercase hex characters")

    if document["contract_address"] is None:
        raise PolicyNotBound(
            f"{path} was written before a deployment existed. Bind it with `wrasse "
            "rebind-policy`, which mints a new request_id because a rebound quote is a new "
            "action."
        )
    if document["chain_id"] != chain_id:
        raise PolicyDocumentError(
            f"the document targets chain {document['chain_id']}, this run targets {chain_id}"
        )
    if not _same_address(document["contract_address"], contract_address):
        raise PolicyDocumentError(
            f"the document targets {document['contract_address']}, "
            f"this run targets {contract_address}"
        )
    if document["engine_version"] != ENGINE_VERSION:
        raise PolicyDocumentError(
            f"the document was written by {document['engine_version']}, this build is "
            f"{ENGINE_VERSION}; the version is hashed into the commitment"
        )

    _check_executability(document["executability"])

    buyer_side = _check_side(document["buyer"], _BUYER_KEYS, "buyer", buyer, provider)
    provider_side = _check_side(document["provider"], _PROVIDER_KEYS, "provider", provider, buyer)

    persona = _exact_keys(provider_side["persona"], _PERSONA_KEYS, "the provider persona")
    if not isinstance(persona["commitment"], str) or len(persona["commitment"]) != 64:
        raise PolicyDocumentError("the persona commitment is not a sha256 digest")

    provider_terms = _exact_keys(
        provider_side["terms"], _PROVIDER_TERMS_KEYS, "the provider's terms"
    )
    provider_used = _check_used(provider_terms, provider_side, "provider")

    profiles = buyer_side["profiles"]
    if not isinstance(profiles, dict) or profile not in profiles:
        available = ", ".join(sorted(profiles)) if isinstance(profiles, dict) else "none"
        raise PolicyDocumentError(f"no profile named {profile!r}; the document has: {available}")
    if profile not in PROFILES:
        raise PolicyDocumentError(
            f"{profile!r} is not a profile this engine produces: {', '.join(sorted(PROFILES))}"
        )

    # Every profile is validated, not only the one being signed. A side-by-side document whose
    # unselected columns are unchecked can show a reader whatever it likes.
    validated = None
    for name in sorted(profiles):
        if name not in PROFILES:
            raise PolicyDocumentError(
                f"{name!r} is not a profile this engine produces: {', '.join(sorted(PROFILES))}"
            )
        candidate = _check_profile(
            profiles[name], name, document, buyer_side, provider_terms, provider_used,
            request_id=request_id, chain_id=chain_id, contract_address=contract_address,
        )
        if name == profile:
            validated = candidate

    assert validated is not None
    return validated


def _check_executability(executability: Any) -> None:
    """The label a reader trusts to know whether this was checked against a chain at all."""

    executability = _exact_keys(executability, _EXECUTABILITY_KEYS, "executability")
    basis = executability["basis"]
    if basis not in {"supplied-reference", "chain-observation"}:
        raise PolicyDocumentError(f"executability basis {basis!r} is not one this build writes")

    live = basis == "chain-observation"
    if executability["executable"] is not live:
        raise PolicyDocumentError(
            f"a {basis} quote cannot be marked executable={executability['executable']}"
        )
    if live:
        _exact_keys(executability["chain"], _CHAIN_KEYS, "the observed chain")
        for field in ("inclusion_margin_seconds", "observed_lag_seconds"):
            _bounded_int(executability[field], field, low=0, high=2**32)
    elif executability["chain"] is not None:
        raise PolicyDocumentError("a supplied-reference quote must not carry chain observations")
    _bounded_int(executability["reference_timestamp"], "reference_timestamp", low=0, high=2**64 - 1)


def _check_side(side: Any, keys: set[str], role: str, owner: str, counterparty: str) -> dict[str, Any]:
    side = _exact_keys(side, keys, f"the {role} half")
    if not _same_address(side["address"], owner):
        raise PolicyDocumentError(
            f"the document names {side['address']} as the {role}, this run uses {owner}"
        )
    if not _same_address(side["counterparty"], counterparty):
        raise PolicyDocumentError(
            f"the {role} half names counterparty {side['counterparty']}, this run targets "
            f"{counterparty}"
        )
    if not isinstance(side["cold_start"], bool):
        raise PolicyDocumentError(f"the {role} cold_start is not a boolean")
    if not isinstance(side["verdict"], str):
        raise PolicyDocumentError(f"the {role} verdict is not a string")
    if not isinstance(side["recalled_evidence"], list):
        raise PolicyDocumentError(f"the {role} recalled_evidence is not a list")
    for index, item in enumerate(side["recalled_evidence"]):
        if not isinstance(item, dict) or "event_id" not in item:
            raise PolicyDocumentError(f"{role} recalled_evidence[{index}] has no event_id")
    return side


def _check_used(terms: dict[str, Any], side: dict[str, Any], role: str) -> list[str]:
    """What a side used has to be part of what it holds, or the receipt cites nothing real."""

    used = terms["used_evidence_ids"]
    if not isinstance(used, list):
        raise PolicyDocumentError(f"the {role} used_evidence_ids is not a list")

    recalled = {canonical_event_id(str(item["event_id"])) for item in side["recalled_evidence"]}
    canonical = [canonical_event_id(str(item)) for item in used]
    stray = sorted(set(canonical) - recalled)
    if stray:
        raise PolicyDocumentError(
            f"the {role} used evidence it does not hold: {', '.join(stray)}"
        )
    return canonical


def _check_profile(
    chosen: Any, name: str, document: dict[str, Any], buyer_side: dict[str, Any],
    provider_terms: dict[str, Any], provider_used: list[str], *,
    request_id: str, chain_id: int, contract_address: str,
) -> ValidatedPolicy:
    chosen = _exact_keys(chosen, _PROFILE_KEYS, f"profile {name!r}")
    preimage = _exact_keys(chosen["policy_preimage"], _PREIMAGE_KEYS, f"profile {name!r} preimage")

    document_buyer = _address(preimage["buyer"], "buyer")
    document_provider = _address(preimage["provider"], "provider")
    if not _same_address(document_buyer, buyer_side["address"]):
        raise PolicyDocumentError(f"profile {name!r} commits to a different buyer")
    if not _same_address(document_provider, buyer_side["counterparty"]):
        raise PolicyDocumentError(f"profile {name!r} commits to a different provider")
    if preimage["engine_version"] != document["engine_version"]:
        raise PolicyDocumentError("the preimage and the document disagree on engine_version")

    price = _bounded_int(preimage["price"], "price", low=1, high=2**256 - 1)
    bond_bps = _bounded_int(preimage["bond_bps"], "bond_bps", low=0, high=MAX_PROVIDER_BOND_BPS)
    service_window = _bounded_int(preimage["service_window"], "service_window", low=1, high=MAX_DURATION)
    payout_delay = _bounded_int(preimage["payout_delay"], "payout_delay", low=1, high=MAX_DURATION)
    accept_by = _bounded_int(preimage["accept_by"], "accept_by", low=0, high=2**64 - 1)

    validated = ValidatedPolicy(
        request_id=request_id,
        profile=name,
        chain_id=chain_id,
        contract_address=Web3.to_checksum_address(contract_address),
        engine_version=document["engine_version"],
        buyer=document_buyer,
        provider=document_provider,
        price_wei=price,
        bond_bps=bond_bps,
        service_window=service_window,
        payout_delay=payout_delay,
        buyer_evidence_hash=preimage["buyer_evidence_hash"],
        provider_evidence_hash=preimage["provider_evidence_hash"],
        quoted_accept_by=accept_by,
        quoted_policy_hash=chosen["policy_hash"],
    )

    recomputed = policy_hash(validated.preimage_for(accept_by))
    if recomputed != chosen["policy_hash"]:
        raise PolicyDocumentError(
            f"profile {name!r} quotes {chosen['policy_hash']} but its own fields hash to {recomputed}"
        )

    buyer_used = _check_used(
        _exact_keys(chosen["terms"], _TERMS_KEYS, f"profile {name!r} terms"), buyer_side, "buyer"
    )
    _check_terms(chosen["terms"], validated, name)

    # Each side commits to what moved its own numbers. A hash over everything recalled would
    # describe the reading rather than the reasoning.
    for label, used, committed in (
        ("buyer", buyer_used, validated.buyer_evidence_hash),
        ("provider", provider_used, validated.provider_evidence_hash),
    ):
        expected = evidence_hash(used)
        if expected != committed:
            raise PolicyDocumentError(
                f"profile {name!r} commits {label} evidence {committed}, but the receipts it "
                f"says it used hash to {expected}"
            )

    for field, displayed, committed in (
        ("price_wei", provider_terms["price_wei"], price),
        ("payout_delay", provider_terms["payout_delay"], payout_delay),
    ):
        if displayed != committed:
            raise PolicyDocumentError(
                f"the provider displays {field}={displayed!r} but profile {name!r} commits "
                f"{committed!r}"
            )
    return validated


def _check_terms(terms: dict[str, Any], validated: ValidatedPolicy, profile: str) -> None:
    """The numbers a human reads must be the numbers the signature commits to.

    `terms` is the displayed half of the document and `policy_preimage` is the signed half.
    Validating only the signed half would let a file show a low price beside a commitment that
    funds a high one, which is precisely the substitution an explainable receipt must rule out.
    """

    for name, displayed, committed in (
        ("price_wei", terms["price_wei"], validated.price_wei),
        ("provider_bond_bps", terms["provider_bond_bps"], validated.bond_bps),
        ("service_window", terms["service_window"], validated.service_window),
        ("payout_delay", terms["payout_delay"], validated.payout_delay),
    ):
        if displayed != committed:
            raise PolicyDocumentError(
                f"profile {profile!r} displays {name}={displayed!r} but commits to {committed!r}"
            )
    if not isinstance(terms["risk"], str):
        raise PolicyDocumentError("risk is not a string")

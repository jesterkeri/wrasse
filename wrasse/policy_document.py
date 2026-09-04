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

from .policy_hash import (
    MAX_DURATION,
    MAX_PROVIDER_BOND_BPS,
    PolicyPreimage,
    canonical_event_id,
    evidence_hash,
    policy_hash,
)

#: This is a small document describing one quote. Anything larger is not one.
MAX_POLICY_BYTES = 1 << 20

SCHEMA_VERSION = 1

_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")

_TOP_LEVEL = {
    "schema_version",
    "request_id",
    "chain_id",
    "contract_address",
    "engine_version",
    "counterparty",
    "memory_verdict",
    "cold_start",
    "executability",
    "profiles",
    "evidence",
}

_PROFILE_KEYS = {"terms", "policy_preimage", "policy_hash"}

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
    """Validate one profile of a policy document against this deployment and this wallet."""

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

    profiles = document["profiles"]
    if not isinstance(profiles, dict) or profile not in profiles:
        available = ", ".join(sorted(profiles)) if isinstance(profiles, dict) else "none"
        raise PolicyDocumentError(f"no profile named {profile!r}; the document has: {available}")

    chosen = _exact_keys(profiles[profile], _PROFILE_KEYS, f"profile {profile!r}")
    preimage = _exact_keys(chosen["policy_preimage"], _PREIMAGE_KEYS, f"profile {profile!r} preimage")

    document_buyer = _address(preimage["buyer"], "buyer")
    document_provider = _address(preimage["provider"], "provider")
    if not _same_address(document_buyer, buyer):
        raise PolicyDocumentError(
            f"the document names buyer {document_buyer}, but the signing wallet is {buyer}"
        )
    if not _same_address(document_provider, provider):
        raise PolicyDocumentError(
            f"the document names provider {document_provider}, configured provider is {provider}"
        )

    if preimage["engine_version"] != document["engine_version"]:
        raise PolicyDocumentError("the preimage and the document disagree on engine_version")

    price = _bounded_int(preimage["price"], "price", low=1, high=2**256 - 1)
    bond_bps = _bounded_int(preimage["bond_bps"], "bond_bps", low=0, high=MAX_PROVIDER_BOND_BPS)
    service_window = _bounded_int(preimage["service_window"], "service_window", low=1, high=MAX_DURATION)
    payout_delay = _bounded_int(preimage["payout_delay"], "payout_delay", low=1, high=MAX_DURATION)
    accept_by = _bounded_int(preimage["accept_by"], "accept_by", low=0, high=2**64 - 1)

    validated = ValidatedPolicy(
        request_id=request_id,
        profile=profile,
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

    # Recompute rather than believe. A quoted hash is a claim about the fields beside it.
    recomputed = policy_hash(validated.preimage_for(accept_by))
    if recomputed != chosen["policy_hash"]:
        raise PolicyDocumentError(
            f"profile {profile!r} quotes {chosen['policy_hash']} but its own fields hash to {recomputed}"
        )

    _check_evidence(document, validated)
    return validated


def _check_evidence(document: dict[str, Any], validated: ValidatedPolicy) -> None:
    """The buyer's evidence commitment must cover exactly the receipts the document lists."""

    evidence = document["evidence"]
    if not isinstance(evidence, list):
        raise PolicyDocumentError("evidence is not a list")

    identifiers = []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict) or "event_id" not in item:
            raise PolicyDocumentError(f"evidence[{index}] has no event_id")
        identifiers.append(canonical_event_id(str(item["event_id"])))

    expected = evidence_hash(identifiers)
    if expected != validated.buyer_evidence_hash:
        raise PolicyDocumentError(
            f"the buyer evidence commitment is {validated.buyer_evidence_hash}, but the listed "
            f"receipts hash to {expected}"
        )

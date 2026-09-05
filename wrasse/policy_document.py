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

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web3 import Web3

from .constants import MANIFEST_DIGEST, canonical_manifest
from .negotiation import CONCESSION, MEMORY, RULE, Position, bond_is_collectible, settle
from .negotiation import TERMS as NEGOTIATED_TERMS
from .negotiation import price_wei as negotiation_price_wei
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

SCHEMA_VERSION = 3

_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")

_TOP_LEVEL = {
    "schema_version",
    "request_id",
    "chain_id",
    "contract_address",
    "engine_version",
    "engine",
    "executability",
    "buyer",
    "provider",
}

_ENGINE_KEYS = {"negotiation_manifest"}

_BUYER_KEYS = {"address", "counterparty", "verdict", "cold_start", "recalled_evidence", "profiles"}
_PROVIDER_KEYS = {
    "address", "counterparty", "verdict", "cold_start", "recalled_evidence", "persona",
}

#: A profile that reached agreement, and one that did not. Two exact shapes rather than one
#: shape with a flag: a refused profile **omits** the executable fields entirely, so there is
#: nothing to sign and nothing to misread. A profile carrying both a refusal and a policy hash
#: is a malformed document, not a choice.
_AGREED_PROFILE_KEYS = {
    "baseline", "buyer", "provider", "settlement", "terms", "policy_preimage", "policy_hash",
}
_REFUSED_PROFILE_KEYS = {"baseline", "buyer", "provider", "settlement"}

_BASELINE_KEYS = {"price_wei", "provider_bond_bps", "service_window", "payout_delay"}
_TERMS_KEYS = {"price_wei", "provider_bond_bps", "service_window", "payout_delay"}

_BUYER_HALF_KEYS = {"proposes", "limits", "risk", "used_evidence_ids"}
_BUYER_PROPOSES_KEYS = {"provider_bond_bps", "service_window"}
_BUYER_LIMITS_KEYS = {"max_price_bps", "min_payout_delay"}

_PROVIDER_HALF_KEYS = {"proposes", "limits", "walkaway", "risk", "used_evidence_ids"}
_PROVIDER_PROPOSES_KEYS = {"price_bps", "payout_delay"}
_PROVIDER_LIMITS_KEYS = {"max_bond_bps", "min_service_window"}
_PROVIDER_WALKAWAY_KEYS = {"price_floor_bps"}

_AGREED_SETTLEMENT_KEYS = {"agreed", "moves"}
_REFUSED_SETTLEMENT_KEYS = {"agreed", "failed_on", "gap"}
_MOVE_KEYS = {"term", "from", "to", "kind", "because"}

#: The three ways a number can move, and they are not interchangeable. A memory adjustment
#: cites receipts, a concession cites the counterparty's published limit, a rule cites the
#: constant by name. Collapsing them would let a constant masquerade as evidence.
_MOVE_KINDS = {MEMORY, CONCESSION, RULE}
_PERSONA_KEYS = {"name", "cashflow_sensitivity", "commitment"}
_EXECUTABILITY_KEYS = {
    "basis", "reference_timestamp", "chain", "inclusion_margin_seconds",
    "observed_lag_seconds", "executable", "note",
}
_CHAIN_KEYS = {"chain_id", "block_number", "block_timestamp"}

#: The two sentences this build writes about what an executability check was worth. A closed
#: set, because it is the line a reader trusts to know whether any of this was held against a
#: chain, and free text there is a place to write something flattering.
_NOTES = {
    'Judged against a supplied time, not against Base. Reproducible, but not a live quote: re-derive the deadline from chain time before signing.',
    "Judged against the latest observed Base block, with an inclusion margin that already absorbs the node's observed lag. Re-validate immediately before signing; inclusion time is not guaranteed.",
}

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
    #: The document as written, carried out whole so the signer can hold every part of it
    #: against the stores. Internal consistency proves a document is not self-contradictory; it
    #: cannot prove any of it came from anyone's memory, and the half a judge reads is a
    #: different object from the half a signature covers.
    document: dict[str, Any]

    @property
    def recalled_evidence(self) -> dict[str, tuple[dict[str, Any], ...]]:
        return {
            side: tuple(self.document[side]["recalled_evidence"])
            for side in ("buyer", "provider")
        }

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


_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: Every verdict `WrasseStore.recall` can produce. Anything else is a label somebody invented.
_VERDICTS = {"match", "no_match", "empty_store"}


def _bounded_decimal(value: Any, name: str) -> Decimal:
    """A score a reader is shown has to be a number between nothing and everything."""

    if not isinstance(value, str):
        raise PolicyDocumentError(f"{name} is not a string")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise PolicyDocumentError(f"{name} is not a decimal") from error
    # `Decimal("NaN")` parses. Comparing it then raises `InvalidOperation` out of this
    # function, so an untrusted document produced a traceback where it was promised a
    # validation refusal. Finiteness is a property of the number, so it is checked as one.
    if not number.is_finite():
        raise PolicyDocumentError(f"{name} is {value!r}, which is not a finite number")
    if not Decimal("0") <= number <= Decimal("1"):
        raise PolicyDocumentError(f"{name} is outside 0..1")
    return number


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


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse a JSON object that says the same thing twice.

    `json.loads` keeps the last of a repeated key and says nothing, so a document carrying
    both `"price_wei": 1` and the real price parses to the real price here while a reader, or
    any first-wins parser, sees the other one. Every check downstream then compares the value
    this build happened to keep, which is why canonical encoding could not see it: by the time
    the comparison runs, the ambiguity has already been resolved and discarded.

    A document that cannot be read the same way twice is not a receipt.
    """

    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise PolicyDocumentError(
                f"the document names {key!r} twice in one object. A file that reads differently "
                "depending on the parser cannot explain anything."
            )
        seen.add(key)
    return dict(pairs)


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
        document = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys
        )
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
    if not _SHA256.match(str(persona["commitment"])):
        raise PolicyDocumentError("the persona commitment is not a sha256 digest")
    _bounded_decimal(persona["cashflow_sensitivity"], "cashflow_sensitivity")

    # Compared by canonical bytes, never by Python equality. `5000.0 == 5000` in Python and
    # not in JSON, so an equality check accepts a manifest that hashes to something else
    # entirely, and a reader following the README recomputes a digest the validator did not
    # see. The digest is what the version claims to carry, so the digest is what is compared.
    engine = _exact_keys(document["engine"], _ENGINE_KEYS, "the engine block")
    published = json.dumps(
        engine["negotiation_manifest"], sort_keys=True, separators=(",", ":")
    )
    if published != canonical_manifest():
        digest = hashlib.sha256(published.encode("utf-8")).hexdigest()
        raise PolicyDocumentError(
            "the published negotiation manifest is not the one this build hashes into "
            f"engine_version: it digests to {digest[:12]}, this build to "
            f"{MANIFEST_DIGEST[:12]}. The constants that decide a term are covered by the "
            "same commitment as the term, and this document's are not."
        )

    profiles = buyer_side["profiles"]
    if not isinstance(profiles, dict):
        raise PolicyDocumentError("the buyer's profiles are not an object")
    # Exactly the set this engine publishes. A subset is how a refusal disappears: drop the
    # profile that had no overlap and the document reads as though every choice worked.
    if set(profiles) != set(PROFILES):
        missing = sorted(set(PROFILES) - set(profiles))
        extra = sorted(set(profiles) - set(PROFILES))
        raise PolicyDocumentError(
            f"the document offers profiles {sorted(profiles)}; this engine produces "
            f"{sorted(PROFILES)}. Missing: {missing or 'none'}. Unknown: {extra or 'none'}. "
            "A document may not add, drop or rename the choices a reader is given."
        )
    if profile not in PROFILES:
        raise PolicyDocumentError(
            f"{profile!r} is not a profile this engine produces: {', '.join(sorted(PROFILES))}"
        )

    # Every profile is validated, not only the one being signed. A side-by-side document whose
    # unselected columns are unchecked can show a reader whatever it likes.
    validated = None
    for name in sorted(profiles):
        candidate = _check_profile(
            profiles[name], name, document, buyer_side, provider_side,
            request_id=request_id, chain_id=chain_id, contract_address=contract_address,
        )
        if name == profile:
            validated = candidate

    if validated is None:
        raise PolicyDocumentError(
            f"profile {profile!r} did not reach agreement: "
            f"{profiles[profile]['settlement']['failed_on']} had no overlap by "
            f"{profiles[profile]['settlement']['gap']}. There are no terms to sign."
        )
    return validated


def _check_executability(executability: Any) -> None:
    """The label a reader trusts to know whether this was checked against a chain at all."""

    executability = _exact_keys(executability, _EXECUTABILITY_KEYS, "executability")
    basis = executability["basis"]
    if basis not in {"supplied-reference", "chain-observation"}:
        raise PolicyDocumentError(f"executability basis {basis!r} is not one this build writes")

    live = basis == "chain-observation"
    if executability["note"] not in _NOTES:
        raise PolicyDocumentError(
            "the executability note is not one this build writes. It is the sentence a reader "
            "is given about whether any of this was checked against a chain, so it is a closed "
            "set rather than free text."
        )
    if executability["executable"] is not live:
        raise PolicyDocumentError(
            f"a {basis} quote cannot be marked executable={executability['executable']}"
        )
    if live:
        chain = _exact_keys(executability["chain"], _CHAIN_KEYS, "the observed chain")
        # The nested fields too. Checking the key set and leaving the values alone let a
        # document carry a string block number and a null timestamp under a label claiming
        # they came off Base.
        _bounded_int(chain["chain_id"], "the observed chain_id", low=1, high=2**64 - 1)
        _bounded_int(chain["block_number"], "the observed block_number", low=0, high=2**64 - 1)
        _bounded_int(
            chain["block_timestamp"], "the observed block_timestamp", low=0, high=2**64 - 1
        )
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
    if side["verdict"] not in _VERDICTS:
        raise PolicyDocumentError(
            f"the {role} verdict {side['verdict']!r} is not one this build produces"
        )
    if not isinstance(side["recalled_evidence"], list):
        raise PolicyDocumentError(f"the {role} recalled_evidence is not a list")

    # A document showing evidence beside "I remember nothing" is telling a reader two
    # different things at once, and only one of them can be true.
    holds = bool(side["recalled_evidence"])
    if side["cold_start"] is holds:
        raise PolicyDocumentError(
            f"the {role} says cold_start={side['cold_start']} while listing "
            f"{len(side['recalled_evidence'])} receipts"
        )
    if (side["verdict"] == "match") is not holds:
        raise PolicyDocumentError(
            f"the {role} verdict {side['verdict']!r} does not match the evidence it lists"
        )
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


def _check_settlement(settlement: Any, name: str) -> bool:
    """The settlement block, and whether this profile is executable at all."""

    if not isinstance(settlement, dict) or not isinstance(settlement.get("agreed"), bool):
        raise PolicyDocumentError(f"profile {name!r} has no readable settlement")
    agreed = settlement["agreed"]
    keys = _AGREED_SETTLEMENT_KEYS if agreed else _REFUSED_SETTLEMENT_KEYS
    settlement = _exact_keys(settlement, keys, f"profile {name!r} settlement")

    if not agreed:
        if settlement["failed_on"] not in NEGOTIATED_TERMS:
            raise PolicyDocumentError(
                f"profile {name!r} says it failed on {settlement['failed_on']!r}, which is not "
                "a term this build negotiates"
            )
        _bounded_int(settlement["gap"], f"profile {name!r} gap", low=1, high=2**256 - 1)
        return False

    if not isinstance(settlement["moves"], list):
        raise PolicyDocumentError(f"profile {name!r} settlement moves is not a list")
    for move in settlement["moves"]:
        move = _exact_keys(move, _MOVE_KEYS, f"a move in profile {name!r}")
        if move["term"] not in NEGOTIATED_TERMS:
            raise PolicyDocumentError(f"profile {name!r} moves {move['term']!r}, which is not a term")
        if move["kind"] not in _MOVE_KINDS:
            raise PolicyDocumentError(
                f"profile {name!r} explains a move as {move['kind']!r}, which is not one of "
                f"{', '.join(sorted(_MOVE_KINDS))}. A movement's cause is a receipt, a "
                "counterparty's limit or a named rule, and those are not interchangeable."
            )
        if not isinstance(move["because"], str) or not move["because"]:
            raise PolicyDocumentError(f"profile {name!r} moves {move['term']!r} for no stated reason")
        if move["from"] == move["to"]:
            raise PolicyDocumentError(
                f"profile {name!r} reports {move['term']!r} as moved from {move['from']!r} to "
                "itself, which is not a movement"
            )
    return True


def _check_positions(body: dict[str, Any], name: str) -> dict[str, Position]:
    """Both sides' published numbers, strictly, and the positions they add up to.

    Shape-checking was not enough. A reader is told they can recompute the settlement from
    this block, and a block whose proposals are strings supports no such thing: the document
    would carry a validator-approved account of a negotiation that never happened, beside a
    perfectly genuine signed preimage.
    """

    baseline = _exact_keys(body["baseline"], _BASELINE_KEYS, f"profile {name!r} baseline")
    for field, high in (
        ("price_wei", 2**256 - 1),
        ("provider_bond_bps", MAX_PROVIDER_BOND_BPS),
        ("service_window", MAX_DURATION),
        ("payout_delay", MAX_DURATION),
    ):
        _bounded_int(baseline[field], f"profile {name!r} baseline {field}", low=1, high=high)

    buyer = _exact_keys(body["buyer"], _BUYER_HALF_KEYS, f"profile {name!r} buyer half")
    proposes = _exact_keys(buyer["proposes"], _BUYER_PROPOSES_KEYS, f"profile {name!r} buyer proposals")
    limits = _exact_keys(buyer["limits"], _BUYER_LIMITS_KEYS, f"profile {name!r} buyer limits")
    _bounded_decimal(buyer["risk"], f"profile {name!r} buyer risk")

    provider = _exact_keys(body["provider"], _PROVIDER_HALF_KEYS, f"profile {name!r} provider half")
    offers = _exact_keys(provider["proposes"], _PROVIDER_PROPOSES_KEYS, f"profile {name!r} provider proposals")
    caps = _exact_keys(provider["limits"], _PROVIDER_LIMITS_KEYS, f"profile {name!r} provider limits")
    walkaway = _exact_keys(provider["walkaway"], _PROVIDER_WALKAWAY_KEYS, f"profile {name!r} provider walkaway")
    _bounded_decimal(provider["risk"], f"profile {name!r} provider risk")

    numbers = {
        "buyer bond proposal": (proposes["provider_bond_bps"], MAX_PROVIDER_BOND_BPS, 0),
        "buyer window proposal": (proposes["service_window"], MAX_DURATION, 1),
        "buyer max price": (limits["max_price_bps"], 2**32, 1),
        "buyer min payout delay": (limits["min_payout_delay"], MAX_DURATION, 1),
        "provider price proposal": (offers["price_bps"], 2**32, 1),
        "provider payout delay proposal": (offers["payout_delay"], MAX_DURATION, 1),
        "provider max bond": (caps["max_bond_bps"], MAX_PROVIDER_BOND_BPS, 0),
        "provider min window": (caps["min_service_window"], MAX_DURATION, 1),
        "provider price floor": (walkaway["price_floor_bps"], 2**32, 1),
    }
    for label, (value, high, low) in numbers.items():
        _bounded_int(value, f"profile {name!r} {label}", low=low, high=high)

    # The walk-aways the settlement is stated over. Three are derived, in the open, from the
    # baseline and the side's own proposal: a side concedes back to what it would have asked a
    # stranger and never past a number it offered itself. The fourth, price, is published,
    # because the provider concedes only part of the way back and that is what makes a refusal
    # possible at all.
    return {
        "provider_bond_bps": Position(
            proposal=proposes["provider_bond_bps"],
            limit=caps["max_bond_bps"],
            walkaway=min(baseline["provider_bond_bps"], proposes["provider_bond_bps"]),
            limit_name="provider_max_bond_bps",
            limit_kind=MEMORY,
        ),
        "service_window": Position(
            proposal=proposes["service_window"],
            limit=caps["min_service_window"],
            walkaway=max(baseline["service_window"], proposes["service_window"]),
            limit_name="provider_min_service_window",
            limit_kind=RULE,
        ),
        "price_bps": Position(
            proposal=offers["price_bps"],
            limit=limits["max_price_bps"],
            walkaway=walkaway["price_floor_bps"],
            limit_name="buyer_max_price_bps",
            limit_kind=RULE,
        ),
        "payout_delay": Position(
            proposal=offers["payout_delay"],
            limit=limits["min_payout_delay"],
            walkaway=max(baseline["payout_delay"], offers["payout_delay"]),
            limit_name="buyer_min_payout_delay",
            limit_kind=RULE,
        ),
    }


def _check_settlement_follows(body: dict[str, Any], positions: dict[str, Position], name: str) -> None:
    """Run the settlement again from the published numbers and require the same answer.

    This is the whole of Gate 7's standalone claim. Without it the document says a
    negotiation happened and nothing checks that it is the negotiation these numbers produce,
    so an edited account passes validation beside a genuine signed preimage and a judge is
    shown an explanation that is not the one that set the price.
    """

    recomputed = settle(positions).as_dict()
    published = body["settlement"]
    if recomputed != published:
        raise PolicyDocumentError(
            f"profile {name!r} publishes a settlement its own numbers do not produce. It says "
            f"{published!r}; those positions settle to {recomputed!r}."
        )

    if not published["agreed"]:
        return

    settled = settle(positions).terms
    baseline = body["baseline"]
    price = negotiation_price_wei(baseline["price_wei"], settled["price_bps"])
    expected = {
        "price_wei": price,
        "provider_bond_bps": settled["provider_bond_bps"],
        "service_window": settled["service_window"],
        "payout_delay": settled["payout_delay"],
    }
    displayed = _exact_keys(body["terms"], _TERMS_KEYS, f"profile {name!r} terms")
    if displayed != expected:
        raise PolicyDocumentError(
            f"profile {name!r} displays terms {displayed!r}, but its own settlement produces "
            f"{expected!r}"
        )
    if not bond_is_collectible(price, settled["provider_bond_bps"]):
        raise PolicyDocumentError(
            f"profile {name!r} settles {settled['provider_bond_bps']} bps of {price} wei, "
            "which rounds to a zero-wei bond the contract rejects"
        )


def _check_profile(
    chosen: Any, name: str, document: dict[str, Any], buyer_side: dict[str, Any],
    provider_side: dict[str, Any], *,
    request_id: str, chain_id: int, contract_address: str,
) -> ValidatedPolicy | None:
    """One profile. Returns `None` for a refused one, which has nothing to validate against.

    A refused profile is checked for shape and then left alone: it carries no terms, no
    preimage and no hash, by omission rather than by null, so there is nothing to sign and
    nothing a reader can mistake for an offer.
    """

    if not isinstance(chosen, dict) or "settlement" not in chosen:
        raise PolicyDocumentError(f"profile {name!r} has no settlement")
    agreed = _check_settlement(chosen["settlement"], name)
    keys = _AGREED_PROFILE_KEYS if agreed else _REFUSED_PROFILE_KEYS
    chosen = _exact_keys(chosen, keys, f"profile {name!r}")
    positions = _check_positions(chosen, name)
    _check_settlement_follows(chosen, positions, name)

    buyer_used = _check_used(chosen["buyer"], buyer_side, "buyer")
    provider_used = _check_used(chosen["provider"], provider_side, "provider")
    if not agreed:
        return None

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
        document=document,
    )

    recomputed = policy_hash(validated.preimage_for(accept_by))
    if recomputed != chosen["policy_hash"]:
        raise PolicyDocumentError(
            f"profile {name!r} quotes {chosen['policy_hash']} but its own fields hash to {recomputed}"
        )

    # `_check_settlement_follows` already bound the displayed terms to the settlement. This
    # binds them to the signature, which is the other half: the numbers a person reads must be
    # the numbers the key funds.
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
    # Risk moved to each side's own half in schema 3, because there are two of them and one
    # displayed number could only ever have been one side's.

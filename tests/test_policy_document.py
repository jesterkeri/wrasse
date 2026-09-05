"""`policy.json` is untrusted input, and this is where that claim is actually tested.

The document is written by this program and read back by a process that will sign against it.
Between those moments it is a file anything can edit. The half a human reads and the half a
signature commits to are separate objects in that file, so the substitution that matters most
is a document showing a small price beside a commitment that funds a large one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from web3 import Web3

from wrasse.cli import main
from wrasse.policy_document import (
    MAX_POLICY_BYTES,
    PolicyDocumentError,
    PolicyNotBound,
    load_policy,
)

CHAIN_ID = 84532
ESCROW = Web3.to_checksum_address("0x" + "22" * 20)
BUYER = Web3.to_checksum_address("0x30C95B7eb3E08F83992E803Be2A5AB0E0af93d22")
PROVIDER = Web3.to_checksum_address("0x3333333333333333333333333333333333333333")


@pytest.fixture
def document(tmp_path, monkeypatch, capsys) -> Path:
    """A genuine document, produced the way the demo produces one."""

    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", str(CHAIN_ID))
    path = tmp_path / "policy.json"
    assert main([
        "policy", PROVIDER, "--buyer", BUYER,
        "--accept-by", "1700003600", "--reference-timestamp", "1700000000",
        "--output", str(path),
    ]) == 0
    capsys.readouterr()
    return path


def _load(path: Path, **overrides):
    arguments = {
        "profile": "urgent",
        "chain_id": CHAIN_ID,
        "contract_address": ESCROW,
        "buyer": BUYER,
        "provider": PROVIDER,
    }
    arguments.update(overrides)
    return load_policy(path, **arguments)


def _rewrite(path: Path, mutate) -> Path:
    body = json.loads(path.read_text())
    mutate(body)
    path.write_text(json.dumps(body))
    return path


def test_a_genuine_document_validates(document):
    policy = _load(document)
    assert policy.intent_id.endswith(":urgent")
    assert policy.provider == PROVIDER
    assert policy.buyer == BUYER


def test_the_displayed_price_cannot_differ_from_the_committed_price(document):
    """The substitution this validation exists to stop.

    `terms` is what a person reads. `policy_preimage` is what the signature funds. A file
    that shows one and commits the other would be an explainable receipt for a deal that
    never happened.
    """

    def undercut(body):
        body["buyer"]["profiles"]["urgent"]["terms"]["price_wei"] = 1

    with pytest.raises(PolicyDocumentError, match="its own settlement produces"):
        _load(_rewrite(document, undercut))


@pytest.mark.parametrize("field", ["provider_bond_bps", "service_window"])
def test_every_displayed_term_is_bound_to_the_commitment(document, field):
    with pytest.raises(PolicyDocumentError, match="displays|settlement produces"):
        _load(_rewrite(document, lambda body: body["buyer"]["profiles"]["urgent"]["terms"].update({field: 7})))


def test_a_side_cannot_claim_to_have_used_evidence_it_does_not_hold(document):
    """A receipt citing a record the store never had explains nothing."""

    def relist(body):
        body["buyer"]["profiles"]["urgent"]["buyer"]["used_evidence_ids"] = ["0x" + "5c" * 32]

    with pytest.raises(PolicyDocumentError, match="used evidence it does not hold"):
        _load(_rewrite(document, relist))


def test_each_side_commits_to_the_evidence_it_actually_used(document):
    """The commitment describes the reasoning, not the reading.

    A cold start uses nothing, so its hash is the empty set. Planting a used id that does not
    hash to the commitment is the substitution this check exists for.
    """

    def plant(body):
        row = {"event_id": "0x" + "7a" * 32}
        body["buyer"]["recalled_evidence"] = [row]
        body["buyer"]["cold_start"] = False
        body["buyer"]["verdict"] = "match"
        body["buyer"]["profiles"]["urgent"]["buyer"]["used_evidence_ids"] = [row["event_id"]]

    with pytest.raises(PolicyDocumentError, match="commits buyer evidence"):
        _load(_rewrite(document, plant))


def test_a_quoted_hash_that_its_own_fields_do_not_produce_is_refused(document):
    def tamper(body):
        body["buyer"]["profiles"]["urgent"]["policy_preimage"]["payout_delay"] = 999

    with pytest.raises(PolicyDocumentError, match="hash to"):
        _load(_rewrite(document, tamper))


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"chain_id": 1}, "targets chain"),
        ({"contract_address": "0x" + "99" * 20}, "this run targets"),
        ({"buyer": "0x" + "88" * 20}, "this run uses"),
        ({"provider": "0x" + "77" * 20}, "counterparty"),
        ({"profile": "nonexistent"}, "not a profile this engine produces"),
    ],
)
def test_a_document_meant_for_something_else_is_refused(document, overrides, reason):
    with pytest.raises(PolicyDocumentError, match=reason):
        _load(document, **overrides)


def test_an_unknown_profile_name_is_refused_even_if_the_document_offers_it(document):
    """The document could offer a profile this engine has never produced."""

    def smuggle(body):
        body["buyer"]["profiles"]["freebie"] = body["buyer"]["profiles"]["urgent"]

    with pytest.raises(PolicyDocumentError, match="may not add, drop or rename"):
        _load(_rewrite(document, smuggle), profile="freebie")


@pytest.mark.parametrize(
    "where",
    ["top", "buyer", "profile", "preimage", "terms", "executability"],
)
def test_unknown_fields_are_refused_everywhere(document, where):
    def add(body):
        target = {
            "top": body,
            "buyer": body["buyer"],
            "profile": body["buyer"]["profiles"]["urgent"],
            "preimage": body["buyer"]["profiles"]["urgent"]["policy_preimage"],
            "terms": body["buyer"]["profiles"]["urgent"]["terms"],
            "executability": body["executability"],
        }[where]
        target["surprise"] = True

    with pytest.raises(PolicyDocumentError, match="unknown fields: surprise"):
        _load(_rewrite(document, add))


def test_a_missing_field_is_refused(document):
    with pytest.raises(PolicyDocumentError, match="is missing: engine_version"):
        _load(_rewrite(document, lambda body: body.pop("engine_version")))


def test_a_schema_version_this_build_does_not_know_is_refused(document):
    with pytest.raises(PolicyDocumentError, match="cannot know which fields"):
        _load(_rewrite(document, lambda body: body.update({"schema_version": 4})))


def test_the_schema_this_build_writes_is_the_schema_it_reads(document):
    """Two literals with no import between them drift, and the drift is invisible until a
    document this build wrote is refused by this build."""

    from wrasse.cli import POLICY_SCHEMA_VERSION
    from wrasse.policy_document import SCHEMA_VERSION

    assert POLICY_SCHEMA_VERSION is SCHEMA_VERSION
    assert json.loads(document.read_text())["schema_version"] == SCHEMA_VERSION


def test_an_engine_version_this_build_did_not_write_is_refused(document):
    """The version is hashed into the commitment, so it is a term, not a label."""

    def relabel(body):
        body["engine_version"] = "wrasse/0.2.0"
        body["buyer"]["profiles"]["urgent"]["policy_preimage"]["engine_version"] = "wrasse/0.2.0"

    with pytest.raises(PolicyDocumentError, match="this build is"):
        _load(_rewrite(document, relabel))


def test_a_request_id_that_is_not_a_request_id_is_refused(document):
    with pytest.raises(PolicyDocumentError, match="32 lowercase hex"):
        _load(_rewrite(document, lambda body: body.update({"request_id": "../../etc/passwd"})))


def test_a_provider_evidence_commitment_this_build_cannot_explain_is_refused(document):
    """Provider-side recall does not exist yet, so a non-empty commitment here is unexplainable."""

    def invent(body):
        body["buyer"]["profiles"]["urgent"]["policy_preimage"]["provider_evidence_hash"] = "0x" + "3c" * 32

    with pytest.raises(PolicyDocumentError, match="hash to|not the empty set"):
        _load(_rewrite(document, invent))


def test_a_document_written_before_any_deployment_says_how_to_bind_it(document):
    """This build cannot produce one any more, because a quote is bound to a deployment.

    Older documents exist, and the message has to point somewhere useful rather than simply
    refusing.
    """

    def unbind(body):
        body["contract_address"] = None

    with pytest.raises(PolicyNotBound, match="rebind-policy"):
        _load(_rewrite(document, unbind))


def test_rebinding_mints_a_new_identity(tmp_path, monkeypatch, capsys, document):
    """A rebound quote is a different action, so it must not inherit the old identity."""

    before = json.loads(document.read_text())["request_id"]
    bound = tmp_path / "bound.json"
    assert main(["rebind-policy", "--policy", str(document), "--output", str(bound)]) == 0
    capsys.readouterr()

    policy = _load(bound)
    assert policy.request_id != before
    assert policy.contract_address == ESCROW


def test_a_document_larger_than_the_cap_is_refused(document):
    def bloat(body):
        body["evidence"] = [{"event_id": "0x" + "11" * 32, "padding": "x" * 4096}] * 400

    with pytest.raises(PolicyDocumentError, match="over the"):
        _load(_rewrite(document, bloat))
    assert document.stat().st_size > MAX_POLICY_BYTES


def test_a_file_that_is_not_json_is_refused(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    with pytest.raises(PolicyDocumentError, match="not valid JSON"):
        _load(path)


# --------------------------------------------------------------------------------------
# The half a person reads has to be coherent, not merely well shaped
# --------------------------------------------------------------------------------------


def test_a_document_cannot_show_evidence_and_claim_to_remember_nothing(document):
    """Two statements, only one of which can be true, shown to the same reader."""

    def contradict(body):
        body["buyer"]["recalled_evidence"] = [{"event_id": "0x" + "7a" * 32}]

    with pytest.raises(PolicyDocumentError, match="cold_start=True while listing"):
        _load(_rewrite(document, contradict))


def test_a_verdict_this_build_never_produces_is_refused(document):
    with pytest.raises(PolicyDocumentError, match="not one this build produces"):
        _load(_rewrite(document, lambda body: body["buyer"].update({"verdict": "trusted"})))


@pytest.mark.parametrize("risk", ["not a number", "-0.5", "2", "1e400", "NaN", "Infinity", 0.5])
@pytest.mark.parametrize("side", ["buyer", "provider"])
def test_a_risk_a_reader_cannot_trust_is_refused(document, side, risk):
    """A score shown beside a price has to be a finite number between nothing and everything.

    Schema 3 moved risk out of the shared terms block and into each side's own half, because
    there are two of them and one displayed number could only ever have been one side's. This
    test kept writing to the old place, where the key is simply unknown, so it passed without
    ever reaching the bounds check. `NaN` parses as a `Decimal` and then raises out of the
    comparison, which is a traceback where a refusal was promised.
    """
    with pytest.raises(PolicyDocumentError, match="risk"):
        _load(_rewrite(
            document,
            lambda body: body["buyer"]["profiles"]["urgent"][side].update({"risk": risk}),
        ))


def test_a_persona_commitment_that_is_not_a_digest_is_refused(document):
    """A fake fingerprint beside a real signature is the forgery worth catching."""
    with pytest.raises(PolicyDocumentError, match="not a sha256 digest"):
        _load(_rewrite(
            document, lambda body: body["provider"]["persona"].update({"commitment": "trust me"})
        ))


# --------------------------------------------------------------------------------------
# The constants that decide a term, covered by the same commitment as the term
# --------------------------------------------------------------------------------------


def test_editing_a_negotiation_constant_changes_the_engine_version(monkeypatch):
    """The claim that would otherwise be release discipline dressed as proof.

    `engineVersionHash` is `keccak(ENGINE_VERSION)`. If that string is typed by a person,
    editing a constant changes every term and no hash, and saying the constants are committed
    is false. Deriving the string from a manifest of those constants makes it true.
    """

    import hashlib
    import importlib
    import json as _json

    from wrasse import constants

    before = constants.ENGINE_VERSION
    edited = _json.loads(constants.canonical_manifest())
    edited["max_bond_bps"] += 1
    digest = hashlib.sha256(
        _json.dumps(edited, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert digest[:12] != constants.MANIFEST_DIGEST[:12]
    assert before.endswith(constants.MANIFEST_DIGEST[:12])
    assert f"wrasse/0.2.0+{digest[:12]}" != before, (
        "a changed constant must produce a changed version, or the commitment covers nothing"
    )
    importlib.reload(constants)  # leave the module as it was found


def test_the_published_manifest_is_the_one_the_version_was_hashed_from(document):
    """A reader recomputes the digest rather than trusting it, so the manifest has to be real."""

    import hashlib

    from wrasse.constants import ENGINE_VERSION

    body = json.loads(document.read_text())
    published = body["engine"]["negotiation_manifest"]
    digest = hashlib.sha256(
        json.dumps(published, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert ENGINE_VERSION.endswith(digest[:12])
    assert body["engine_version"] == ENGINE_VERSION


def test_a_document_publishing_someone_elses_constants_is_refused(document):
    def swap(body):
        body["engine"]["negotiation_manifest"]["max_bond_bps"] = 9_999

    with pytest.raises(PolicyDocumentError, match="not the one this build hashes"):
        _load(_rewrite(document, swap))


def test_a_manifest_that_says_the_same_thing_in_different_bytes_is_refused(document):
    """`5000.0 == 5000` in Python and not in JSON.

    Comparing parsed objects accepted a manifest that hashes to something else entirely, so a
    reader following the README recomputed a digest the validator never saw. The digest is
    what the version claims to carry, so the digest is what is compared.
    """

    def retype(body):
        body["engine"]["negotiation_manifest"]["max_bond_bps"] = 5_000.0

    with pytest.raises(PolicyDocumentError, match="digests to"):
        _load(_rewrite(document, retype))


def test_every_constant_that_can_move_a_term_is_in_the_manifest():
    """The test of membership is not "is it a negotiation constant".

    It is: can editing this change a term? A constant that can and is missing makes the claim
    on `ENGINE_VERSION` false, which is worse than not making the claim at all. These are the
    ones a review found missing: the contract-mirroring bounds used by every clamp and by the
    price conversion, the two relevance multipliers, the risk clamp, and the rounding mode.
    """

    from wrasse.constants import NEGOTIATION_MANIFEST

    for required in (
        "bps_denominator", "max_provider_bond_bps", "max_duration_seconds",
        "relevant_multiplier", "irrelevant_multiplier",
        "risk_floor", "risk_ceiling", "rounding",
        "max_bond_bps", "concession_num", "concession_den",
        "min_service_window_seconds", "min_payout_delay_seconds", "profiles",
    ):
        assert required in NEGOTIATION_MANIFEST, f"{required} can move a term and is not hashed"


def test_the_manifest_mirrors_the_contract_bounds():
    """`constants` has no intra-package imports, so the mirroring is pinned here instead."""

    from wrasse.constants import NEGOTIATION_MANIFEST
    from wrasse.policy_hash import BPS_DENOMINATOR, MAX_DURATION, MAX_PROVIDER_BOND_BPS

    assert NEGOTIATION_MANIFEST["bps_denominator"] == BPS_DENOMINATOR
    assert NEGOTIATION_MANIFEST["max_provider_bond_bps"] == MAX_PROVIDER_BOND_BPS
    assert NEGOTIATION_MANIFEST["max_duration_seconds"] == MAX_DURATION


def test_the_engine_uses_the_multipliers_it_publishes():
    """A manifest entry nothing reads is a decoration, not a commitment."""

    import inspect

    from wrasse import engine

    source = inspect.getsource(engine)
    assert 'Decimal("1.5")' not in source and 'Decimal("0.5")' not in source, (
        "a relevance multiplier is inlined again, so the manifest no longer describes it"
    )
    assert "RELEVANT_MULTIPLIER" in source and "IRRELEVANT_MULTIPLIER" in source


# --------------------------------------------------------------------------------------
# The settlement has to be the one these numbers produce
# --------------------------------------------------------------------------------------


def test_an_invented_move_is_refused(document):
    """The gate's whole standalone claim.

    Shape-checking a `moves` list proves it is a list of well-formed objects. It does not
    prove any of it happened. Without recomputing, a document could carry a fabricated account
    of a negotiation beside a perfectly genuine signed preimage, and a judge would be shown an
    explanation that is not the one that set the price.
    """

    def invent(body):
        body["buyer"]["profiles"]["urgent"]["settlement"]["moves"].append({
            "term": "price_bps", "from": 12_000, "to": 9_000,
            "kind": "concession", "because": "buyer_max_price_bps=9000",
        })

    with pytest.raises(PolicyDocumentError, match="its own numbers do not produce"):
        _load(_rewrite(document, invent))


def test_a_proposal_the_settlement_does_not_follow_from_is_refused(document):
    """Edit an input and the published outcome stops being the one it produces."""

    def shift(body):
        body["buyer"]["profiles"]["urgent"]["provider"]["limits"]["max_bond_bps"] = 1

    with pytest.raises(PolicyDocumentError, match="its own numbers do not produce"):
        _load(_rewrite(document, shift))


@pytest.mark.parametrize("field,value", [("proposes", "not a number"), ("limits", None)])
def test_a_published_number_that_is_not_a_number_is_refused(document, field, value):
    """A block a reader is told they can recompute from must contain numbers to recompute."""

    def corrupt(body):
        half = body["buyer"]["profiles"]["urgent"]["buyer"][field]
        half[sorted(half)[0]] = value

    with pytest.raises(PolicyDocumentError, match="is not an integer"):
        _load(_rewrite(document, corrupt))


def test_a_document_that_hides_a_refusal_by_dropping_the_profile_is_refused(document):
    """Drop the profile that had no overlap and every remaining choice appears to have worked."""

    with pytest.raises(PolicyDocumentError, match="may not add, drop or rename"):
        _load(_rewrite(document, lambda body: body["buyer"]["profiles"].pop("budget")))


def test_a_zero_bond_quote_can_be_read_back_by_the_build_that_wrote_it(
    tmp_path, monkeypatch, capsys
):
    """The writer/reader split, closed end to end rather than at one of its two ends.

    A zero bond rate is valid on the deployed contract. The command that writes a quote and
    the code that reads one back must agree about that, and they did not.
    """

    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", str(CHAIN_ID))
    path = tmp_path / "zero-bond.json"

    assert main([
        "policy", PROVIDER, "--buyer", BUYER, "--base-bond-bps", "0",
        "--accept-by", "1700003600", "--reference-timestamp", "1700000000",
        "--output", str(path),
    ]) == 0
    capsys.readouterr()

    assert _load(path).bond_bps == 0


def test_a_baseline_this_build_cannot_read_back_is_refused_before_it_is_written(
    tmp_path, monkeypatch, capsys
):
    """Refused by the writer, not discovered later by the reader."""

    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", str(CHAIN_ID))
    path = tmp_path / "never-written.json"

    with pytest.raises(RuntimeError, match="could not be read back by this build"):
        main([
            "policy", PROVIDER, "--buyer", BUYER, "--base-bond-bps", "99999",
            "--accept-by", "1700003600", "--reference-timestamp", "1700000000",
            "--output", str(path),
        ])
    assert not path.exists(), "a refused baseline must not leave a document behind"


@pytest.mark.parametrize(
    "mutate,where",
    [
        (lambda body: body["buyer"]["profiles"]["urgent"]["terms"].update(
            {"provider_bond_bps": 500.0}), "terms"),
        (lambda body: body["buyer"]["profiles"]["urgent"]["baseline"].update(
            {"service_window": 3600.0}), "baseline"),
    ],
)
def test_a_number_written_as_a_float_is_not_the_same_number(document, mutate, where):
    """`2500.0 == 2500` in Python and not in JSON.

    The validator claimed bounded integers and exact comparison, and then compared with
    Python's `==`, so a document that encodes differently, hashes differently and reads
    differently to any other implementation was accepted as identical.
    """

    with pytest.raises(PolicyDocumentError):
        _load(_rewrite(document, mutate))


def test_the_rounding_mode_the_manifest_hashes_is_the_one_the_engine_uses():
    """A manifest entry nothing reads is a decoration, not a commitment.

    Editing a decorative entry changes the version without changing a term; editing the real
    rounding changes terms at half-unit boundaries without changing the version. Both
    directions are wrong, and the value being a plain string is what lets one number do both
    jobs.
    """

    import inspect
    from decimal import Decimal

    from wrasse import engine
    from wrasse.constants import INTEGER_QUANTUM, ROUNDING

    assert Decimal("2.5").quantize(Decimal(INTEGER_QUANTUM), rounding=ROUNDING) == Decimal("3")
    source = inspect.getsource(engine)
    assert "ROUND_HALF_UP" not in source, (
        "the engine names a rounding mode directly again, so the manifest no longer drives it"
    )
    assert "rounding=ROUNDING" in source


def test_the_provider_risk_weight_is_hashed_and_used():
    """It decides the provider's price, delay, bond ceiling and floor, and it was a literal."""

    import inspect

    from wrasse import engine
    from wrasse.constants import NEGOTIATION_MANIFEST

    assert NEGOTIATION_MANIFEST["provider_risk_weight"] == "1"
    source = inspect.getsource(engine)
    assert source.count("PROVIDER_RISK_WEIGHT") >= 4, (
        "both the score and its causal-set recomputation must read the hashed weight"
    )

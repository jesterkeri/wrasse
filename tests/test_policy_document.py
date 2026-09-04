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
PROVIDER = Web3.to_checksum_address("0x0b920573ADf657f45Fecd9f7e48e66B5535A90C0")


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
        body["profiles"]["urgent"]["terms"]["price_wei"] = 1

    with pytest.raises(PolicyDocumentError, match="displays price_wei=1 but commits"):
        _load(_rewrite(document, undercut))


@pytest.mark.parametrize("field", ["provider_bond_bps", "service_window"])
def test_every_displayed_term_is_bound_to_the_commitment(document, field):
    with pytest.raises(PolicyDocumentError, match="displays"):
        _load(_rewrite(document, lambda body: body["profiles"]["urgent"]["terms"].update({field: 7})))


def test_the_receipts_a_profile_says_it_priced_must_be_the_ones_it_committed_to(document):
    def relist(body):
        body["profiles"]["urgent"]["terms"]["evidence_event_ids"] = ["0x" + "5c" * 32]

    with pytest.raises(PolicyDocumentError, match="not the receipts it committed to"):
        _load(_rewrite(document, relist))


def test_the_listed_evidence_must_hash_to_the_buyer_commitment(document):
    def plant(body):
        body["evidence"] = [{"event_id": "0x" + "7a" * 32}]

    with pytest.raises(PolicyDocumentError, match="listed"):
        _load(_rewrite(document, plant))


def test_a_quoted_hash_that_its_own_fields_do_not_produce_is_refused(document):
    def tamper(body):
        body["profiles"]["urgent"]["policy_preimage"]["payout_delay"] = 999

    with pytest.raises(PolicyDocumentError, match="hash to"):
        _load(_rewrite(document, tamper))


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"chain_id": 1}, "targets chain"),
        ({"contract_address": "0x" + "99" * 20}, "this run targets"),
        ({"buyer": "0x" + "88" * 20}, "signing wallet"),
        ({"provider": "0x" + "77" * 20}, "counterparty"),
        ({"profile": "nonexistent"}, "no profile named"),
    ],
)
def test_a_document_meant_for_something_else_is_refused(document, overrides, reason):
    with pytest.raises(PolicyDocumentError, match=reason):
        _load(document, **overrides)


def test_an_unknown_profile_name_is_refused_even_if_the_document_offers_it(document):
    """The document could offer a profile this engine has never produced."""

    def smuggle(body):
        body["profiles"]["freebie"] = body["profiles"]["urgent"]

    with pytest.raises(PolicyDocumentError, match="not a profile this engine produces"):
        _load(_rewrite(document, smuggle), profile="freebie")


@pytest.mark.parametrize(
    "where",
    ["top", "profile", "preimage", "terms", "executability"],
)
def test_unknown_fields_are_refused_everywhere(document, where):
    def add(body):
        target = {
            "top": body,
            "profile": body["profiles"]["urgent"],
            "preimage": body["profiles"]["urgent"]["policy_preimage"],
            "terms": body["profiles"]["urgent"]["terms"],
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
        _load(_rewrite(document, lambda body: body.update({"schema_version": 2})))


def test_an_engine_version_this_build_did_not_write_is_refused(document):
    """The version is hashed into the commitment, so it is a term, not a label."""

    def relabel(body):
        body["engine_version"] = "wrasse/0.2.0"
        body["profiles"]["urgent"]["policy_preimage"]["engine_version"] = "wrasse/0.2.0"

    with pytest.raises(PolicyDocumentError, match="this build is"):
        _load(_rewrite(document, relabel))


def test_a_request_id_that_is_not_a_request_id_is_refused(document):
    with pytest.raises(PolicyDocumentError, match="32 lowercase hex"):
        _load(_rewrite(document, lambda body: body.update({"request_id": "../../etc/passwd"})))


def test_a_provider_evidence_commitment_this_build_cannot_explain_is_refused(document):
    """Provider-side recall does not exist yet, so a non-empty commitment here is unexplainable."""

    def invent(body):
        body["profiles"]["urgent"]["policy_preimage"]["provider_evidence_hash"] = "0x" + "3c" * 32

    with pytest.raises(PolicyDocumentError, match="hash to|not the empty set"):
        _load(_rewrite(document, invent))


def test_an_unbound_document_says_how_to_bind_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.delenv("WRASSE_ESCROW_ADDRESS", raising=False)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", str(CHAIN_ID))
    path = tmp_path / "unbound.json"
    assert main([
        "policy", PROVIDER, "--buyer", BUYER,
        "--accept-by", "1700003600", "--reference-timestamp", "1700000000",
        "--output", str(path),
    ]) == 0
    capsys.readouterr()

    with pytest.raises(PolicyNotBound, match="rebind-policy"):
        _load(path)


def test_rebinding_mints_a_new_identity(tmp_path, monkeypatch, capsys):
    """A rebound quote is a different action, so it must not inherit the old identity."""

    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.delenv("WRASSE_ESCROW_ADDRESS", raising=False)
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", str(CHAIN_ID))
    unbound = tmp_path / "unbound.json"
    assert main([
        "policy", PROVIDER, "--buyer", BUYER,
        "--accept-by", "1700003600", "--reference-timestamp", "1700000000",
        "--output", str(unbound),
    ]) == 0
    capsys.readouterr()
    before = json.loads(unbound.read_text())["request_id"]

    monkeypatch.setenv("WRASSE_ESCROW_ADDRESS", ESCROW)
    bound = tmp_path / "bound.json"
    assert main(["rebind-policy", "--policy", str(unbound), "--output", str(bound)]) == 0
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

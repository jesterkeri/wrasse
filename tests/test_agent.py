"""One side, computed by something that can only see one side.

Until this module existed, a single process opened both memories and computed both halves. The
memories were separate and the reasoning was not, which made "two agents" a description of the
model rather than a fact about the running system. These tests are what turn that back into a
claim about the code: what a side is allowed to publish, what it must never see, and that two
sides publishing separately settle on the same terms the joint path produced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from web3 import Web3

from wrasse import agent
from wrasse.dimensions import DIMENSION_CATEGORY, DimensionDefinition
from wrasse.evidence import ChainEvent
from wrasse.providers import ProviderPersona
from wrasse.store import WrasseStore, persona_digest

BUYER = Web3.to_checksum_address("0x30C95B7eb3E08F83992E803Be2A5AB0E0af93d22")
PROVIDER = Web3.to_checksum_address("0x0b920573ADf657f45Fecd9f7e48e66B5535A90C0")
ESCROW = Web3.to_checksum_address("0x5525653f05990DA1479578893b5a624183AFa22E")
REPO = Path(__file__).resolve().parent.parent

BASELINE = {
    "base_price_wei": 100_000_000_000_000, "base_bond_bps": 500,
    "base_service_window": 600, "base_payout_delay": 1_800,
}

TIMEOUT = ChainEvent(
    chain_id=84532, contract_address=ESCROW, tx_hash="0x" + "b1" * 32, log_index=1,
    block_number=46_390_048, event_type="timeout_claimed_without_delivery", deal_id=1,
    buyer=BUYER, provider=PROVIDER, observed_at=datetime(2026, 9, 4, tzinfo=UTC).isoformat(),
)
#: The provider's grievance. Both receipts are needed or the two sides are not symmetric: a
#: timeout is about the provider, so it moves the buyer's view and leaves the provider's at
#: zero. A fixture holding only that one makes the provider look like it has nothing to say,
#: which is true of the fixture and false of the system.
DELAY = ChainEvent(
    chain_id=84532, contract_address=ESCROW, tx_hash="0x" + "d2" * 32, log_index=2,
    block_number=46_390_100, event_type="delivered_and_claimed_after_delay", deal_id=2,
    buyer=BUYER, provider=PROVIDER, observed_at=datetime(2026, 9, 4, tzinfo=UTC).isoformat(),
)
READING = DimensionDefinition(
    dimension_id="non_delivery_after_payment",
    source_event_type="timeout_claimed_without_delivery",
    signal_direction="negative", severity=0.9, confidence=0.9,
    applies_when=("deadline_sensitive", "cost_sensitive", "quality_sensitive"),
)
DELAY_READING = DimensionDefinition(
    dimension_id="buyer_payment_delay",
    source_event_type="delivered_and_claimed_after_delay",
    signal_direction="negative", severity=0.8, confidence=0.9,
    applies_when=("deadline_sensitive", "cost_sensitive", "quality_sensitive"),
)


def _store(directory: Path, role: str, owner: str, *, receipts=(TIMEOUT, DELAY)) -> WrasseStore:
    store = WrasseStore.open(
        directory / f"{role}-memory.db", role=role, owner_address=owner,
        chain_id=84532, escrow_address=ESCROW,
    )
    if role == "provider":
        digest, persona = persona_digest(REPO / "personas" / "provider-a.json")
        store.commit_persona(name=persona["name"], digest=digest)
    for reading in (READING, DELAY_READING):
        store.memory.set_entity(
            DIMENSION_CATEGORY, reading.dimension_id, reading.body(), status="active"
        )
    for receipt in receipts:
        store.ingest(receipt)
    return store


@pytest.fixture
def persona() -> ProviderPersona:
    _, document = persona_digest(REPO / "personas" / "provider-a.json")
    return ProviderPersona.from_document(document)


def test_the_buyer_publishes_only_what_the_buyer_owns(tmp_path):
    """Bond and window are its proposals; the price ceiling and the delay floor are its limits.

    Nothing about the price it will be asked or the delay it will be offered, because those are
    the other side's to propose. A side that published the counterparty's terms would be
    speaking for a memory it cannot read.
    """

    published = agent.publish_positions(
        role="buyer", store=_store(tmp_path, "buyer", BUYER),
        counterparty=PROVIDER, **BASELINE,
    )

    assert set(published["profiles"]["urgent"]["proposes"]) == {
        "provider_bond_bps", "service_window"
    }
    assert set(published["profiles"]["urgent"]["limits"]) == {
        "max_price_bps", "min_payout_delay"
    }
    assert "persona" not in published
    assert "price_bps" not in str(published["profiles"]["urgent"]["proposes"])


def test_the_provider_publishes_only_what_the_provider_owns(tmp_path, persona):
    published = agent.publish_positions(
        role="provider", store=_store(tmp_path, "provider", PROVIDER),
        counterparty=BUYER, persona=persona, **BASELINE,
    )

    assert set(published["proposes"]) == {"price_bps", "payout_delay"}
    assert set(published["limits"]) == {"max_bond_bps", "min_service_window"}
    assert set(published["walkaways"]) == {"price_floor_bps"}
    assert "profiles" not in published


def test_neither_side_publishes_its_ontology(tmp_path, persona):
    """A dimension is how a side reads an outcome. The other side reading it would make one
    memory out of two, which is the property this whole project rests on."""

    for role, owner, extra in (
        ("buyer", BUYER, {}), ("provider", PROVIDER, {"persona": persona}),
    ):
        published = agent.publish_positions(
            role=role, store=_store(tmp_path / role, role, owner),
            counterparty=PROVIDER if role == "buyer" else BUYER, **BASELINE, **extra,
        )
        printed = str(published)
        assert READING.dimension_id not in printed
        assert "severity" not in printed and "confidence" not in printed


def test_both_sides_publish_what_they_hold_so_a_gap_is_visible(tmp_path, persona):
    """The joint path asserted internally that the two memories agreed. Split apart, that has
    to become part of the exchange: each side says what it holds and a disagreement stops the
    quote, because neither side can be trusted to speak for the other."""

    agreed = tmp_path / "agreed"
    buyer = agent.publish_positions(
        role="buyer", store=_store(agreed / "b", "buyer", BUYER),
        counterparty=PROVIDER, **BASELINE,
    )
    provider = agent.publish_positions(
        role="provider", store=_store(agreed / "p", "provider", PROVIDER),
        counterparty=BUYER, persona=persona, **BASELINE,
    )
    assert buyer["held_event_ids"] == provider["held_event_ids"]
    assert len(buyer["held_event_ids"]) == 2

    starved = agent.publish_positions(
        role="provider", store=_store(tmp_path / "starved", "provider", PROVIDER, receipts=()),
        counterparty=BUYER, persona=persona, **BASELINE,
    )
    assert starved["held_event_ids"] != buyer["held_event_ids"]
    assert starved["cold_start"] is True


def test_two_separately_published_sides_settle_to_the_live_terms(tmp_path, persona):
    """The check the split exists to survive.

    Two sides publishing independently have to resolve to the same deal the joint path
    produced. If they did not, splitting the process would have changed the economics, and the
    thing being demonstrated would no longer be the thing that was reviewed.
    """

    buyer = agent.publish_positions(
        role="buyer", store=_store(tmp_path / "b", "buyer", BUYER),
        counterparty=PROVIDER, **BASELINE,
    )
    provider = agent.publish_positions(
        role="provider", store=_store(tmp_path / "p", "provider", PROVIDER),
        counterparty=BUYER, persona=persona, **BASELINE,
    )

    urgent = buyer["profiles"]["urgent"]
    proposals = {
        "provider_bond_bps": urgent["proposes"]["provider_bond_bps"],
        "service_window": urgent["proposes"]["service_window"],
        "price_bps": provider["proposes"]["price_bps"],
        "payout_delay": provider["proposes"]["payout_delay"],
    }
    limits = {
        "provider_bond_bps": provider["limits"]["max_bond_bps"],
        "service_window": provider["limits"]["min_service_window"],
        "price_bps": urgent["limits"]["max_price_bps"],
        "payout_delay": urgent["limits"]["min_payout_delay"],
    }

    # Every number that decides this deal came from a call that saw one memory.
    assert proposals["provider_bond_bps"] == 3500
    assert proposals["price_bps"] == 11800
    assert limits["provider_bond_bps"] == 2480
    assert limits["payout_delay"] == 900
    assert provider["walkaways"]["price_floor_bps"] == 11350

    settled = {
        "provider_bond_bps": min(proposals["provider_bond_bps"], limits["provider_bond_bps"]),
        "service_window": max(proposals["service_window"], limits["service_window"]),
        "price_bps": min(proposals["price_bps"], limits["price_bps"]),
        "payout_delay": max(proposals["payout_delay"], limits["payout_delay"]),
    }
    assert settled == {
        "provider_bond_bps": 2480, "service_window": 300,
        "price_bps": 11800, "payout_delay": 900,
    }


def test_a_side_refuses_to_publish_over_an_outcome_it_cannot_read(tmp_path):
    """Publishing a number derived from an unreadable receipt would assert it was harmless."""

    store = _store(tmp_path, "buyer", BUYER, receipts=())
    unreadable = ChainEvent(
        chain_id=84532, contract_address=ESCROW, tx_hash="0x" + "c2" * 32, log_index=2,
        block_number=46_390_050, event_type="delivered_and_released_by_buyer", deal_id=2,
        buyer=BUYER, provider=PROVIDER,
        observed_at=datetime(2026, 9, 5, tzinfo=UTC).isoformat(),
    )
    store.ingest(unreadable)
    with pytest.raises(agent.AgentError, match="no dimension yet"):
        agent.publish_positions(
            role="buyer", store=store, counterparty=PROVIDER, **BASELINE
        )


def test_a_role_that_is_not_a_side_is_refused(tmp_path):
    with pytest.raises(agent.AgentError, match="not a side"):
        agent.publish_positions(
            role="auditor", store=_store(tmp_path, "buyer", BUYER),
            counterparty=PROVIDER, **BASELINE,
        )


def test_the_provider_cannot_publish_without_its_committed_persona(tmp_path):
    """The persona is fixed before any receipt and hashed into the store. Publishing without
    one would be publishing terms nobody committed to in advance."""

    with pytest.raises(agent.AgentError, match="committed persona"):
        agent.publish_positions(
            role="provider", store=_store(tmp_path, "provider", PROVIDER),
            counterparty=BUYER, **BASELINE,
        )

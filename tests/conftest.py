from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wrasse import liabilities

from wrasse.evidence import ChainEvent

#: The provider most tests quote against. The shipped persona names the real provider A wallet,
#: and the persona is deliberately bound to the address it describes, so a test using a
#: different provider has to write its own.
TEST_PROVIDER = "0x3333333333333333333333333333333333333333"


def write_persona(directory: Path, address: str, *, name: str = "atlas") -> Path:
    """A persona for one wallet, written where a test can point the CLI at it."""

    path = Path(directory) / "persona.json"
    path.write_text(json.dumps({
        "name": name,
        "address": address,
        "cashflow_sensitivity": "0.70",
        "price_sensitivity_bps": 2500,
        "delay_sensitivity_seconds": 1800,
    }))
    return path


@pytest.fixture
def chain_event() -> ChainEvent:
    return ChainEvent(
        chain_id=84532,
        contract_address="0x1111111111111111111111111111111111111111",
        tx_hash="0x" + "22" * 32,
        log_index=3,
        block_number=123456,
        event_type="timeout_claimed_without_delivery",
        deal_id=7,
        buyer="0x4444444444444444444444444444444444444444",
        provider="0x3333333333333333333333333333333333333333",
        observed_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC).isoformat(),
    )



@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """No test may touch the repository's real memories or its real transaction ledger.

    The stores now refuse to open under a different owner, which is the point of them, so a
    test that leaked into `.wrasse/` would fail every later test for the right reason and the
    wrong cause.
    """

    monkeypatch.setenv("WRASSE_BUYER_MEMORY_PATH", str(tmp_path / "buyer-memory.db"))
    monkeypatch.setenv("WRASSE_PROVIDER_MEMORY_PATH", str(tmp_path / "provider-memory.db"))
    monkeypatch.setenv("WRASSE_TX_DB", str(tmp_path / "transactions.db"))
    # The open-deal index too. It is read at boot to decide what to close and collect, so a
    # test that wrote into the deployment's own would make a later run try to recover a deal
    # invented by a fixture.
    monkeypatch.setenv("WRASSE_LIABILITY_DB", str(tmp_path / "liabilities.db"))
    monkeypatch.setattr(
        liabilities, "LIABILITY_DB", tmp_path / "liabilities.db", raising=False
    )
    monkeypatch.setenv("WRASSE_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv(
        "WRASSE_ESCROW_ADDRESS", "0x2222222222222222222222222222222222222222"
    )
    monkeypatch.setenv("BASE_SEPOLIA_CHAIN_ID", "84532")
    monkeypatch.setenv("WRASSE_PROVIDER_A_ADDRESS", TEST_PROVIDER)
    monkeypatch.setenv("WRASSE_PROVIDER_PERSONA", str(write_persona(tmp_path, TEST_PROVIDER)))

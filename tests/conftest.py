from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wrasse.evidence import ChainEvent


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
        provider="0x3333333333333333333333333333333333333333",
        observed_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC).isoformat(),
    )


"""Chain time, not local time, decides whether a deal can still be created.

A deadline is enforced by the block that mines the transaction. Any timestamp taken from
this machine, or supplied on the command line, is a guess about that block. Treating a
guess as the authority is what lets a quote look executable and then revert.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


#: Refuse to quote against an observation this far from local time. Used only to veto. A
#: disagreement means either the node is lagging or this machine's clock is wrong, and
#: neither is a safe basis for a deadline, so it can only ever refuse and never approve.
DEFAULT_MAX_OBSERVATION_SKEW_SECONDS = 300


class ChainTimeUnavailable(RuntimeError):
    """The chain could not be read, or what it returned cannot be trusted.

    Raised rather than falling back to the local clock. A live quote without a live
    observation is the exact overstatement this module exists to prevent.
    """


@dataclass(frozen=True)
class ChainObservation:
    """What the configured chain said, and when it said it."""

    chain_id: int
    block_number: int
    timestamp: int

    def as_dict(self) -> dict[str, int]:
        return {
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_timestamp": self.timestamp,
        }


def observe_chain_time(web3: Any, *, expected_chain_id: int) -> ChainObservation:
    """Read the latest block of the configured chain, or refuse."""

    try:
        chain_id = int(web3.eth.chain_id)
        block = web3.eth.get_block("latest")
    except Exception as error:  # noqa: BLE001 - any transport failure means no live quote
        raise ChainTimeUnavailable(f"could not read the latest block: {error}") from error

    if chain_id != expected_chain_id:
        raise ChainTimeUnavailable(
            f"connected to chain {chain_id}, expected {expected_chain_id}; "
            "a deadline checked against the wrong chain is not checked at all"
        )
    return ChainObservation(
        chain_id=chain_id,
        block_number=int(block["number"]),
        timestamp=int(block["timestamp"]),
    )


def require_recent(
    observation: ChainObservation,
    *,
    local_now: int,
    max_skew_seconds: int = DEFAULT_MAX_OBSERVATION_SKEW_SECONDS,
) -> None:
    """Refuse an observation too far from local time to be a useful reading."""

    drift = abs(local_now - observation.timestamp)
    if drift > max_skew_seconds:
        raise ChainTimeUnavailable(
            f"block {observation.block_number} is {drift}s from local time, beyond the "
            f"{max_skew_seconds}s bound; refusing to quote against a stale or skewed reading"
        )

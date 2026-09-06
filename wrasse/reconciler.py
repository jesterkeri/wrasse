"""Turning a Base receipt into evidence, or refusing to.

A receipt is the only thing allowed to create memory here, and it has to earn that. Five
things must agree before anything is written: the ledger row this build recorded, the receipt
itself, who sent it, exactly one recognised outcome in it, and the deal as the contract held it
at that block. Any disagreement stops the write, because a store that contains something
nobody can explain is worse than a store missing an entry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from web3 import Web3

from . import chain, escrow
from .chain import _as_hex
from .evidence import ChainEvent
from .store import both_locked

TIMEOUT_SIGNATURE = Web3.keccak(text="TimeoutClaimed(uint256)")
RELEASED_SIGNATURE = Web3.keccak(text="DealReleased(uint256,bool)")

#: Every outcome this build recognises, and what each one means about the two parties. The
#: sender is not a guess: a timeout is claimed by the buyer, a release is granted by the buyer,
#: and a payment claimed after the delay is taken by the provider.
OUTCOMES: dict[str, dict[str, Any]] = {
    "timeout_claimed_without_delivery": {"sender_role": "buyer", "deal_state": "TimedOut"},
    "delivered_and_released_by_buyer": {"sender_role": "buyer", "deal_state": "Released"},
    "delivered_and_claimed_after_delay": {"sender_role": "provider", "deal_state": "Released"},
}


class ChainVerificationError(RuntimeError):
    """A receipt does not prove the neutral event Wrasse was asked to ingest."""


def _field(value: Any, name: str) -> Any:
    return value[name] if isinstance(value, dict) else getattr(value, name)


def _hex(value: Any) -> str:
    return "0x" + bytes(value).hex()


def _classify(log: Any) -> tuple[str, int] | None:
    """Name the outcome a log describes, or None if this build does not recognise it."""

    topics = _field(log, "topics")
    if not topics:
        return None
    signature = bytes(topics[0])
    if signature == bytes(TIMEOUT_SIGNATURE):
        if len(topics) != 2:
            raise ChainVerificationError("timeout event has an invalid topic count")
        return "timeout_claimed_without_delivery", int.from_bytes(bytes(topics[1]), "big")

    if signature == bytes(RELEASED_SIGNATURE):
        if len(topics) != 2:
            raise ChainVerificationError("release event has an invalid topic count")
        released_by_buyer = int.from_bytes(bytes(_field(log, "data")), "big") != 0
        name = (
            "delivered_and_released_by_buyer"
            if released_by_buyer
            else "delivered_and_claimed_after_delay"
        )
        return name, int.from_bytes(bytes(topics[1]), "big")

    return None


def verify_outcome(
    web3: Any,
    ledger: chain.TransactionLedger,
    *,
    tx_hash: str,
    expected_chain_id: int,
    expected_contract: str,
) -> ChainEvent:
    """Return canonical evidence only when every one of the five checks agrees."""

    contract_address = Web3.to_checksum_address(expected_contract)

    if int(web3.eth.chain_id) != expected_chain_id:
        raise ChainVerificationError("connected chain id does not match configuration")

    # 1. This build sent it, and watched it settle. Inclusion is not enough: a reorged receipt
    #    would put a fact into the store that the chain no longer agrees with.
    row = ledger.find_by_tx_hash(chain_id=expected_chain_id, tx_hash=tx_hash)
    if row is None:
        raise ChainVerificationError(f"{tx_hash} is not in this ledger")
    chain.verify_row_integrity(row)
    if row.status != chain.CONFIRMED_SUCCESS:
        raise ChainVerificationError(
            f"{tx_hash} is {row.status}; only a confirmed success may become memory. A "
            "reverted transaction proves no outcome at all."
        )

    # 2. The receipt itself, and the block it is in now.
    receipt = web3.eth.get_transaction_receipt(tx_hash)
    if int(_field(receipt, "status")) != 1:
        raise ChainVerificationError("transaction receipt is not successful")
    transaction = web3.eth.get_transaction(tx_hash)
    if Web3.to_checksum_address(_field(transaction, "to")) != contract_address:
        raise ChainVerificationError("transaction target is not the configured contract")
    block_number = int(_field(receipt, "blockNumber"))
    block_hash = _as_hex(_field(receipt, "blockHash"))

    # 2a. The same block the ledger watched settle, and that block still on the canonical
    #     chain. `confirmed_success` is terminal, so it survives a reorg that moves the
    #     transaction: the row keeps saying confirmed while the transaction is back in a
    #     block that nothing has waited on. Without this, memory takes a receipt that was
    #     re-included seconds ago and records it as a settled fact, which is the one thing the
    #     confirmed-only rule exists to prevent.
    if row.block_number is None or row.block_hash is None:
        raise ChainVerificationError(
            f"{tx_hash} is recorded as {row.status} with no block, so there is nothing to "
            "check the receipt against. Re-resolve it before reconciling."
        )
    if block_number != row.block_number or block_hash != row.block_hash:
        raise ChainVerificationError(
            f"{tx_hash} confirmed in block {row.block_number} but the chain now returns it in "
            f"block {block_number}. It was reorged and re-included; run tx-resolve so it is "
            "confirmed again on this fork before it becomes memory."
        )
    try:
        canonical = web3.eth.get_block(block_number)
    except Exception as error:  # noqa: BLE001
        raise ChainVerificationError(
            f"could not read block {block_number} to confirm it is still canonical: {error}"
        ) from error
    if _as_hex(_field(canonical, "hash")) != block_hash:
        raise ChainVerificationError(
            f"block {block_number} no longer has hash {block_hash}; the receipt this build "
            "confirmed is on a fork the chain has abandoned"
        )

    # 3. Exactly one recognised outcome. Two would mean the receipt describes more than one
    #    thing happening; none would mean it describes nothing this build understands.
    matches = []
    for log in _field(receipt, "logs"):
        if Web3.to_checksum_address(_field(log, "address")) != contract_address:
            continue
        classified = _classify(log)
        if classified is not None:
            matches.append((classified, log))
    if len(matches) != 1:
        raise ChainVerificationError(
            f"receipt carries {len(matches)} recognised outcomes from the escrow, expected one"
        )
    (event_type, deal_id), log = matches[0]

    # 4. The deal as the contract held it at that block, never at the tip. A failed historical
    #    read stops here rather than falling back to current state, which would be a different
    #    claim wearing the same words.
    try:
        deal = escrow.read_deal(web3, contract_address, deal_id, block=block_number)
    except Exception as error:  # noqa: BLE001
        raise ChainVerificationError(
            f"could not read deal {deal_id} at block {block_number}: {error}"
        ) from error

    expected_state = OUTCOMES[event_type]["deal_state"]
    if deal["state"] != expected_state:
        raise ChainVerificationError(
            f"{event_type} needs the deal {expected_state} at block {block_number}, "
            f"it was {deal['state']}"
        )

    # 5. Who sent it. The role is implied by the outcome, and the identity comes from contract
    #    state rather than from the log.
    sender_role = OUTCOMES[event_type]["sender_role"]
    sender = Web3.to_checksum_address(_field(transaction, "from"))
    if sender != deal[sender_role]:
        raise ChainVerificationError(
            f"{event_type} must come from the {sender_role} {deal[sender_role]}, not {sender}"
        )

    block = web3.eth.get_block(block_number)
    return ChainEvent(
        chain_id=expected_chain_id,
        contract_address=contract_address,
        tx_hash=_hex(_field(receipt, "transactionHash")),
        log_index=int(_field(log, "logIndex")),
        block_number=block_number,
        event_type=event_type,
        deal_id=deal_id,
        buyer=deal["buyer"],
        provider=deal["provider"],
        observed_at=datetime.fromtimestamp(_field(block, "timestamp"), UTC).isoformat(),
    )


def reconcile(
    stores: dict[str, Any],
    web3: Any,
    ledger: chain.TransactionLedger,
    **expected: Any,
) -> dict[str, dict[str, Any]]:
    """Verify once, then deliver the same neutral fact to every store.

    Both sides receive it. The receipt does not belong to one of them: a deal timed out, or a
    buyer made a provider wait, and each side draws its own conclusion from the same sentence.
    Delivering it to only one would leave the other with a blind spot it has no way to know
    about, and would contradict the claim that the result teaches both memories.

    Delivery goes through each store's own `ingest`, not through `persist_verified_event`
    directly, because storing the record is only half of it: each side also has to index the
    fact under its own view of the relationship, or the pricing path will never find it.

    Delivery is idempotent per store, so a partial write from an earlier crash converges when
    the same receipt is replayed rather than duplicating what already landed.
    """

    event = verify_outcome(web3, ledger, **expected)
    # Both locks across both deliveries, not one lock per delivery. A reader taking both in the
    # gap between them sees one side holding a receipt the other does not, which is exactly
    # what a corrupted pair looks like, and the quote correctly refuses. The receipt was fine;
    # the window was the defect.
    with both_locked(stores):
        return {name: store.ingest(event) for name, store in stores.items()}

# RapportEscrow pre-deployment review

Date: 2026-09-03 UTC

Scope: `contracts/src/RapportEscrow.sol` and its Python policy-hash producer.
Target: Base Sepolia. This review was performed before any deployment.

## Result

No known critical or high-severity fund-loss issue remains in the reviewed
scope. Deployment is still conditional on the full tests passing against the
exact commit to be deployed.

The review found and fixed one material commitment-integrity issue: the initial
contract accepted an arbitrary `policyHash`. It now derives `policyHash`
onchain from the economic terms actually supplied to `createDeal`, plus the
engine-version and evidence commitments. A shared Python/Solidity fixture tests
the exact ABI encoding.

## Invariants checked

- The buyer's price comes only from `msg.value`; no duplicate price parameter
  can disagree with the actual deposit.
- The provider must be the configured address and must deposit the exact bond.
- The service deadline starts when the provider accepts, not when the offer is
  created.
- An unaccepted offer is refundable only after `acceptBy`, eliminating an
  overlap in which buyer cancellation and provider acceptance are both valid.
- Delivery after the deadline reverts.
- A timeout returns both price and provider bond to the buyer.
- A delivered deal pays price plus returned bond to the provider, either on
  early buyer release or after the payout delay.
- State changes precede all ETH transfers, and payout/refund functions use a
  reentrancy guard. A hostile receiver regression test attempts reentry.
- Every terminal path has a tested fund exit.

## Known limitations

- Delivery is asserted by the provider; the contract does not prove service
  quality or arbitrate disputes.
- A buyer or provider contract that permanently refuses ETH can prevent its own
  payout. Failed transfers revert atomically, so another party cannot capture
  those funds, but the MVP has no alternate withdrawal address.
- ETH forcibly sent to the contract outside `createDeal` is not recoverable.
- The contract is intentionally immutable and has no admin recovery function.

These limitations are acceptable for the hackathon's single-service prototype
and must be stated in the demo rather than described as solved.

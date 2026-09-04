# WrasseEscrow pre-deployment review

Date: 2026-09-03 UTC

Scope: `contracts/src/WrasseEscrow.sol` and its Python policy-hash producer.
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
- No state transition sends ETH. Settlement assigns a withdrawable credit, and
  `withdraw` is the contract's only external call site. It zeroes the balance
  before calling out and is reentrancy-guarded; a hostile receiver regression
  test attempts reentry through it.
- Every terminal path has a tested fund exit, and terminal states reject every
  further transition.
- Credits from several deals aggregate into one balance per address, and a
  regression test shows one deal cannot reach another deal's funds.
- A handler-driven invariant suite fuzzes arbitrary interleavings and asserts the
  escrow balance always covers open liabilities plus uncollected credits. Both
  invariants were confirmed to fail against a double-credit mutation and against
  a settlement that skips its state change.
- `contracts/test/fixtures/policy-vectors.json` is run through `createDeal` here
  and through `validate_creatable` in the Python suite, so the producer's claim
  to mirror the creation rules is differentially tested, including the
  multiplication overflow that unbounded Python integers would otherwise miss.

## Known limitations

- Delivery is asserted by the provider; the contract does not prove service
  quality or arbitrate disputes.
- A buyer or provider contract that permanently refuses ETH can strand its own
  credit. It cannot strand the counterparty's deposit: settlement completes
  without calling out, so no deal can be held in a non-terminal state, and the
  credit holder may nominate any withdrawal address. An address that can neither
  receive ETH nor nominate an alternative keeps its own funds locked.
- ETH forcibly sent to the contract outside `createDeal` is not recoverable.
- The contract is intentionally immutable and has no admin recovery function.

These limitations are acceptable for the hackathon's single-service prototype
and must be stated in the demo rather than described as solved.

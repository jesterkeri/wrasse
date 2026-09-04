// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {WrasseEscrow} from "../src/WrasseEscrow.sol";

interface Vm {
    struct Log {
        bytes32[] topics;
        bytes data;
        address emitter;
    }

    function deal(address who, uint256 newBalance) external;
    function recordLogs() external;
    function getRecordedLogs() external returns (Log[] memory);
    function prank(address sender) external;
    function startPrank(address sender) external;
    function stopPrank() external;
    function warp(uint256 newTimestamp) external;
    function expectRevert() external;
    function expectRevert(bytes4 selector) external;
    function expectRevert(bytes calldata revertData) external;
    function readFile(string calldata path) external view returns (string memory);
    function parseJsonUintArray(string calldata json, string calldata key) external pure returns (uint256[] memory);
    function parseJsonBoolArray(string calldata json, string calldata key) external pure returns (bool[] memory);
}

contract ReentrantBuyer {
    WrasseEscrow private immutable escrow;
    uint256 private dealId;
    bool private attempted;
    bool public reentrySucceeded;

    constructor(WrasseEscrow target) {
        escrow = target;
    }

    function create(address provider, uint64 acceptBy) external payable {
        dealId = escrow.createDeal{value: msg.value}(
            provider,
            2_000,
            acceptBy,
            2 hours,
            30 minutes,
            keccak256("wrasse/0.1.0"),
            keccak256("buyer-evidence"),
            keccak256("provider-evidence")
        );
    }

    function cancel() external {
        escrow.cancelUnaccepted(dealId);
    }

    function collect() external {
        escrow.withdraw(payable(address(this)));
    }

    receive() external payable {
        if (!attempted) {
            attempted = true;
            (reentrySucceeded,) =
                address(escrow).call(abi.encodeCall(WrasseEscrow.withdraw, (payable(address(this)))));
        }
    }
}

/// @dev Refuses every incoming transfer. Under a design that pushed value during settlement,
/// this address could hold a deal in a non-terminal state forever and freeze the
/// counterparty's deposit along with its own.
contract RejectingParty {
    WrasseEscrow private immutable escrow;

    constructor(WrasseEscrow target) {
        escrow = target;
    }

    function create(address provider, uint256 price, uint64 acceptBy) external returns (uint256) {
        return escrow.createDeal{value: price}(
            provider,
            2_000,
            acceptBy,
            2 hours,
            30 minutes,
            keccak256("wrasse/0.1.0"),
            keccak256("buyer-evidence"),
            keccak256("provider-evidence")
        );
    }

    function accept(uint256 dealId, uint256 bond) external {
        escrow.acceptDeal{value: bond}(dealId);
    }

    function deliver(uint256 dealId) external {
        escrow.markDelivered(dealId);
    }

    function claimTimeout(uint256 dealId) external {
        escrow.claimTimeout(dealId);
    }

    function collect() external {
        escrow.withdraw(payable(address(this)));
    }

    receive() external payable {
        revert("refuses ETH");
    }
}

contract WrasseEscrowTest {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    WrasseEscrow private escrow;
    address private constant BUYER = address(0xB0B);
    address private constant PROVIDER = address(0xA11CE);
    uint256 private constant PRICE = 1 ether;
    uint256 private constant BOND = 0.2 ether;
    uint64 private constant ACCEPT_WINDOW = 1 hours;
    uint64 private constant SERVICE_WINDOW = 2 hours;
    uint64 private constant PAYOUT_DELAY = 30 minutes;
    bytes32 private constant ENGINE_VERSION_HASH = 0x3adccb560ae1964af4cdd5471d5863cdbd37f484db6bbd2cc7a9f397d4753c81;
    /// @dev What the buyer recalled about the provider.
    bytes32 private constant BUYER_EVIDENCE_HASH = 0x2f685994ab703309ca4d0393ec2524b0368f819050ff85e7e3fb719cc5b48de3;
    /// @dev keccak256(abi.encode(new bytes32[](0))). Every first deal commits this on one side.
    bytes32 private constant EMPTY_EVIDENCE_HASH = 0x569e75fc77c1a856f6daaf9e69d8a9566ca34aa47f9133711ce065a571af0cfd;

    // Canonical cross-language fixture, shared with tests/test_policy_hash.py.
    address private constant CANON_BUYER = address(0x4444444444444444444444444444444444444444);
    address private constant CANON_PROVIDER = address(0x3333333333333333333333333333333333333333);
    uint64 private constant CANON_ACCEPT_BY = 1_700_000_000;
    bytes32 private constant POLICY_HASH = 0x2feccea0356143b90ef1b559f53a0cfa28c8c52f3e47d046a5d5f68909317d2b;

    /// @dev Recorded by _create so a test can recompute the commitment it produced.
    uint64 private lastAcceptBy;

    function setUp() public {
        escrow = new WrasseEscrow();
        vm.deal(BUYER, 10 ether);
        vm.deal(PROVIDER, 10 ether);
    }

    function testCreateAndAcceptUsesRelativeDeadline() public {
        uint256 dealId = _create();
        vm.warp(block.timestamp + 10 minutes);
        uint256 acceptedAt = block.timestamp;
        _accept(dealId);

        (,,,,, uint64 storedAcceptedAt,, uint64 deadline,,,, WrasseEscrow.State state) = escrow.deals(dealId);
        _assertEq(uint256(storedAcceptedAt), acceptedAt);
        _assertEq(uint256(deadline), acceptedAt + SERVICE_WINDOW);
        _assertEq(uint256(state), uint256(WrasseEscrow.State.Accepted));
    }

    function testCancelOnlyAfterAcceptanceWindow() public {
        uint256 dealId = _create();
        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.AcceptanceStillOpen.selector);
        escrow.cancelUnaccepted(dealId);

        vm.warp(block.timestamp + ACCEPT_WINDOW + 1);
        uint256 beforeBalance = BUYER.balance;
        vm.prank(BUYER);
        escrow.cancelUnaccepted(dealId);
        // Settlement assigns; it does not send. The escrow still holds the refund.
        _assertEq(BUYER.balance, beforeBalance);
        _assertEq(escrow.withdrawable(BUYER), PRICE);
        _assertEq(address(escrow).balance, PRICE);

        vm.prank(BUYER);
        escrow.withdraw(payable(BUYER));
        _assertEq(BUYER.balance, beforeBalance + PRICE);
        _assertEq(address(escrow).balance, 0);
    }

    function testDeliveredDealCanBeReleasedEarlyByBuyer() public {
        uint256 dealId = _acceptedDeal();
        vm.prank(PROVIDER);
        escrow.markDelivered(dealId);
        uint256 beforeBalance = PROVIDER.balance;
        vm.prank(BUYER);
        escrow.releaseDeal(dealId);
        _assertEq(PROVIDER.balance, beforeBalance);
        _assertEq(escrow.withdrawable(PROVIDER), PRICE + BOND);

        vm.prank(PROVIDER);
        escrow.withdraw(payable(PROVIDER));
        _assertEq(PROVIDER.balance, beforeBalance + PRICE + BOND);
        _assertEq(address(escrow).balance, 0);
    }

    function testProviderCanClaimAfterPayoutDelay() public {
        uint256 dealId = _acceptedDeal();
        vm.prank(PROVIDER);
        escrow.markDelivered(dealId);
        vm.prank(PROVIDER);
        vm.expectRevert(WrasseEscrow.PayoutNotReady.selector);
        escrow.claimPayment(dealId);

        vm.warp(block.timestamp + PAYOUT_DELAY);
        uint256 beforeBalance = PROVIDER.balance;
        vm.prank(PROVIDER);
        escrow.claimPayment(dealId);
        _assertEq(escrow.withdrawable(PROVIDER), PRICE + BOND);

        vm.prank(PROVIDER);
        escrow.withdraw(payable(PROVIDER));
        _assertEq(PROVIDER.balance, beforeBalance + PRICE + BOND);
    }

    function testProviderCannotMarkDeliveredAfterDeadline() public {
        uint256 dealId = _acceptedDeal();
        (,,,,,,, uint64 deadline,,,,) = escrow.deals(dealId);
        vm.warp(uint256(deadline) + 1);
        vm.prank(PROVIDER);
        vm.expectRevert(WrasseEscrow.DeadlinePassed.selector);
        escrow.markDelivered(dealId);
    }

    function testTimeoutReturnsPriceAndBondToBuyer() public {
        uint256 dealId = _acceptedDeal();
        (,,,,,,, uint64 deadline,,,,) = escrow.deals(dealId);
        vm.warp(uint256(deadline) + 1);
        uint256 beforeBalance = BUYER.balance;
        vm.prank(BUYER);
        escrow.claimTimeout(dealId);
        _assertEq(escrow.withdrawable(BUYER), PRICE + BOND);
        _assertEq(address(escrow).balance, PRICE + BOND);

        vm.prank(BUYER);
        escrow.withdraw(payable(BUYER));
        _assertEq(BUYER.balance, beforeBalance + PRICE + BOND);
        _assertEq(address(escrow).balance, 0);
    }

    function testOnlyProviderCanAcceptAndBondMustMatch() public {
        uint256 dealId = _create();
        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.Unauthorized.selector);
        escrow.acceptDeal{value: BOND}(dealId);

        vm.prank(PROVIDER);
        vm.expectRevert(abi.encodeWithSelector(WrasseEscrow.WrongBond.selector, BOND, BOND - 1));
        escrow.acceptDeal{value: BOND - 1}(dealId);
    }

    /// @notice Withdrawal is the only place the contract calls out, so it is the only place
    /// reentrancy can be attempted at all.
    function testWithdrawRejectsReentrancy() public {
        ReentrantBuyer attacker = new ReentrantBuyer(escrow);
        attacker.create{value: PRICE}(PROVIDER, uint64(block.timestamp + ACCEPT_WINDOW));
        vm.warp(block.timestamp + ACCEPT_WINDOW + 1);
        attacker.cancel();
        attacker.collect();

        require(!attacker.reentrySucceeded(), "reentrant call succeeded");
        _assertEq(address(attacker).balance, PRICE);
        _assertEq(escrow.withdrawable(address(attacker)), 0);
        _assertEq(address(escrow).balance, 0);
    }

    function testPolicyHashIsStoredExactly() public {
        uint256 dealId = _create();
        (,,,,,,,,,, bytes32 storedPolicyHash,) = escrow.deals(dealId);
        bytes32 expected = escrow.computePolicyHash(
            BUYER,
            PROVIDER,
            PRICE,
            2_000,
            lastAcceptBy,
            SERVICE_WINDOW,
            PAYOUT_DELAY,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
        _assertEq(uint256(storedPolicyHash), uint256(expected));
    }

    /// @notice The empty evidence set must hash identically in Solidity and Python.
    /// Every first deal of a relationship commits it on at least one side, so a mismatch
    /// here breaks bilateral deals silently rather than loudly.
    function testEmptyEvidenceSetMatchesPythonFixture() public pure {
        require(keccak256(abi.encode(new bytes32[](0))) == EMPTY_EVIDENCE_HASH);
    }

    function testCanonicalPolicyHashMatchesPythonFixture() public pure {
        bytes32 computed = keccak256(
            abi.encode(
                CANON_BUYER,
                CANON_PROVIDER,
                uint256(1 ether),
                uint256(2_000),
                CANON_ACCEPT_BY,
                uint64(7_200),
                uint64(1_800),
                ENGINE_VERSION_HASH,
                BUYER_EVIDENCE_HASH,
                EMPTY_EVIDENCE_HASH
            )
        );
        require(computed == POLICY_HASH);
    }

    function testContractComputesCanonicalPolicyHash() public view {
        bytes32 computed = escrow.computePolicyHash(
            CANON_BUYER,
            CANON_PROVIDER,
            1 ether,
            2_000,
            CANON_ACCEPT_BY,
            7_200,
            1_800,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
        require(computed == POLICY_HASH);
    }

    /// @notice The two evidence sides are not interchangeable. If swapping them left the
    /// commitment unchanged, two parameters would carry no more meaning than one.
    function testSwappingEvidenceSidesChangesCommitment() public view {
        bytes32 asIs = escrow.computePolicyHash(
            CANON_BUYER,
            CANON_PROVIDER,
            1 ether,
            2_000,
            CANON_ACCEPT_BY,
            7_200,
            1_800,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
        bytes32 swapped = escrow.computePolicyHash(
            CANON_BUYER,
            CANON_PROVIDER,
            1 ether,
            2_000,
            CANON_ACCEPT_BY,
            7_200,
            1_800,
            ENGINE_VERSION_HASH,
            EMPTY_EVIDENCE_HASH,
            BUYER_EVIDENCE_HASH
        );
        require(asIs != swapped);
    }

    /// @notice acceptBy and buyer are enforced by the contract, so both must be committed.
    function testBuyerAndAcceptByAreCommitted() public view {
        bytes32 base = escrow.computePolicyHash(
            CANON_BUYER,
            CANON_PROVIDER,
            1 ether,
            2_000,
            CANON_ACCEPT_BY,
            7_200,
            1_800,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
        bytes32 otherBuyer = escrow.computePolicyHash(
            address(0x5555555555555555555555555555555555555555),
            CANON_PROVIDER,
            1 ether,
            2_000,
            CANON_ACCEPT_BY,
            7_200,
            1_800,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
        bytes32 otherAcceptBy = escrow.computePolicyHash(
            CANON_BUYER,
            CANON_PROVIDER,
            1 ether,
            2_000,
            CANON_ACCEPT_BY + 1,
            7_200,
            1_800,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
        require(base != otherBuyer);
        require(base != otherAcceptBy);
    }

    /// @notice The log must carry basis points, not only the rounded absolute bond, or the
    /// preimage cannot be rebuilt from the receipt.
    function testDealCreatedCarriesBasisPoints() public {
        uint256 dealId = _create();
        // The stored bond is the rounded product; bps is unrecoverable from it alone.
        (,,, uint256 storedBond,,,,,,,,) = escrow.deals(dealId);
        _assertEq(storedBond, (PRICE * 2_000) / escrow.BPS_DENOMINATOR());
        // Recomputing the commitment requires the bps value, which the event supplies.
        (,,,,,,,,,, bytes32 storedPolicyHash,) = escrow.deals(dealId);
        bytes32 rebuilt = escrow.computePolicyHash(
            BUYER,
            PROVIDER,
            PRICE,
            2_000,
            lastAcceptBy,
            SERVICE_WINDOW,
            PAYOUT_DELAY,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
        _assertEq(uint256(storedPolicyHash), uint256(rebuilt));
    }

    function testRejectsDurationsBeyondBound() public {
        // Read the bound up front. An external call placed inside the argument list would
        // consume the vm.expectRevert intended for createDeal.
        uint64 maxDuration = escrow.MAX_DURATION();
        uint64 validAcceptBy = uint64(block.timestamp + ACCEPT_WINDOW);

        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.DurationOutOfRange.selector);
        escrow.createDeal{value: PRICE}(
            PROVIDER,
            2_000,
            uint64(block.timestamp) + maxDuration + 1,
            SERVICE_WINDOW,
            PAYOUT_DELAY,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );

        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.DurationOutOfRange.selector);
        escrow.createDeal{value: PRICE}(
            PROVIDER,
            2_000,
            validAcceptBy,
            maxDuration + 1,
            PAYOUT_DELAY,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );

        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.DurationOutOfRange.selector);
        escrow.createDeal{value: PRICE}(
            PROVIDER,
            2_000,
            validAcceptBy,
            SERVICE_WINDOW,
            maxDuration + 1,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );

        // The bound itself must remain accepted, so the check is a bound and not an off-by-one.
        vm.prank(BUYER);
        escrow.createDeal{value: PRICE}(
            PROVIDER,
            2_000,
            validAcceptBy,
            maxDuration,
            maxDuration,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
    }

    /// @notice A nonzero bond rate that truncates to zero wei must revert rather than
    /// silently sell unbonded protection.
    function testNonzeroRateCannotRoundToZeroBond() public {
        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.ZeroBond.selector);
        escrow.createDeal{value: 1 wei}(
            PROVIDER,
            1, // 0.01%, which truncates to 0 wei on a 1 wei price
            uint64(block.timestamp + ACCEPT_WINDOW),
            SERVICE_WINDOW,
            PAYOUT_DELAY,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
    }

    // ---------------------------------------------------------------------------------
    // Receipt reconstruction
    // ---------------------------------------------------------------------------------

    struct Decoded {
        uint256 createdCount;
        uint256 commitmentCount;
        uint256 dealIdCreated;
        address buyer;
        address provider;
        uint256 price;
        uint256 bondBps;
        uint64 acceptBy;
        uint64 serviceWindow;
        uint64 payoutDelay;
        bytes32 policyHashCreated;
        uint256 dealIdCommitment;
        bytes32 engineVersionHash;
        bytes32 buyerEvidenceHash;
        bytes32 providerEvidenceHash;
        bytes32 policyHashCommitment;
    }

    bytes32 private constant DEAL_CREATED_SIG =
        keccak256("DealCreated(uint256,address,address,uint256,uint256,uint256,uint64,uint64,uint64,bytes32)");
    bytes32 private constant DEAL_COMMITMENT_SIG = keccak256("DealCommitment(uint256,bytes32,bytes32,bytes32,bytes32)");

    /// @notice The claim under review: someone holding only the creation receipt can rebuild
    /// the commitment. Every input below is decoded from a log, never read from storage or
    /// borrowed from a fixture, so dropping a field from an event fails this test.
    function testCreationReceiptAloneRebuildsTheCommitment() public {
        vm.recordLogs();
        uint256 dealId = _create();
        Decoded memory d = _decodeCreation(vm.getRecordedLogs());

        require(d.createdCount == 1, "expected exactly one DealCreated from the escrow");
        require(d.commitmentCount == 1, "expected exactly one DealCommitment from the escrow");
        require(d.dealIdCreated == d.dealIdCommitment, "the two events describe different deals");
        require(d.policyHashCreated == d.policyHashCommitment, "the two events disagree on the commitment");
        _assertEq(d.dealIdCreated, dealId);

        bytes32 rebuilt = escrow.computePolicyHash(
            d.buyer,
            d.provider,
            d.price,
            d.bondBps,
            d.acceptBy,
            d.serviceWindow,
            d.payoutDelay,
            d.engineVersionHash,
            d.buyerEvidenceHash,
            d.providerEvidenceHash
        );
        require(rebuilt == d.policyHashCreated, "the receipt does not rebuild its own commitment");

        (,,,,,,,,,, bytes32 stored,) = escrow.deals(dealId);
        _assertEq(uint256(stored), uint256(rebuilt));
    }

    function _decodeCreation(Vm.Log[] memory logs) private view returns (Decoded memory d) {
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter != address(escrow)) continue;
            if (logs[i].topics[0] == DEAL_CREATED_SIG) {
                d.createdCount++;
                d.dealIdCreated = uint256(logs[i].topics[1]);
                d.buyer = address(uint160(uint256(logs[i].topics[2])));
                d.provider = address(uint160(uint256(logs[i].topics[3])));
                (d.price, d.bondBps,, d.acceptBy, d.serviceWindow, d.payoutDelay, d.policyHashCreated) =
                    abi.decode(logs[i].data, (uint256, uint256, uint256, uint64, uint64, uint64, bytes32));
            } else if (logs[i].topics[0] == DEAL_COMMITMENT_SIG) {
                d.commitmentCount++;
                d.dealIdCommitment = uint256(logs[i].topics[1]);
                (d.engineVersionHash, d.buyerEvidenceHash, d.providerEvidenceHash, d.policyHashCommitment) =
                    abi.decode(logs[i].data, (bytes32, bytes32, bytes32, bytes32));
            }
        }
    }

    /// @notice Swapping the two evidence sides is not a sufficient test on its own: an
    /// implementation that ignored one side entirely would still pass it. Each side is
    /// therefore mutated alone.
    function testMutatingEitherEvidenceSideAloneChangesTheCommitment() public view {
        bytes32 other = keccak256("a different recalled set");
        bytes32 base = _canonicalHash(BUYER_EVIDENCE_HASH, EMPTY_EVIDENCE_HASH);
        bytes32 buyerSideChanged = _canonicalHash(other, EMPTY_EVIDENCE_HASH);
        bytes32 providerSideChanged = _canonicalHash(BUYER_EVIDENCE_HASH, other);

        require(base != buyerSideChanged, "buyer evidence is not committed");
        require(base != providerSideChanged, "provider evidence is not committed");
        require(buyerSideChanged != providerSideChanged, "the two sides are interchangeable");
    }

    function _canonicalHash(bytes32 buyerEvidence, bytes32 providerEvidence) private view returns (bytes32) {
        return escrow.computePolicyHash(
            CANON_BUYER,
            CANON_PROVIDER,
            1 ether,
            2_000,
            CANON_ACCEPT_BY,
            7_200,
            1_800,
            ENGINE_VERSION_HASH,
            buyerEvidence,
            providerEvidence
        );
    }

    // ---------------------------------------------------------------------------------
    // Boundaries
    // ---------------------------------------------------------------------------------

    /// @notice The maximum acceptance horizon must itself be accepted, or the bound is an
    /// off-by-one rather than a bound. The window fields already cover this; acceptBy did not.
    function testAcceptByIsAcceptedAtExactlyTheMaximum() public {
        uint64 maxDuration = escrow.MAX_DURATION();
        vm.prank(BUYER);
        escrow.createDeal{value: PRICE}(
            PROVIDER,
            2_000,
            uint64(block.timestamp) + maxDuration,
            SERVICE_WINDOW,
            PAYOUT_DELAY,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
    }

    /// @notice A zero-length window is not a short deal, it is an unresolvable one.
    function testZeroDurationsAreRejected() public {
        uint64 validAcceptBy = uint64(block.timestamp + ACCEPT_WINDOW);

        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.DurationOutOfRange.selector);
        escrow.createDeal{value: PRICE}(
            PROVIDER, 2_000, validAcceptBy, 0, PAYOUT_DELAY, ENGINE_VERSION_HASH, BUYER_EVIDENCE_HASH, EMPTY_EVIDENCE_HASH
        );

        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.DurationOutOfRange.selector);
        escrow.createDeal{value: PRICE}(
            PROVIDER, 2_000, validAcceptBy, SERVICE_WINDOW, 0, ENGINE_VERSION_HASH, BUYER_EVIDENCE_HASH, EMPTY_EVIDENCE_HASH
        );

        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.DurationOutOfRange.selector);
        escrow.createDeal{value: PRICE}(
            PROVIDER,
            2_000,
            uint64(block.timestamp),
            SERVICE_WINDOW,
            PAYOUT_DELAY,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
    }

    /// @notice acceptBy is the last instant acceptance works and the last instant cancellation
    /// does not. The two must hand off with neither a gap nor an overlap.
    function testAcceptanceAndCancellationHandOffExactlyAtAcceptBy() public {
        uint256 first = _create();
        vm.warp(lastAcceptBy);

        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.AcceptanceStillOpen.selector);
        escrow.cancelUnaccepted(first);
        vm.prank(PROVIDER);
        escrow.acceptDeal{value: BOND}(first);

        uint256 second = _create();
        vm.warp(uint256(lastAcceptBy) + 1);
        vm.prank(PROVIDER);
        vm.expectRevert(WrasseEscrow.AcceptanceClosed.selector);
        escrow.acceptDeal{value: BOND}(second);
        vm.prank(BUYER);
        escrow.cancelUnaccepted(second);
    }

    /// @notice The service deadline hands off the same way: delivery is still open on the
    /// deadline itself, and the timeout claim only opens the instant after it.
    function testDeliveryAndTimeoutHandOffExactlyAtTheDeadline() public {
        uint256 first = _acceptedDeal();
        (,,,,,,, uint64 deadline,,,,) = escrow.deals(first);
        vm.warp(deadline);

        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.DeadlineNotPassed.selector);
        escrow.claimTimeout(first);
        vm.prank(PROVIDER);
        escrow.markDelivered(first);

        uint256 second = _acceptedDeal();
        (,,,,,,, uint64 secondDeadline,,,,) = escrow.deals(second);
        vm.warp(uint256(secondDeadline) + 1);
        vm.prank(PROVIDER);
        vm.expectRevert(WrasseEscrow.DeadlinePassed.selector);
        escrow.markDelivered(second);
        vm.prank(BUYER);
        escrow.claimTimeout(second);
    }

    // ---------------------------------------------------------------------------------
    // Liveness and solvency
    // ---------------------------------------------------------------------------------

    /// @notice The failure this design exists to remove. When settlement pushed value, a
    /// provider that refuses ETH left the deal stuck in Delivered forever, freezing the
    /// buyer's deposit alongside its own payout. Settlement must now complete regardless.
    function testRejectingProviderCannotStrandTheBuyersDeposit() public {
        RejectingParty rejecting = new RejectingParty(escrow);
        vm.deal(address(rejecting), BOND);

        lastAcceptBy = uint64(block.timestamp + ACCEPT_WINDOW);
        vm.prank(BUYER);
        uint256 dealId = escrow.createDeal{value: PRICE}(
            address(rejecting),
            2_000,
            lastAcceptBy,
            SERVICE_WINDOW,
            PAYOUT_DELAY,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
        rejecting.accept(dealId, BOND);
        rejecting.deliver(dealId);

        vm.prank(BUYER);
        escrow.releaseDeal(dealId);

        (,,,,,,,,,,, WrasseEscrow.State state) = escrow.deals(dealId);
        _assertEq(uint256(state), uint256(WrasseEscrow.State.Released));
        _assertEq(escrow.withdrawable(address(rejecting)), PRICE + BOND);

        // It can strand its own credit, and nothing else.
        vm.expectRevert(WrasseEscrow.TransferFailed.selector);
        rejecting.collect();

        // The failed withdrawal reverted whole: the credit is still owed and still held.
        _assertEq(escrow.withdrawable(address(rejecting)), PRICE + BOND);
        _assertEq(address(escrow).balance, PRICE + BOND);
    }

    /// @notice The mirror image: a buyer that refuses ETH must not be able to hold a deal in
    /// Accepted forever and freeze the provider's bond there.
    function testRejectingBuyerCannotFreezeTheDealInAcceptedState() public {
        RejectingParty rejecting = new RejectingParty(escrow);
        vm.deal(address(rejecting), PRICE);
        uint256 dealId = rejecting.create(PROVIDER, PRICE, uint64(block.timestamp + ACCEPT_WINDOW));

        vm.prank(PROVIDER);
        escrow.acceptDeal{value: BOND}(dealId);
        vm.warp(block.timestamp + 2 hours + 1);
        rejecting.claimTimeout(dealId);

        (,,,,,,,,,,, WrasseEscrow.State state) = escrow.deals(dealId);
        _assertEq(uint256(state), uint256(WrasseEscrow.State.TimedOut));
        _assertEq(escrow.withdrawable(address(rejecting)), PRICE + BOND);
        _assertEq(address(escrow).balance, PRICE + BOND);
    }

    /// @notice One address settling several deals holds a single summed balance, and one
    /// withdrawal drains all of it.
    function testCreditsFromSeveralDealsAggregateIntoOneBalance() public {
        _releaseFreshDeal();
        _releaseFreshDeal();
        _assertEq(escrow.withdrawable(PROVIDER), 2 * (PRICE + BOND));

        uint256 beforeBalance = PROVIDER.balance;
        vm.prank(PROVIDER);
        escrow.withdraw(payable(PROVIDER));
        _assertEq(PROVIDER.balance, beforeBalance + 2 * (PRICE + BOND));
        _assertEq(escrow.withdrawable(PROVIDER), 0);
        _assertEq(address(escrow).balance, 0);
    }

    /// @notice Settling or draining one deal must never reach another deal's money.
    function testConcurrentDealsAreFundedIndependently() public {
        uint256 first = _acceptedDeal();
        uint256 second = _acceptedDeal();
        _assertEq(address(escrow).balance, 2 * (PRICE + BOND));

        (,,,,,,, uint64 deadline,,,,) = escrow.deals(first);
        vm.warp(uint256(deadline) + 1);
        vm.prank(BUYER);
        escrow.claimTimeout(first);

        // Settling the first deal moves no ETH and leaves the second deal fully funded.
        _assertEq(escrow.withdrawable(BUYER), PRICE + BOND);
        _assertEq(address(escrow).balance, 2 * (PRICE + BOND));

        vm.prank(BUYER);
        escrow.withdraw(payable(BUYER));
        // Draining the settled credit leaves exactly the open deal's liability behind.
        _assertEq(address(escrow).balance, PRICE + BOND);

        vm.prank(BUYER);
        escrow.claimTimeout(second);
        vm.prank(BUYER);
        escrow.withdraw(payable(BUYER));
        _assertEq(address(escrow).balance, 0);
    }

    /// @notice A terminal deal is finished for everyone, which is what makes a second payout
    /// impossible rather than merely unlikely.
    function testTerminalStateRejectsEveryFurtherTransition() public {
        uint256 dealId = _releaseFreshDeal();

        vm.prank(BUYER);
        vm.expectRevert(
            abi.encodeWithSelector(
                WrasseEscrow.WrongState.selector, WrasseEscrow.State.Delivered, WrasseEscrow.State.Released
            )
        );
        escrow.releaseDeal(dealId);

        vm.prank(PROVIDER);
        vm.expectRevert(
            abi.encodeWithSelector(
                WrasseEscrow.WrongState.selector, WrasseEscrow.State.Delivered, WrasseEscrow.State.Released
            )
        );
        escrow.claimPayment(dealId);

        vm.prank(BUYER);
        vm.expectRevert(
            abi.encodeWithSelector(
                WrasseEscrow.WrongState.selector, WrasseEscrow.State.Accepted, WrasseEscrow.State.Released
            )
        );
        escrow.claimTimeout(dealId);

        vm.prank(BUYER);
        vm.expectRevert(
            abi.encodeWithSelector(
                WrasseEscrow.WrongState.selector, WrasseEscrow.State.Offered, WrasseEscrow.State.Released
            )
        );
        escrow.cancelUnaccepted(dealId);

        vm.prank(PROVIDER);
        vm.expectRevert(
            abi.encodeWithSelector(
                WrasseEscrow.WrongState.selector, WrasseEscrow.State.Offered, WrasseEscrow.State.Released
            )
        );
        escrow.acceptDeal{value: BOND}(dealId);

        _assertEq(escrow.withdrawable(PROVIDER), PRICE + BOND);
    }

    // ---------------------------------------------------------------------------------
    // Withdrawal
    // ---------------------------------------------------------------------------------

    function testWithdrawRejectsEmptyBalanceAndZeroRecipient() public {
        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.NothingToWithdraw.selector);
        escrow.withdraw(payable(BUYER));

        uint256 dealId = _create();
        vm.warp(block.timestamp + ACCEPT_WINDOW + 1);
        vm.prank(BUYER);
        escrow.cancelUnaccepted(dealId);

        vm.prank(BUYER);
        vm.expectRevert(WrasseEscrow.InvalidRecipient.selector);
        escrow.withdraw(payable(address(0)));
    }

    /// @notice The withdrawal destination is chosen at collection time and is deliberately not
    /// part of any commitment. It is not a negotiated term of the deal.
    function testCreditHolderMayNominateADifferentRecipient() public {
        address nominee = address(0xBEEF);
        uint256 dealId = _create();
        vm.warp(block.timestamp + ACCEPT_WINDOW + 1);
        vm.prank(BUYER);
        escrow.cancelUnaccepted(dealId);

        vm.prank(BUYER);
        escrow.withdraw(payable(nominee));
        _assertEq(nominee.balance, PRICE);
        _assertEq(escrow.withdrawable(BUYER), 0);
    }

    function _releaseFreshDeal() private returns (uint256 dealId) {
        dealId = _acceptedDeal();
        vm.prank(PROVIDER);
        escrow.markDelivered(dealId);
        vm.prank(BUYER);
        escrow.releaseDeal(dealId);
    }

    // ---------------------------------------------------------------------------------
    // Cross-language agreement
    // ---------------------------------------------------------------------------------

    /// @notice The Python producer claims to mirror this contract's creation rules. Matching
    /// constants would not show that; only running the same terms through both does. The same
    /// file drives `tests/test_policy_rules.py`.
    function testEveryPolicyVectorAgreesWithThePythonValidator() public {
        string memory json = vm.readFile("test/fixtures/policy-vectors.json");
        uint256[] memory price = vm.parseJsonUintArray(json, ".price");
        uint256[] memory bondBps = vm.parseJsonUintArray(json, ".bondBps");
        uint256[] memory acceptByOffset = vm.parseJsonUintArray(json, ".acceptByOffset");
        uint256[] memory serviceWindow = vm.parseJsonUintArray(json, ".serviceWindow");
        uint256[] memory payoutDelay = vm.parseJsonUintArray(json, ".payoutDelay");
        bool[] memory creatable = vm.parseJsonBoolArray(json, ".creatable");

        require(price.length > 0, "no vectors loaded");
        require(
            bondBps.length == price.length && acceptByOffset.length == price.length
                && serviceWindow.length == price.length && payoutDelay.length == price.length
                && creatable.length == price.length,
            "vector columns are ragged"
        );

        for (uint256 i = 0; i < price.length; i++) {
            _runVector(price[i], bondBps[i], acceptByOffset[i], serviceWindow[i], payoutDelay[i], creatable[i]);
        }
    }

    function _runVector(
        uint256 price,
        uint256 bondBps,
        uint256 acceptByOffset,
        uint256 serviceWindow,
        uint256 payoutDelay,
        bool creatable
    ) private {
        // A silent narrowing cast would let this harness exercise a truncated value while
        // Python exercised the original, so both suites could report the same verdict for
        // different reasons. An unrepresentable vector is a rejected producer input, and is
        // never handed to createDeal in a form the contract could accept.
        //
        // The sum is tested before it is taken, not after. Computing it first would panic on
        // a large offset before ever reaching this branch, so the guard would not be total
        // over the uint256 values the JSON can carry.
        if (
            acceptByOffset > type(uint64).max || serviceWindow > type(uint64).max
                || payoutDelay > type(uint64).max
                || block.timestamp > uint256(type(uint64).max) - acceptByOffset
        ) {
            require(!creatable, "a vector outside uint64 cannot be creatable");
            return;
        }

        vm.deal(BUYER, price);
        uint64 acceptBy = uint64(block.timestamp + acceptByOffset);

        vm.prank(BUYER);
        if (!creatable) {
            // The rejection reason varies, including an arithmetic panic on the overflow
            // vector, so only the verdict is compared.
            vm.expectRevert();
        }
        escrow.createDeal{value: price}(
            PROVIDER,
            bondBps,
            acceptBy,
            uint64(serviceWindow),
            uint64(payoutDelay),
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
    }

    function _create() private returns (uint256) {
        lastAcceptBy = uint64(block.timestamp + ACCEPT_WINDOW);
        vm.prank(BUYER);
        return escrow.createDeal{value: PRICE}(
            PROVIDER,
            2_000,
            lastAcceptBy,
            SERVICE_WINDOW,
            PAYOUT_DELAY,
            ENGINE_VERSION_HASH,
            BUYER_EVIDENCE_HASH,
            EMPTY_EVIDENCE_HASH
        );
    }

    function _accept(uint256 dealId) private {
        vm.prank(PROVIDER);
        escrow.acceptDeal{value: BOND}(dealId);
    }

    function _acceptedDeal() private returns (uint256 dealId) {
        dealId = _create();
        _accept(dealId);
    }

    function _assertEq(uint256 left, uint256 right) private pure {
        require(left == right, "assert eq failed");
    }
}

// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {RapportEscrow} from "../src/RapportEscrow.sol";

interface Vm {
    function deal(address who, uint256 newBalance) external;
    function prank(address sender) external;
    function startPrank(address sender) external;
    function stopPrank() external;
    function warp(uint256 newTimestamp) external;
    function expectRevert(bytes4 selector) external;
    function expectRevert(bytes calldata revertData) external;
}

contract ReentrantBuyer {
    RapportEscrow private immutable escrow;
    uint256 private dealId;
    bool private attempted;
    bool public reentrySucceeded;

    constructor(RapportEscrow target) {
        escrow = target;
    }

    function create(address provider, uint64 acceptBy) external payable {
        dealId =
            escrow.createDeal{value: msg.value}(provider, 2_000, acceptBy, 2 hours, 30 minutes, keccak256("policy"));
    }

    function cancel() external {
        escrow.cancelUnaccepted(dealId);
    }

    receive() external payable {
        if (!attempted) {
            attempted = true;
            (reentrySucceeded,) = address(escrow).call(abi.encodeCall(RapportEscrow.cancelUnaccepted, (dealId)));
        }
    }
}

contract RapportEscrowTest {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    RapportEscrow private escrow;
    address private constant BUYER = address(0xB0B);
    address private constant PROVIDER = address(0xA11CE);
    uint256 private constant PRICE = 1 ether;
    uint256 private constant BOND = 0.2 ether;
    uint64 private constant ACCEPT_WINDOW = 1 hours;
    uint64 private constant SERVICE_WINDOW = 2 hours;
    uint64 private constant PAYOUT_DELAY = 30 minutes;
    bytes32 private constant POLICY_HASH = keccak256("policy");

    function setUp() public {
        escrow = new RapportEscrow();
        vm.deal(BUYER, 10 ether);
        vm.deal(PROVIDER, 10 ether);
    }

    function testCreateAndAcceptUsesRelativeDeadline() public {
        uint256 dealId = _create();
        vm.warp(block.timestamp + 10 minutes);
        uint256 acceptedAt = block.timestamp;
        _accept(dealId);

        (,,,,, uint64 storedAcceptedAt,, uint64 deadline,,,, RapportEscrow.State state) = escrow.deals(dealId);
        _assertEq(uint256(storedAcceptedAt), acceptedAt);
        _assertEq(uint256(deadline), acceptedAt + SERVICE_WINDOW);
        _assertEq(uint256(state), uint256(RapportEscrow.State.Accepted));
    }

    function testCancelOnlyAfterAcceptanceWindow() public {
        uint256 dealId = _create();
        vm.prank(BUYER);
        vm.expectRevert(RapportEscrow.AcceptanceStillOpen.selector);
        escrow.cancelUnaccepted(dealId);

        vm.warp(block.timestamp + ACCEPT_WINDOW + 1);
        uint256 beforeBalance = BUYER.balance;
        vm.prank(BUYER);
        escrow.cancelUnaccepted(dealId);
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
        _assertEq(PROVIDER.balance, beforeBalance + PRICE + BOND);
        _assertEq(address(escrow).balance, 0);
    }

    function testProviderCanClaimAfterPayoutDelay() public {
        uint256 dealId = _acceptedDeal();
        vm.prank(PROVIDER);
        escrow.markDelivered(dealId);
        vm.prank(PROVIDER);
        vm.expectRevert(RapportEscrow.PayoutNotReady.selector);
        escrow.claimPayment(dealId);

        vm.warp(block.timestamp + PAYOUT_DELAY);
        uint256 beforeBalance = PROVIDER.balance;
        vm.prank(PROVIDER);
        escrow.claimPayment(dealId);
        _assertEq(PROVIDER.balance, beforeBalance + PRICE + BOND);
    }

    function testProviderCannotMarkDeliveredAfterDeadline() public {
        uint256 dealId = _acceptedDeal();
        (,,,,,,, uint64 deadline,,,,) = escrow.deals(dealId);
        vm.warp(uint256(deadline) + 1);
        vm.prank(PROVIDER);
        vm.expectRevert(RapportEscrow.DeadlinePassed.selector);
        escrow.markDelivered(dealId);
    }

    function testTimeoutReturnsPriceAndBondToBuyer() public {
        uint256 dealId = _acceptedDeal();
        (,,,,,,, uint64 deadline,,,,) = escrow.deals(dealId);
        vm.warp(uint256(deadline) + 1);
        uint256 beforeBalance = BUYER.balance;
        vm.prank(BUYER);
        escrow.claimTimeout(dealId);
        _assertEq(BUYER.balance, beforeBalance + PRICE + BOND);
        _assertEq(address(escrow).balance, 0);
    }

    function testOnlyProviderCanAcceptAndBondMustMatch() public {
        uint256 dealId = _create();
        vm.prank(BUYER);
        vm.expectRevert(RapportEscrow.Unauthorized.selector);
        escrow.acceptDeal{value: BOND}(dealId);

        vm.prank(PROVIDER);
        vm.expectRevert(abi.encodeWithSelector(RapportEscrow.WrongBond.selector, BOND, BOND - 1));
        escrow.acceptDeal{value: BOND - 1}(dealId);
    }

    function testRefundPathRejectsReentrancy() public {
        ReentrantBuyer attacker = new ReentrantBuyer(escrow);
        attacker.create{value: PRICE}(PROVIDER, uint64(block.timestamp + ACCEPT_WINDOW));
        vm.warp(block.timestamp + ACCEPT_WINDOW + 1);
        attacker.cancel();

        require(!attacker.reentrySucceeded(), "reentrant call succeeded");
        _assertEq(address(attacker).balance, PRICE);
        _assertEq(address(escrow).balance, 0);
    }

    function testPolicyHashIsStoredExactly() public {
        uint256 dealId = _create();
        (,,,,,,,,,, bytes32 storedPolicyHash,) = escrow.deals(dealId);
        _assertEq(uint256(storedPolicyHash), uint256(POLICY_HASH));
    }

    function testCanonicalPolicyHashMatchesPythonFixture() public pure {
        bytes32[] memory eventIds = new bytes32[](2);
        eventIds[0] = bytes32(uint256(0x1111111111111111111111111111111111111111111111111111111111111111));
        eventIds[1] = bytes32(uint256(0x2222222222222222222222222222222222222222222222222222222222222222));
        bytes32 evidenceHash = keccak256(abi.encode(eventIds));
        bytes32 computed = keccak256(
            abi.encode(
                address(0x3333333333333333333333333333333333333333),
                uint256(1 ether),
                uint256(2_000),
                uint64(7_200),
                uint64(1_800),
                keccak256(bytes("rapport/0.1.0")),
                evidenceHash
            )
        );
        require(evidenceHash == 0x2f685994ab703309ca4d0393ec2524b0368f819050ff85e7e3fb719cc5b48de3);
        require(computed == 0x37e7cc805371ef4785d992cac48ef3bcf082c19e706dbe057e009a9c41ad8484);
    }

    function _create() private returns (uint256) {
        vm.prank(BUYER);
        return escrow.createDeal{value: PRICE}(
            PROVIDER, 2_000, uint64(block.timestamp + ACCEPT_WINDOW), SERVICE_WINDOW, PAYOUT_DELAY, POLICY_HASH
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

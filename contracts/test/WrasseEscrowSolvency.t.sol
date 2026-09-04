// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {WrasseEscrow} from "../src/WrasseEscrow.sol";

interface Vm {
    function deal(address who, uint256 newBalance) external;
    function prank(address sender) external;
    function warp(uint256 newTimestamp) external;
}

/// @notice Drives the escrow through arbitrary interleavings so the solvency argument is
/// regression-protected rather than only reasoned about and demonstrated on two examples.
/// @dev Every call is wrapped, so a rejected transition is a no-op rather than an aborted
/// run. The escrow is created here rather than in the invariant contract's setUp, so the
/// fuzzer drives it through this handler and its authorisation rules.
contract SolvencyHandler {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    WrasseEscrow public escrow;
    address[4] public actors;
    uint256[] public dealIds;

    constructor() {
        escrow = new WrasseEscrow();
        actors = [address(0xA1), address(0xA2), address(0xA3), address(0xA4)];
    }

    /// @notice Price for an open offer, price plus bond once accepted, nothing once terminal.
    function openLiabilities() public view returns (uint256 total) {
        for (uint256 i = 0; i < dealIds.length; i++) {
            (,, uint256 price, uint256 bond,,,,,,,, WrasseEscrow.State state) = escrow.deals(dealIds[i]);
            if (state == WrasseEscrow.State.Offered) {
                total += price;
            } else if (state == WrasseEscrow.State.Accepted || state == WrasseEscrow.State.Delivered) {
                total += price + bond;
            }
        }
    }

    function outstandingCredits() public view returns (uint256 total) {
        for (uint256 i = 0; i < actors.length; i++) {
            total += escrow.withdrawable(actors[i]);
        }
    }

    function dealCount() external view returns (uint256) {
        return dealIds.length;
    }

    function createDeal(uint256 buyerSeed, uint256 priceSeed, uint256 bondSeed, uint256 windowSeed) external {
        address buyer = actors[buyerSeed % actors.length];
        address provider = actors[(buyerSeed + 1 + (priceSeed % (actors.length - 1))) % actors.length];
        if (buyer == provider) return;

        uint256 price = _bound(priceSeed, 1 ether, 100 ether);
        uint256 bondBps = _bound(bondSeed, 0, 10_000);
        uint64 window = uint64(_bound(windowSeed, 1, 2 days));

        vm.deal(buyer, buyer.balance + price);
        vm.prank(buyer);
        try escrow.createDeal{value: price}(
            provider,
            bondBps,
            uint64(block.timestamp + window),
            window,
            window,
            keccak256("wrasse/0.1.0"),
            keccak256("buyer-evidence"),
            keccak256("provider-evidence")
        ) returns (uint256 dealId) {
            dealIds.push(dealId);
        } catch {}
    }

    function acceptDeal(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pick(dealSeed);
        if (!ok) return;
        (, address provider,, uint256 bond,,,,,,,,) = escrow.deals(dealId);
        vm.deal(provider, provider.balance + bond);
        vm.prank(provider);
        try escrow.acceptDeal{value: bond}(dealId) {} catch {}
    }

    function markDelivered(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pick(dealSeed);
        if (!ok) return;
        (, address provider,,,,,,,,,,) = escrow.deals(dealId);
        vm.prank(provider);
        try escrow.markDelivered(dealId) {} catch {}
    }

    function releaseDeal(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pick(dealSeed);
        if (!ok) return;
        (address buyer,,,,,,,,,,,) = escrow.deals(dealId);
        vm.prank(buyer);
        try escrow.releaseDeal(dealId) {} catch {}
    }

    function claimPayment(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pick(dealSeed);
        if (!ok) return;
        (, address provider,,,,,,,,,,) = escrow.deals(dealId);
        vm.prank(provider);
        try escrow.claimPayment(dealId) {} catch {}
    }

    function claimTimeout(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pick(dealSeed);
        if (!ok) return;
        (address buyer,,,,,,,,,,,) = escrow.deals(dealId);
        vm.prank(buyer);
        try escrow.claimTimeout(dealId) {} catch {}
    }

    function cancelUnaccepted(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pick(dealSeed);
        if (!ok) return;
        (address buyer,,,,,,,,,,,) = escrow.deals(dealId);
        vm.prank(buyer);
        try escrow.cancelUnaccepted(dealId) {} catch {}
    }

    function withdraw(uint256 actorSeed) external {
        address actor = actors[actorSeed % actors.length];
        vm.prank(actor);
        try escrow.withdraw(payable(actor)) {} catch {}
    }

    /// @notice Time has to move or no deadline is ever reachable.
    function passTime(uint256 secondsForward) external {
        vm.warp(block.timestamp + _bound(secondsForward, 1, 3 days));
    }

    function _pick(uint256 seed) private view returns (uint256 dealId, bool ok) {
        if (dealIds.length == 0) return (0, false);
        return (dealIds[seed % dealIds.length], true);
    }

    function _bound(uint256 value, uint256 low, uint256 high) private pure returns (uint256) {
        return low + (value % (high - low + 1));
    }
}

contract WrasseEscrowSolvencyInvariant {
    SolvencyHandler private handler;

    function setUp() public {
        handler = new SolvencyHandler();
    }

    /// @notice The escrow must always hold enough to honour every deal still open and every
    /// credit not yet collected. Anything less means one deal can spend another's money.
    function invariant_balanceCoversLiabilitiesAndCredits() public view {
        uint256 balance = address(handler.escrow()).balance;
        uint256 owed = handler.openLiabilities() + handler.outstandingCredits();
        require(balance >= owed, "escrow cannot cover what it owes");
    }

    /// @notice Credit alone can never exceed the balance, which is the assertion that would
    /// break first if a terminal transition ever paid out twice.
    function invariant_creditsNeverExceedTheBalance() public view {
        require(
            handler.outstandingCredits() <= address(handler.escrow()).balance, "credits exceed the escrow balance"
        );
    }
}

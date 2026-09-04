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
/// @dev Selection is aware of both state and time, so an action is attempted only when the
/// escrow will accept it. Nothing is wrapped in try/catch and `fail_on_revert` is on, so the
/// campaign's zero-revert result means the transitions actually happened rather than that
/// their rejections were swallowed. The escrow is created here rather than in the invariant
/// contract's setUp, so the fuzzer drives it through this handler.
contract SolvencyHandler {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    WrasseEscrow public escrow;
    address[4] public actors;
    uint256[] public dealIds;

    // Successful transitions only. Catching every revert keeps a campaign running, but it
    // also means a regression that makes a transition always revert would leave the solvency
    // assertions green. These counters are what distinguishes "held" from "never reached".
    uint256 public created;
    uint256 public accepted;
    uint256 public delivered;
    uint256 public released;
    uint256 public paidOut;
    uint256 public timedOut;
    uint256 public cancelled;
    uint256 public withdrawn;

    function transitions() external view returns (uint256) {
        return created + accepted + delivered + released + paidOut + timedOut + cancelled + withdrawn;
    }

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

    /// @dev Which transition a candidate deal has to be ready for, in state and in time.
    enum Ready {
        Accept,
        Deliver,
        Release,
        Payment,
        Timeout,
        Cancel
    }

    function createDeal(uint256 buyerSeed, uint256 priceSeed, uint256 bondSeed, uint256 windowSeed) external {
        uint256 buyerIndex = buyerSeed % actors.length;
        uint256 offset = 1 + (priceSeed % (actors.length - 1));
        address buyer = actors[buyerIndex];
        address provider = actors[(buyerIndex + offset) % actors.length];

        uint256 price = _bound(priceSeed, 1 ether, 100 ether);
        uint256 bondBps = _bound(bondSeed, 0, 10_000);
        // Short enough that deadlines are reachable inside a campaign, long enough that the
        // acceptance and delivery windows are not trivially closed on the next call.
        uint64 window = uint64(_bound(windowSeed, 1 hours, 4 hours));

        vm.deal(buyer, buyer.balance + price);
        vm.prank(buyer);
        uint256 dealId = escrow.createDeal{value: price}(
            provider,
            bondBps,
            uint64(block.timestamp + window),
            window,
            window,
            keccak256("wrasse/0.1.0"),
            keccak256("buyer-evidence"),
            keccak256("provider-evidence")
        );
        dealIds.push(dealId);
        created++;
    }

    function acceptDeal(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pickReady(dealSeed, Ready.Accept);
        if (!ok) return;
        (, address provider,, uint256 bond,,,,,,,,) = escrow.deals(dealId);
        vm.deal(provider, provider.balance + bond);
        vm.prank(provider);
        escrow.acceptDeal{value: bond}(dealId);
        accepted++;
    }

    function markDelivered(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pickReady(dealSeed, Ready.Deliver);
        if (!ok) return;
        (, address provider,,,,,,,,,,) = escrow.deals(dealId);
        vm.prank(provider);
        escrow.markDelivered(dealId);
        delivered++;
    }

    function releaseDeal(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pickReady(dealSeed, Ready.Release);
        if (!ok) return;
        (address buyer,,,,,,,,,,,) = escrow.deals(dealId);
        vm.prank(buyer);
        escrow.releaseDeal(dealId);
        released++;
    }

    function claimPayment(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pickReady(dealSeed, Ready.Payment);
        if (!ok) return;
        (, address provider,,,,,,,,,,) = escrow.deals(dealId);
        vm.prank(provider);
        escrow.claimPayment(dealId);
        paidOut++;
    }

    function claimTimeout(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pickReady(dealSeed, Ready.Timeout);
        if (!ok) return;
        (address buyer,,,,,,,,,,,) = escrow.deals(dealId);
        vm.prank(buyer);
        escrow.claimTimeout(dealId);
        timedOut++;
    }

    function cancelUnaccepted(uint256 dealSeed) external {
        (uint256 dealId, bool ok) = _pickReady(dealSeed, Ready.Cancel);
        if (!ok) return;
        (address buyer,,,,,,,,,,,) = escrow.deals(dealId);
        vm.prank(buyer);
        escrow.cancelUnaccepted(dealId);
        cancelled++;
    }

    function withdraw(uint256 actorSeed) external {
        address actor = actors[actorSeed % actors.length];
        if (escrow.withdrawable(actor) == 0) return;
        vm.prank(actor);
        escrow.withdraw(payable(actor));
        withdrawn++;
    }

    /// @notice Time has to move or no deadline is ever reachable.
    function passTime(uint256 secondsForward) external {
        vm.warp(block.timestamp + _bound(secondsForward, 1, 2 hours));
    }

    /// @dev Finds a deal the escrow will actually accept this transition on, in state and in
    /// time. State alone is not enough: an offer past its acceptance deadline is still
    /// `Offered`, and attempting to accept it would be a rejection the handler had to swallow.
    /// Swallowed rejections are what made a zero-revert campaign mean less than it looked.
    function _pickReady(uint256 seed, Ready ready) private view returns (uint256, bool) {
        uint256 count = dealIds.length;
        if (count == 0) return (0, false);
        uint256 start = seed % count;
        for (uint256 i = 0; i < count; i++) {
            uint256 candidate = dealIds[(start + i) % count];
            if (_isReady(candidate, ready)) return (candidate, true);
        }
        return (0, false);
    }

    function _isReady(uint256 dealId, Ready ready) private view returns (bool) {
        (,,,, uint64 acceptBy,,, uint64 deadline,, uint64 payoutAvailableAt,, WrasseEscrow.State state) =
            escrow.deals(dealId);

        if (ready == Ready.Accept) {
            return state == WrasseEscrow.State.Offered && block.timestamp <= acceptBy;
        }
        if (ready == Ready.Cancel) {
            return state == WrasseEscrow.State.Offered && block.timestamp > acceptBy;
        }
        if (ready == Ready.Deliver) {
            return state == WrasseEscrow.State.Accepted && block.timestamp <= deadline;
        }
        if (ready == Ready.Timeout) {
            return state == WrasseEscrow.State.Accepted && block.timestamp > deadline;
        }
        if (ready == Ready.Release) {
            return state == WrasseEscrow.State.Delivered;
        }
        return state == WrasseEscrow.State.Delivered && block.timestamp >= payoutAvailableAt;
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

    /// @dev Without this the fuzzer also calls the escrow directly, where almost every call
    /// is rejected on authorisation and the campaign's depth is spent on nothing.
    function targetContracts() public view returns (address[] memory targets) {
        targets = new address[](1);
        targets[0] = address(handler);
    }

    /// @dev A coverage floor deliberately does NOT live here. `afterInvariant` participates
    /// in shrinking, so any assertion of the form "the campaign reached state X" is satisfied
    /// by shrinking the sequence to nothing and reporting that as the counterexample. It
    /// reports a failure without ever describing a real one. The question it was meant to
    /// answer, whether every lifecycle state is reachable through this handler at all, is
    /// answered deterministically by `SolvencyHandlerReachabilityTest` below. The counters on
    /// the handler remain, and a verbose run prints them.

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


/// @notice Answers a question the fuzzing campaign cannot: is every lifecycle state actually
/// reachable through the handler, or does the invariant hold only because some transition
/// silently never fires?
contract SolvencyHandlerReachabilityTest {
    SolvencyHandler private handler;

    function setUp() public {
        handler = new SolvencyHandler();
    }

    function testEveryLifecycleStateIsReachableThroughTheHandler() public {
        // Released, by the buyer.
        handler.createDeal(0, 1, 1, 1);
        handler.acceptDeal(0);
        handler.markDelivered(0);
        handler.releaseDeal(0);
        require(handler.released() == 1, "release never fired");

        // Released, by the provider after the payout delay.
        handler.createDeal(1, 1, 1, 1);
        handler.acceptDeal(0);
        handler.markDelivered(0);
        for (uint256 i = 0; i < 4; i++) {
            handler.passTime(7199);
        }
        handler.claimPayment(0);
        require(handler.paidOut() == 1, "claimPayment never fired");

        // Timed out.
        handler.createDeal(2, 1, 1, 1);
        handler.acceptDeal(0);
        for (uint256 i = 0; i < 4; i++) {
            handler.passTime(7199);
        }
        handler.claimTimeout(0);
        require(handler.timedOut() == 1, "claimTimeout never fired");

        // Cancelled after an unaccepted offer expired.
        handler.createDeal(3, 1, 1, 1);
        for (uint256 i = 0; i < 4; i++) {
            handler.passTime(7199);
        }
        handler.cancelUnaccepted(0);
        require(handler.cancelled() == 1, "cancelUnaccepted never fired");

        // And the credits those settlements produced can be collected.
        for (uint256 i = 0; i < 4; i++) {
            handler.withdraw(i);
        }
        require(handler.withdrawn() > 0, "withdraw never fired");
        require(handler.created() == 4 && handler.accepted() == 3, "creation or acceptance never fired");
        require(handler.delivered() == 2, "delivery never fired");
        require(handler.outstandingCredits() == 0, "credits were left behind");
        require(address(handler.escrow()).balance == handler.openLiabilities(), "balance does not match liabilities");
    }
}

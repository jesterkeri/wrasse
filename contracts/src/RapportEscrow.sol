// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @title RapportEscrow
/// @notice Minimal bilateral escrow committing each deal to memory-derived terms.
contract RapportEscrow {
    uint256 public constant BPS_DENOMINATOR = 10_000;
    uint256 public constant MAX_PROVIDER_BOND_BPS = 10_000;

    enum State {
        Offered,
        Accepted,
        Delivered,
        Released,
        TimedOut,
        Cancelled
    }

    struct Deal {
        address buyer;
        address provider;
        uint256 price;
        uint256 providerBond;
        uint64 acceptBy;
        uint64 acceptedAt;
        uint64 serviceWindow;
        uint64 deadline;
        uint64 payoutDelay;
        uint64 payoutAvailableAt;
        bytes32 policyHash;
        State state;
    }

    uint256 public nextDealId;
    mapping(uint256 dealId => Deal) public deals;
    uint256 private _locked = 1;

    error InvalidProvider();
    error InvalidTerms();
    error WrongState(State expected, State actual);
    error Unauthorized();
    error AcceptanceClosed();
    error AcceptanceStillOpen();
    error WrongBond(uint256 expected, uint256 received);
    error DeadlinePassed();
    error DeadlineNotPassed();
    error PayoutNotReady();
    error TransferFailed();
    error ReentrantCall();

    event DealCreated(
        uint256 indexed dealId,
        address indexed buyer,
        address indexed provider,
        uint256 price,
        uint256 providerBond,
        uint64 acceptBy,
        uint64 serviceWindow,
        uint64 payoutDelay,
        bytes32 policyHash
    );
    event DealAccepted(uint256 indexed dealId, uint64 acceptedAt, uint64 deadline);
    event DeliveryMarked(uint256 indexed dealId, uint64 deliveredAt, uint64 payoutAvailableAt);
    event DealReleased(uint256 indexed dealId, bool releasedByBuyer);
    event TimeoutClaimed(uint256 indexed dealId);
    event DealCancelled(uint256 indexed dealId);

    modifier nonReentrant() {
        if (_locked != 1) revert ReentrantCall();
        _locked = 2;
        _;
        _locked = 1;
    }

    function createDeal(
        address provider,
        uint256 providerBondBps,
        uint64 acceptBy,
        uint64 serviceWindow,
        uint64 payoutDelay,
        bytes32 policyHash
    ) external payable returns (uint256 dealId) {
        if (provider == address(0) || provider == msg.sender) revert InvalidProvider();
        if (
            msg.value == 0 || providerBondBps > MAX_PROVIDER_BOND_BPS || acceptBy <= block.timestamp
                || serviceWindow == 0 || payoutDelay == 0
        ) revert InvalidTerms();

        uint256 providerBond = (msg.value * providerBondBps) / BPS_DENOMINATOR;
        dealId = nextDealId++;
        deals[dealId] = Deal({
            buyer: msg.sender,
            provider: provider,
            price: msg.value,
            providerBond: providerBond,
            acceptBy: acceptBy,
            acceptedAt: 0,
            serviceWindow: serviceWindow,
            deadline: 0,
            payoutDelay: payoutDelay,
            payoutAvailableAt: 0,
            policyHash: policyHash,
            state: State.Offered
        });

        emit DealCreated(
            dealId, msg.sender, provider, msg.value, providerBond, acceptBy, serviceWindow, payoutDelay, policyHash
        );
    }

    function acceptDeal(uint256 dealId) external payable {
        Deal storage deal = deals[dealId];
        _requireState(deal, State.Offered);
        if (msg.sender != deal.provider) revert Unauthorized();
        if (block.timestamp > deal.acceptBy) revert AcceptanceClosed();
        if (msg.value != deal.providerBond) revert WrongBond(deal.providerBond, msg.value);

        uint64 acceptedAt = uint64(block.timestamp);
        uint64 deadline = acceptedAt + deal.serviceWindow;
        deal.acceptedAt = acceptedAt;
        deal.deadline = deadline;
        deal.state = State.Accepted;
        emit DealAccepted(dealId, acceptedAt, deadline);
    }

    function cancelUnaccepted(uint256 dealId) external nonReentrant {
        Deal storage deal = deals[dealId];
        _requireState(deal, State.Offered);
        if (msg.sender != deal.buyer) revert Unauthorized();
        if (block.timestamp <= deal.acceptBy) revert AcceptanceStillOpen();

        deal.state = State.Cancelled;
        uint256 refund = deal.price;
        emit DealCancelled(dealId);
        _sendValue(deal.buyer, refund);
    }

    function markDelivered(uint256 dealId) external {
        Deal storage deal = deals[dealId];
        _requireState(deal, State.Accepted);
        if (msg.sender != deal.provider) revert Unauthorized();
        if (block.timestamp > deal.deadline) revert DeadlinePassed();

        uint64 deliveredAt = uint64(block.timestamp);
        deal.payoutAvailableAt = deliveredAt + deal.payoutDelay;
        deal.state = State.Delivered;
        emit DeliveryMarked(dealId, deliveredAt, deal.payoutAvailableAt);
    }

    function releaseDeal(uint256 dealId) external nonReentrant {
        Deal storage deal = deals[dealId];
        _requireState(deal, State.Delivered);
        if (msg.sender != deal.buyer) revert Unauthorized();

        deal.state = State.Released;
        uint256 payout = deal.price + deal.providerBond;
        emit DealReleased(dealId, true);
        _sendValue(deal.provider, payout);
    }

    function claimPayment(uint256 dealId) external nonReentrant {
        Deal storage deal = deals[dealId];
        _requireState(deal, State.Delivered);
        if (msg.sender != deal.provider) revert Unauthorized();
        if (block.timestamp < deal.payoutAvailableAt) revert PayoutNotReady();

        deal.state = State.Released;
        uint256 payout = deal.price + deal.providerBond;
        emit DealReleased(dealId, false);
        _sendValue(deal.provider, payout);
    }

    function claimTimeout(uint256 dealId) external nonReentrant {
        Deal storage deal = deals[dealId];
        _requireState(deal, State.Accepted);
        if (msg.sender != deal.buyer) revert Unauthorized();
        if (block.timestamp <= deal.deadline) revert DeadlineNotPassed();

        deal.state = State.TimedOut;
        uint256 refundAndBond = deal.price + deal.providerBond;
        emit TimeoutClaimed(dealId);
        _sendValue(deal.buyer, refundAndBond);
    }

    function _requireState(Deal storage deal, State expected) private view {
        if (deal.state != expected) revert WrongState(expected, deal.state);
    }

    function _sendValue(address recipient, uint256 amount) private {
        (bool success,) = payable(recipient).call{value: amount}("");
        if (!success) revert TransferFailed();
    }
}


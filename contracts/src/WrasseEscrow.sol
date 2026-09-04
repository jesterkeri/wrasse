// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @title WrasseEscrow
/// @notice Minimal bilateral escrow committing each deal to memory-derived terms.
contract WrasseEscrow {
    uint256 public constant BPS_DENOMINATOR = 10_000;
    uint256 public constant MAX_PROVIDER_BOND_BPS = 10_000;
    /// @notice Upper bound on every caller-supplied duration. A typo must not create a deal
    /// that nobody can resolve for years.
    uint64 public constant MAX_DURATION = 30 days;

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
    /// @notice Settlement proceeds assigned to an address but not yet collected.
    /// @dev Credits accumulate, so an address that settles several deals holds one summed
    /// balance rather than a queue of per-deal entries.
    mapping(address account => uint256 amount) public withdrawable;
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
    error DurationOutOfRange();
    error ZeroBond();
    error NothingToWithdraw();
    error InvalidRecipient();

    /// @notice The economic terms of a deal, in the form the commitment covers.
    /// @dev `providerBondBps` is emitted alongside the absolute `providerBond` because the
    /// commitment folds the basis points, and integer division makes them unrecoverable
    /// from the absolute figure alone. Without it the preimage cannot be rebuilt from logs.
    event DealCreated(
        uint256 indexed dealId,
        address indexed buyer,
        address indexed provider,
        uint256 price,
        uint256 providerBondBps,
        uint256 providerBond,
        uint64 acceptBy,
        uint64 serviceWindow,
        uint64 payoutDelay,
        bytes32 policyHash
    );

    /// @notice The memory half of the commitment, emitted in the same transaction as
    /// `DealCreated`. Split from it so neither event overflows the stack.
    /// @param buyerEvidenceHash commitment to receipts the BUYER recalled ABOUT THE PROVIDER
    /// @param providerEvidenceHash commitment to receipts the PROVIDER recalled ABOUT THE BUYER
    event DealCommitment(
        uint256 indexed dealId,
        bytes32 engineVersionHash,
        bytes32 buyerEvidenceHash,
        bytes32 providerEvidenceHash,
        bytes32 policyHash
    );
    event DealAccepted(uint256 indexed dealId, uint64 acceptedAt, uint64 deadline);
    event DeliveryMarked(uint256 indexed dealId, uint64 deliveredAt, uint64 payoutAvailableAt);
    event DealReleased(uint256 indexed dealId, bool releasedByBuyer);
    event TimeoutClaimed(uint256 indexed dealId);
    event DealCancelled(uint256 indexed dealId);
    /// @notice Settlement has assigned `amount` to `recipient`, claimable via `withdraw`.
    /// @dev Deliberately distinct from `Withdrawn`. Settling a deal and collecting the
    /// proceeds are separate facts, and a receipt must never conflate them.
    event PayoutCredited(uint256 indexed dealId, address indexed recipient, uint256 amount);
    /// @notice A credited balance has left the contract.
    event Withdrawn(address indexed account, address indexed recipient, uint256 amount);

    /// @dev Kept on the credit-producing terminal transitions even though none of them calls
    /// out any more. They share the lock with `withdraw`, so a recipient cannot re-enter a
    /// settlement while its own withdrawal is in flight, and a future edit that reintroduces
    /// a call inherits the guard rather than silently losing it. `createDeal`, `acceptDeal`
    /// and `markDelivered` are deliberately unguarded: they produce no credit, and both
    /// payable ones add funded liability equal to the value they receive.
    modifier nonReentrant() {
        if (_locked != 1) revert ReentrantCall();
        _locked = 2;
        _;
        _locked = 1;
    }

    /// @notice Open a funded offer whose terms are committed to onchain.
    /// @param buyerEvidenceHash commitment to receipts the BUYER recalled ABOUT THE PROVIDER
    /// @param providerEvidenceHash commitment to receipts the PROVIDER recalled ABOUT THE BUYER
    function createDeal(
        address provider,
        uint256 providerBondBps,
        uint64 acceptBy,
        uint64 serviceWindow,
        uint64 payoutDelay,
        bytes32 engineVersionHash,
        bytes32 buyerEvidenceHash,
        bytes32 providerEvidenceHash
    ) external payable returns (uint256 dealId) {
        if (provider == address(0) || provider == msg.sender) revert InvalidProvider();
        if (msg.value == 0 || providerBondBps > MAX_PROVIDER_BOND_BPS) revert InvalidTerms();
        if (
            acceptBy <= block.timestamp || acceptBy - uint64(block.timestamp) > MAX_DURATION
                || serviceWindow == 0 || serviceWindow > MAX_DURATION || payoutDelay == 0
                || payoutDelay > MAX_DURATION
        ) revert DurationOutOfRange();

        uint256 providerBond = (msg.value * providerBondBps) / BPS_DENOMINATOR;
        // Integer division truncates, so a nonzero rate on a small price can round to a zero
        // bond. Selling unbonded protection silently would be worse than refusing the deal.
        if (providerBondBps > 0 && providerBond == 0) revert ZeroBond();

        bytes32 policyHash = computePolicyHash(
            msg.sender,
            provider,
            msg.value,
            providerBondBps,
            acceptBy,
            serviceWindow,
            payoutDelay,
            engineVersionHash,
            buyerEvidenceHash,
            providerEvidenceHash
        );
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
            dealId,
            msg.sender,
            provider,
            msg.value,
            providerBondBps,
            providerBond,
            acceptBy,
            serviceWindow,
            payoutDelay,
            policyHash
        );
        emit DealCommitment(dealId, engineVersionHash, buyerEvidenceHash, providerEvidenceHash, policyHash);
    }

    /// @notice Reproduce the exact commitment stored when a deal is created.
    /// @dev Every field here is a term the contract itself enforces. `buyer` and `acceptBy`
    /// were previously enforced but uncommitted, which made the claim that the contract
    /// commits to the terms it enforces untrue.
    /// @param buyerEvidenceHash commitment to receipts the BUYER recalled ABOUT THE PROVIDER
    /// @param providerEvidenceHash commitment to receipts the PROVIDER recalled ABOUT THE BUYER
    function computePolicyHash(
        address buyer,
        address provider,
        uint256 price,
        uint256 providerBondBps,
        uint64 acceptBy,
        uint64 serviceWindow,
        uint64 payoutDelay,
        bytes32 engineVersionHash,
        bytes32 buyerEvidenceHash,
        bytes32 providerEvidenceHash
    ) public pure returns (bytes32) {
        return keccak256(
            abi.encode(
                buyer,
                provider,
                price,
                providerBondBps,
                acceptBy,
                serviceWindow,
                payoutDelay,
                engineVersionHash,
                buyerEvidenceHash,
                providerEvidenceHash
            )
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
        _credit(dealId, deal.buyer, refund);
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
        _credit(dealId, deal.provider, payout);
    }

    function claimPayment(uint256 dealId) external nonReentrant {
        Deal storage deal = deals[dealId];
        _requireState(deal, State.Delivered);
        if (msg.sender != deal.provider) revert Unauthorized();
        if (block.timestamp < deal.payoutAvailableAt) revert PayoutNotReady();

        deal.state = State.Released;
        uint256 payout = deal.price + deal.providerBond;
        emit DealReleased(dealId, false);
        _credit(dealId, deal.provider, payout);
    }

    function claimTimeout(uint256 dealId) external nonReentrant {
        Deal storage deal = deals[dealId];
        _requireState(deal, State.Accepted);
        if (msg.sender != deal.buyer) revert Unauthorized();
        if (block.timestamp <= deal.deadline) revert DeadlineNotPassed();

        deal.state = State.TimedOut;
        uint256 refundAndBond = deal.price + deal.providerBond;
        emit TimeoutClaimed(dealId);
        _credit(dealId, deal.buyer, refundAndBond);
    }

    /// @notice Collect everything this caller has been credited, to an address of its choice.
    /// @dev The only external call in the contract. No state transition sends value, so a
    /// participant that refuses ETH can strand its own credit and nothing else. The
    /// destination is chosen at collection time and is deliberately not part of any deal
    /// commitment: it is not a negotiated term.
    /// @param recipient where to send the balance; the caller may nominate any address.
    function withdraw(address payable recipient) external nonReentrant returns (uint256 amount) {
        if (recipient == address(0)) revert InvalidRecipient();
        amount = withdrawable[msg.sender];
        if (amount == 0) revert NothingToWithdraw();

        withdrawable[msg.sender] = 0;
        emit Withdrawn(msg.sender, recipient, amount);
        (bool success,) = recipient.call{value: amount}("");
        if (!success) revert TransferFailed();
    }

    function _requireState(Deal storage deal, State expected) private view {
        if (deal.state != expected) revert WrongState(expected, deal.state);
    }

    /// @dev Assigns proceeds without calling out. Keeping settlement free of external calls
    /// is what makes a terminal transition impossible for a recipient to block.
    function _credit(uint256 dealId, address recipient, uint256 amount) private {
        withdrawable[recipient] += amount;
        emit PayoutCredited(dealId, recipient, amount);
    }
}

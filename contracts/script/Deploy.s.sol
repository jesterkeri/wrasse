// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {RapportEscrow} from "../src/RapportEscrow.sol";

interface Vm {
    function startBroadcast() external;
    function stopBroadcast() external;
}

contract DeployRapportEscrow {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function run() external returns (RapportEscrow escrow) {
        vm.startBroadcast();
        escrow = new RapportEscrow();
        vm.stopBroadcast();
    }
}


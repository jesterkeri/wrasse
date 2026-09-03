// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {WrasseEscrow} from "../src/WrasseEscrow.sol";

interface Vm {
    function startBroadcast() external;
    function stopBroadcast() external;
}

contract DeployWrasseEscrow {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function run() external returns (WrasseEscrow escrow) {
        vm.startBroadcast();
        escrow = new WrasseEscrow();
        vm.stopBroadcast();
    }
}


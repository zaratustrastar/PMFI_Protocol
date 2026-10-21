// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
// LOCAL TEST FIXTURES ONLY. Never deploy these contracts on public networks.
contract Token {
 string public constant symbol = "PMFI";
 uint8 public constant decimals = 18;
 mapping(address=>uint256) public balanceOf;
 bool public fail;
 bool public taxed;
 event Transfer(address indexed from,address indexed to,uint256 value);
 constructor(){balanceOf[msg.sender]=1000000 ether;emit Transfer(address(0),msg.sender,1000000 ether);}
 function setFail(bool value) external {fail=value;}
 function setTaxed(bool value) external {taxed=value;}
 function transfer(address to,uint256 value) external returns(bool){
   require(!fail,"TEST_REVERT");require(balanceOf[msg.sender]>=value,"BALANCE");
   uint256 tax=taxed?value/10:0;balanceOf[msg.sender]-=value;balanceOf[to]+=value-tax;
   emit Transfer(msg.sender,to,value-tax);if(tax>0)emit Transfer(msg.sender,address(0),tax);return true;
 }
}
contract TestSafe {
 address[] private owners;
 constructor(address a,address b){owners.push(a);owners.push(b);}
 function getOwners() external view returns(address[] memory){return owners;}
 function getThreshold() external pure returns(uint256){return 2;}
 function masterCopy() external view returns(address){return address(this);}
 function isValidSignature(bytes32,bytes calldata) external pure returns(bytes4){return 0x1626ba7e;}
}

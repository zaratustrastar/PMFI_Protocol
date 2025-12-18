const { ethers } = require('ethers');
require('dotenv').config();

const VAULT_ADDRESS = '0x5f10aF485267A33121D4d877708ef99E7dD04E00';
const NEW_POLYMARKET_WALLET = '0x9cbc5577e6f1B3a097d579C6A28D0cDbd247B6A5';

const VAULT_ABI = [
  'function polymarketWallet() view returns (address)',
  'function owner() view returns (address)',
  'function setPolymarketWallet(address _polymarketWallet) external',
  'event PolymarketWalletUpdated(address indexed oldWallet, address indexed newWallet)'
];

async function main() {
  console.log('🔧 Updating Polymarket Wallet on V7 Vault');
  console.log('=========================================');
  
  const rpcUrl = 'https://mainnet.base.org';
  const privateKey = process.env.ORACLE_PRIVATE_KEY || process.env.METAMASK_PRIVATE_KEY;
  
  if (!privateKey) {
    throw new Error('No private key found. Set ORACLE_PRIVATE_KEY or METAMASK_PRIVATE_KEY');
  }
  
  const provider = new ethers.JsonRpcProvider(rpcUrl);
  const wallet = new ethers.Wallet(privateKey, provider);
  const vault = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, wallet);
  
  console.log('Vault Address:', VAULT_ADDRESS);
  console.log('Caller:', wallet.address);
  
  const owner = await vault.owner();
  console.log('Contract Owner:', owner);
  
  if (owner.toLowerCase() !== wallet.address.toLowerCase()) {
    throw new Error(`You are not the owner. Owner is ${owner}, you are ${wallet.address}`);
  }
  
  const currentWallet = await vault.polymarketWallet();
  console.log('Current Polymarket Wallet:', currentWallet);
  console.log('New Polymarket Wallet:', NEW_POLYMARKET_WALLET);
  
  if (currentWallet.toLowerCase() === NEW_POLYMARKET_WALLET.toLowerCase()) {
    console.log('✅ Already set to the new wallet address!');
    return;
  }
  
  console.log('\n📝 Sending transaction to update wallet...');
  
  const tx = await vault.setPolymarketWallet(NEW_POLYMARKET_WALLET);
  console.log('Transaction hash:', tx.hash);
  console.log('Waiting for confirmation...');
  
  const receipt = await tx.wait();
  console.log('✅ Transaction confirmed in block:', receipt.blockNumber);
  
  const updatedWallet = await vault.polymarketWallet();
  console.log('\n✅ Polymarket Wallet updated to:', updatedWallet);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error('❌ Error:', error.message);
    process.exit(1);
  });

/**
 * PredictFi Sniper Vault - Frontend
 * 
 * Simple interface to deposit/withdraw USDC from the vault.
 * Uses ethers.js v6 and MetaMask.
 */

// =============================================================================
// CONFIGURATION - Update these after deploying contracts
// =============================================================================

// Testnet addresses (update after deployment)
const VAULT_ADDRESS = "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0";  // Update this
const USDC_ADDRESS = "0x5FbDB2315678afecb367f032d93F642f64180aa3";   // Update this

// USDC has 6 decimals
const USDC_DECIMALS = 6;

// =============================================================================
// ABIs (minimal - only functions we need)
// =============================================================================

const VAULT_ABI = [
    "function deposit(uint256 assets, address receiver) returns (uint256)",
    "function withdraw(uint256 assets, address receiver, address owner) returns (uint256)",
    "function totalAssets() view returns (uint256)",
    "function balanceOf(address account) view returns (uint256)",
    "function convertToAssets(uint256 shares) view returns (uint256)",
    "function maxDeposit(address) view returns (uint256)",
    "function asset() view returns (address)"
];

const USDC_ABI = [
    "function approve(address spender, uint256 amount) returns (bool)",
    "function allowance(address owner, address spender) view returns (uint256)",
    "function balanceOf(address account) view returns (uint256)",
    "function decimals() view returns (uint8)"
];

// =============================================================================
// STATE
// =============================================================================

let provider = null;
let signer = null;
let userAddress = null;
let vaultContract = null;
let usdcContract = null;

// =============================================================================
// DOM ELEMENTS
// =============================================================================

const connectBtn = document.getElementById("connectBtn");
const walletInfo = document.getElementById("walletInfo");
const userAddressEl = document.getElementById("userAddress");
const usdcBalanceEl = document.getElementById("usdcBalance");
const vaultSharesEl = document.getElementById("vaultShares");
const vaultAddressEl = document.getElementById("vaultAddress");
const totalAssetsEl = document.getElementById("totalAssets");
const redeemableEl = document.getElementById("redeemable");
const depositAmountEl = document.getElementById("depositAmount");
const depositBtn = document.getElementById("depositBtn");
const depositStatus = document.getElementById("depositStatus");
const withdrawAmountEl = document.getElementById("withdrawAmount");
const withdrawBtn = document.getElementById("withdrawBtn");
const withdrawStatus = document.getElementById("withdrawStatus");

// =============================================================================
// UTILITY FUNCTIONS
// =============================================================================

function formatUSDC(amount) {
    return (Number(amount) / 10 ** USDC_DECIMALS).toFixed(2);
}

function parseUSDC(amount) {
    return BigInt(Math.floor(Number(amount) * 10 ** USDC_DECIMALS));
}

function shortenAddress(address) {
    return address.slice(0, 6) + "..." + address.slice(-4);
}

function showStatus(element, message, type) {
    element.textContent = message;
    element.className = `status ${type}`;
    element.classList.remove("hidden");
}

function hideStatus(element) {
    element.classList.add("hidden");
}

// =============================================================================
// WALLET CONNECTION
// =============================================================================

async function connectWallet() {
    if (typeof window.ethereum === "undefined") {
        alert("Please install MetaMask to use this app!");
        return;
    }

    try {
        connectBtn.textContent = "Connecting...";
        connectBtn.disabled = true;

        // Request account access
        const accounts = await window.ethereum.request({
            method: "eth_requestAccounts"
        });

        // Create provider and signer
        provider = new ethers.BrowserProvider(window.ethereum);
        signer = await provider.getSigner();
        userAddress = await signer.getAddress();

        // Instantiate contracts
        vaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);
        usdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, signer);

        // Update UI
        connectBtn.textContent = "Connected";
        walletInfo.classList.remove("hidden");
        userAddressEl.textContent = shortenAddress(userAddress);
        vaultAddressEl.textContent = shortenAddress(VAULT_ADDRESS);

        // Enable buttons
        depositBtn.disabled = false;
        withdrawBtn.disabled = false;

        // Load balances
        await refreshBalances();

        // Listen for account changes
        window.ethereum.on("accountsChanged", handleAccountsChanged);

    } catch (error) {
        console.error("Connection error:", error);
        connectBtn.textContent = "Connect MetaMask";
        connectBtn.disabled = false;
        alert("Failed to connect: " + error.message);
    }
}

function handleAccountsChanged(accounts) {
    if (accounts.length === 0) {
        // User disconnected
        location.reload();
    } else {
        // User switched accounts
        userAddress = accounts[0];
        userAddressEl.textContent = shortenAddress(userAddress);
        refreshBalances();
    }
}

// =============================================================================
// BALANCE REFRESH
// =============================================================================

async function refreshBalances() {
    if (!userAddress || !vaultContract || !usdcContract) return;

    try {
        // Get USDC balance
        const usdcBalance = await usdcContract.balanceOf(userAddress);
        usdcBalanceEl.textContent = formatUSDC(usdcBalance) + " USDC";

        // Get vault share balance
        const shares = await vaultContract.balanceOf(userAddress);
        vaultSharesEl.textContent = formatUSDC(shares) + " pSNIPER";

        // Get redeemable amount
        const redeemable = await vaultContract.convertToAssets(shares);
        redeemableEl.textContent = formatUSDC(redeemable) + " USDC";

        // Get total vault assets
        const totalAssets = await vaultContract.totalAssets();
        totalAssetsEl.textContent = formatUSDC(totalAssets) + " USDC";

    } catch (error) {
        console.error("Error refreshing balances:", error);
    }
}

// =============================================================================
// DEPOSIT
// =============================================================================

async function handleDeposit() {
    const amountStr = depositAmountEl.value;
    if (!amountStr || Number(amountStr) <= 0) {
        showStatus(depositStatus, "Please enter a valid amount", "error");
        return;
    }

    const amount = parseUSDC(amountStr);

    try {
        depositBtn.disabled = true;
        hideStatus(depositStatus);

        // Step 1: Check allowance
        showStatus(depositStatus, "Checking allowance...", "info");
        const allowance = await usdcContract.allowance(userAddress, VAULT_ADDRESS);

        if (allowance < amount) {
            // Step 2: Approve
            showStatus(depositStatus, "Approving USDC... Please confirm in MetaMask", "info");
            const approveTx = await usdcContract.approve(VAULT_ADDRESS, amount);
            showStatus(depositStatus, "Waiting for approval confirmation...", "info");
            await approveTx.wait();
        }

        // Step 3: Deposit
        showStatus(depositStatus, "Depositing... Please confirm in MetaMask", "info");
        const depositTx = await vaultContract.deposit(amount, userAddress);
        showStatus(depositStatus, "Waiting for deposit confirmation...", "info");
        await depositTx.wait();

        // Success
        showStatus(depositStatus, `Successfully deposited ${amountStr} USDC!`, "success");
        depositAmountEl.value = "";
        await refreshBalances();

    } catch (error) {
        console.error("Deposit error:", error);
        showStatus(depositStatus, "Deposit failed: " + (error.reason || error.message), "error");
    } finally {
        depositBtn.disabled = false;
    }
}

// =============================================================================
// WITHDRAW
// =============================================================================

async function handleWithdraw() {
    const amountStr = withdrawAmountEl.value;
    if (!amountStr || Number(amountStr) <= 0) {
        showStatus(withdrawStatus, "Please enter a valid amount", "error");
        return;
    }

    const amount = parseUSDC(amountStr);

    try {
        withdrawBtn.disabled = true;
        hideStatus(withdrawStatus);

        // Withdraw
        showStatus(withdrawStatus, "Withdrawing... Please confirm in MetaMask", "info");
        const withdrawTx = await vaultContract.withdraw(amount, userAddress, userAddress);
        showStatus(withdrawStatus, "Waiting for withdrawal confirmation...", "info");
        await withdrawTx.wait();

        // Success
        showStatus(withdrawStatus, `Successfully withdrew ${amountStr} USDC!`, "success");
        withdrawAmountEl.value = "";
        await refreshBalances();

    } catch (error) {
        console.error("Withdraw error:", error);
        showStatus(withdrawStatus, "Withdraw failed: " + (error.reason || error.message), "error");
    } finally {
        withdrawBtn.disabled = false;
    }
}

// =============================================================================
// EVENT LISTENERS
// =============================================================================

connectBtn.addEventListener("click", connectWallet);
depositBtn.addEventListener("click", handleDeposit);
withdrawBtn.addEventListener("click", handleWithdraw);

// Auto-connect if already authorized
if (typeof window.ethereum !== "undefined") {
    window.ethereum.request({ method: "eth_accounts" }).then(accounts => {
        if (accounts.length > 0) {
            connectWallet();
        }
    });
}

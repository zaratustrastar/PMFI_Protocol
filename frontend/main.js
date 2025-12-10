/**
 * PredictFi Sniper Vault V2 - Frontend
 * 
 * Interface to interact with the vault on Base Sepolia.
 * Shows real-time vault stats and user position.
 */

// =============================================================================
// CONFIGURATION - Base Sepolia V2 Deployment
// =============================================================================

const VAULT_ADDRESS = "0x26BCAe8DEA9A2b04a522cab2679CF9708d3F84E3";
const USDC_ADDRESS = "0x743dBb99B51A542aA7b6E859713b4b615445C019";
const USDC_DECIMALS = 6;
const REFRESH_INTERVAL = 30000; // 30 seconds

// =============================================================================
// ABIs
// =============================================================================

let VAULT_ABI = null;
let USDC_ABI = null;

async function loadABIs() {
    try {
        const [vaultResponse, usdcResponse] = await Promise.all([
            fetch("abis/vault.json"),
            fetch("abis/usdc.json")
        ]);
        
        if (!vaultResponse.ok || !usdcResponse.ok) {
            throw new Error("Failed to load ABI files");
        }
        
        VAULT_ABI = await vaultResponse.json();
        USDC_ABI = await usdcResponse.json();
        
        console.log("ABIs loaded successfully");
        return true;
    } catch (error) {
        console.error("Error loading ABIs:", error);
        return false;
    }
}

// =============================================================================
// STATE
// =============================================================================

let provider = null;
let signer = null;
let userAddress = null;
let vaultContract = null;
let usdcContract = null;
let abisLoaded = false;
let refreshTimer = null;

// =============================================================================
// DOM ELEMENTS
// =============================================================================

const connectBtn = document.getElementById("connectBtn");
const walletAddressEl = document.getElementById("walletAddress");
const tvlValueEl = document.getElementById("tvlValue");
const sharePriceValueEl = document.getElementById("sharePriceValue");
const globalCapValueEl = document.getElementById("globalCapValue");
const walletCapValueEl = document.getElementById("walletCapValue");
const usdcBalanceEl = document.getElementById("usdcBalance");
const vaultSharesEl = document.getElementById("vaultShares");
const redeemableValueEl = document.getElementById("redeemableValue");
const remainingCapacityEl = document.getElementById("remainingCapacity");
const depositAmountEl = document.getElementById("depositAmount");
const depositBtn = document.getElementById("depositBtn");
const depositStatus = document.getElementById("depositStatus");
const withdrawAmountEl = document.getElementById("withdrawAmount");
const withdrawBtn = document.getElementById("withdrawBtn");
const withdrawStatus = document.getElementById("withdrawStatus");

// =============================================================================
// UTILITY FUNCTIONS
// =============================================================================

function formatUSDC(amount, decimals = 2) {
    const value = Number(amount) / 10 ** USDC_DECIMALS;
    return value.toLocaleString('en-US', { 
        minimumFractionDigits: decimals, 
        maximumFractionDigits: decimals 
    });
}

function parseUSDC(amount) {
    return BigInt(Math.floor(Number(amount) * 10 ** USDC_DECIMALS));
}

function shortenAddress(address) {
    return address.slice(0, 6) + "..." + address.slice(-4);
}

function showStatus(element, message, type) {
    element.textContent = message;
    element.className = `status-msg show ${type}`;
}

function hideStatus(element) {
    element.className = "status-msg";
}

// =============================================================================
// VAULT READ FUNCTIONS
// =============================================================================

async function getTotalAssets() {
    if (!vaultContract) return 0n;
    try {
        return await vaultContract.totalAssets();
    } catch (e) {
        console.error("Error getting totalAssets:", e);
        return 0n;
    }
}

async function getTotalSupply() {
    if (!vaultContract) return 0n;
    try {
        return await vaultContract.totalSupply();
    } catch (e) {
        console.error("Error getting totalSupply:", e);
        return 0n;
    }
}

async function getMaxTotalDeposits() {
    if (!vaultContract) return 0n;
    try {
        return await vaultContract.maxTotalDeposits();
    } catch (e) {
        console.error("Error getting maxTotalDeposits:", e);
        return 0n;
    }
}

async function getWalletDepositCap() {
    if (!vaultContract) return 0n;
    try {
        return await vaultContract.walletDepositCap();
    } catch (e) {
        console.error("Error getting walletDepositCap:", e);
        return 0n;
    }
}

async function getWalletDeposited(user) {
    if (!vaultContract || !user) return 0n;
    try {
        return await vaultContract.walletDeposited(user);
    } catch (e) {
        console.error("Error getting walletDeposited:", e);
        return 0n;
    }
}

// =============================================================================
// REFRESH DATA
// =============================================================================

async function refreshVaultStats() {
    try {
        const [totalAssets, totalSupply, maxDeposits, walletCap] = await Promise.all([
            getTotalAssets(),
            getTotalSupply(),
            getMaxTotalDeposits(),
            getWalletDepositCap()
        ]);

        // TVL
        tvlValueEl.textContent = `$${formatUSDC(totalAssets)}`;

        // Share Price (handle zero supply)
        if (totalSupply > 0n) {
            const sharePrice = (Number(totalAssets) / Number(totalSupply)).toFixed(4);
            sharePriceValueEl.textContent = `$${sharePrice}`;
        } else {
            sharePriceValueEl.textContent = "$1.0000";
        }

        // Global Cap
        globalCapValueEl.textContent = `$${formatUSDC(maxDeposits)}`;

        // Per Wallet Cap
        walletCapValueEl.textContent = `$${formatUSDC(walletCap)}`;

    } catch (error) {
        console.error("Error refreshing vault stats:", error);
    }
}

async function refreshUserStats() {
    if (!userAddress || !vaultContract || !usdcContract) return;

    try {
        const [usdcBalance, shares, walletCap, walletDeposited] = await Promise.all([
            usdcContract.balanceOf(userAddress),
            vaultContract.balanceOf(userAddress),
            getWalletDepositCap(),
            getWalletDeposited(userAddress)
        ]);

        // USDC Balance
        usdcBalanceEl.textContent = `$${formatUSDC(usdcBalance)}`;

        // Vault Shares
        vaultSharesEl.textContent = `${formatUSDC(shares)} pSNIPERv2`;

        // Redeemable Value
        if (shares > 0n) {
            const redeemable = await vaultContract.convertToAssets(shares);
            redeemableValueEl.textContent = `$${formatUSDC(redeemable)}`;
        } else {
            redeemableValueEl.textContent = "$0.00";
        }

        // Remaining Deposit Capacity
        const remaining = walletCap > walletDeposited ? walletCap - walletDeposited : 0n;
        remainingCapacityEl.textContent = `$${formatUSDC(remaining)}`;

    } catch (error) {
        console.error("Error refreshing user stats:", error);
    }
}

async function refreshAll() {
    await Promise.all([
        refreshVaultStats(),
        refreshUserStats()
    ]);
}

function startAutoRefresh() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(refreshAll, REFRESH_INTERVAL);
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

        // Load ABIs
        if (!abisLoaded) {
            const loaded = await loadABIs();
            if (!loaded) {
                connectBtn.textContent = "Connect Wallet";
                connectBtn.disabled = false;
                alert("Failed to load contract ABIs");
                return;
            }
            abisLoaded = true;
        }

        // Request account access
        await window.ethereum.request({ method: "eth_requestAccounts" });

        // Create provider and signer
        provider = new ethers.BrowserProvider(window.ethereum);
        signer = await provider.getSigner();
        userAddress = await signer.getAddress();

        // Instantiate contracts
        vaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);
        usdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, signer);

        // Update UI
        connectBtn.textContent = "Connected";
        connectBtn.classList.add("connected");
        walletAddressEl.textContent = shortenAddress(userAddress);
        walletAddressEl.classList.remove("hidden");

        // Enable buttons
        depositBtn.disabled = false;
        withdrawBtn.disabled = false;

        // Load data
        await refreshAll();

        // Start auto-refresh
        startAutoRefresh();

        // Listen for account changes
        window.ethereum.on("accountsChanged", handleAccountsChanged);
        window.ethereum.on("chainChanged", () => location.reload());

    } catch (error) {
        console.error("Connection error:", error);
        connectBtn.textContent = "Connect Wallet";
        connectBtn.disabled = false;
        alert("Failed to connect: " + error.message);
    }
}

function handleAccountsChanged(accounts) {
    if (accounts.length === 0) {
        location.reload();
    } else {
        userAddress = accounts[0];
        walletAddressEl.textContent = shortenAddress(userAddress);
        refreshAll();
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

        // Check allowance
        showStatus(depositStatus, "Checking allowance...", "info");
        const allowance = await usdcContract.allowance(userAddress, VAULT_ADDRESS);

        if (allowance < amount) {
            showStatus(depositStatus, "Approving USDC... Please confirm in wallet", "info");
            const approveTx = await usdcContract.approve(VAULT_ADDRESS, amount);
            showStatus(depositStatus, "Waiting for approval...", "info");
            await approveTx.wait();
        }

        // Deposit
        showStatus(depositStatus, "Depositing... Please confirm in wallet", "info");
        const depositTx = await vaultContract.deposit(amount, userAddress);
        showStatus(depositStatus, "Waiting for confirmation...", "info");
        await depositTx.wait();

        showStatus(depositStatus, `Deposited $${amountStr} USDC`, "success");
        depositAmountEl.value = "";
        await refreshAll();

    } catch (error) {
        console.error("Deposit error:", error);
        showStatus(depositStatus, "Failed: " + (error.reason || error.message), "error");
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

        showStatus(withdrawStatus, "Withdrawing... Please confirm in wallet", "info");
        const withdrawTx = await vaultContract.withdraw(amount, userAddress, userAddress);
        showStatus(withdrawStatus, "Waiting for confirmation...", "info");
        await withdrawTx.wait();

        showStatus(withdrawStatus, `Withdrew $${amountStr} USDC`, "success");
        withdrawAmountEl.value = "";
        await refreshAll();

    } catch (error) {
        console.error("Withdraw error:", error);
        showStatus(withdrawStatus, "Failed: " + (error.reason || error.message), "error");
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

// Try to auto-connect on load
(async function init() {
    // Load ABIs first for read-only data
    const loaded = await loadABIs();
    if (loaded) {
        abisLoaded = true;
        
        // Create read-only provider for vault stats even without wallet
        if (typeof window.ethereum !== "undefined") {
            try {
                provider = new ethers.BrowserProvider(window.ethereum);
                vaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, provider);
                usdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, provider);
                
                // Load vault stats
                await refreshVaultStats();
                
                // Check if already connected
                const accounts = await window.ethereum.request({ method: "eth_accounts" });
                if (accounts.length > 0) {
                    connectWallet();
                }
            } catch (e) {
                console.log("Auto-init failed:", e);
            }
        }
    }
})();

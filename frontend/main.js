/**
 * PredictFi Sniper Vault V2 - Frontend
 * Professional DeFi interface for the pSNIPER vault
 */

// =============================================================================
// CONFIGURATION
// =============================================================================

const VAULT_ADDRESS = "0x26BCAe8DEA9A2b04a522cab2679CF9708d3F84E3";
const USDC_ADDRESS = "0x743dBb99B51A542aA7b6E859713b4b615445C019";
const USDC_DECIMALS = 6;
const REFRESH_INTERVAL = 30000;
const BASE_SEPOLIA_RPC = "https://sepolia.base.org";

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
const sharePriceValueEl = document.getElementById("sharePriceValue");
const positionValueEl = document.getElementById("positionValue");
const sharesBalanceEl = document.getElementById("sharesBalance");
const tvlValueEl = document.getElementById("tvlValue");
const statsSharePriceEl = document.getElementById("statsSharePrice");
const depositAmountEl = document.getElementById("depositAmount");
const depositBtn = document.getElementById("depositBtn");
const depositStatus = document.getElementById("depositStatus");
const withdrawAmountEl = document.getElementById("withdrawAmount");
const withdrawBtn = document.getElementById("withdrawBtn");
const withdrawStatus = document.getElementById("withdrawStatus");
const disclaimerModal = document.getElementById("disclaimerModal");
const understandCheck = document.getElementById("understandCheck");
const dontShowCheck = document.getElementById("dontShowCheck");
const acceptBtn = document.getElementById("acceptBtn");

// =============================================================================
// DISCLAIMER MODAL
// =============================================================================

function initDisclaimer() {
    const dismissed = localStorage.getItem("predictfi_disclaimer_dismissed");
    if (dismissed === "true") {
        disclaimerModal.classList.add("hidden");
    }
    
    understandCheck.addEventListener("change", updateAcceptBtn);
    
    acceptBtn.addEventListener("click", () => {
        if (dontShowCheck.checked) {
            localStorage.setItem("predictfi_disclaimer_dismissed", "true");
        }
        disclaimerModal.classList.add("hidden");
    });
}

function updateAcceptBtn() {
    acceptBtn.disabled = !understandCheck.checked;
}

// =============================================================================
// TAB NAVIGATION
// =============================================================================

function initTabs() {
    const tabs = document.querySelectorAll(".nav-tab");
    tabs.forEach(tab => {
        tab.addEventListener("click", () => {
            const tabName = tab.dataset.tab;
            
            tabs.forEach(t => t.classList.remove("active"));
            tab.classList.add("active");
            
            document.querySelectorAll(".tab-content").forEach(content => {
                content.classList.remove("active");
            });
            
            document.getElementById(`${tabName}Tab`).classList.add("active");
        });
    });
}

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

// =============================================================================
// REFRESH DATA
// =============================================================================

async function refreshVaultStats() {
    try {
        const [totalAssets, totalSupply] = await Promise.all([
            getTotalAssets(),
            getTotalSupply()
        ]);

        // Share Price
        let sharePrice = 1.0;
        if (totalSupply > 0n) {
            sharePrice = Number(totalAssets) / Number(totalSupply);
        }
        
        const sharePriceStr = `$${sharePrice.toFixed(4)}`;
        sharePriceValueEl.textContent = sharePriceStr;
        statsSharePriceEl.textContent = `$${sharePrice.toFixed(2)}`;

        // TVL = share price * total supply (in USDC terms = totalAssets)
        const tvl = Number(totalAssets) / 10 ** USDC_DECIMALS;
        tvlValueEl.textContent = `$${tvl.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

    } catch (error) {
        console.error("Error refreshing vault stats:", error);
    }
}

async function refreshUserStats() {
    if (!userAddress || !vaultContract) return;

    try {
        const shares = await vaultContract.balanceOf(userAddress);
        
        // Shares balance
        const sharesNum = Number(shares) / 10 ** USDC_DECIMALS;
        sharesBalanceEl.textContent = sharesNum.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

        // Position value in USDC
        if (shares > 0n) {
            const redeemable = await vaultContract.convertToAssets(shares);
            positionValueEl.textContent = `$${formatUSDC(redeemable)}`;
        } else {
            positionValueEl.textContent = "$0.00";
        }

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

        await window.ethereum.request({ method: "eth_requestAccounts" });

        provider = new ethers.BrowserProvider(window.ethereum);
        signer = await provider.getSigner();
        userAddress = await signer.getAddress();

        vaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);
        usdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, signer);

        connectBtn.textContent = "Connected";
        connectBtn.classList.add("connected");
        walletAddressEl.textContent = shortenAddress(userAddress);
        walletAddressEl.style.display = "block";

        depositBtn.disabled = false;
        withdrawBtn.disabled = false;

        await refreshAll();
        startAutoRefresh();

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
        showStatus(depositStatus, "Enter a valid amount", "error");
        return;
    }

    const amount = parseUSDC(amountStr);

    try {
        depositBtn.disabled = true;
        hideStatus(depositStatus);

        showStatus(depositStatus, "Checking allowance...", "info");
        const allowance = await usdcContract.allowance(userAddress, VAULT_ADDRESS);

        if (allowance < amount) {
            showStatus(depositStatus, "Approving USDC...", "info");
            const approveTx = await usdcContract.approve(VAULT_ADDRESS, amount);
            showStatus(depositStatus, "Waiting for approval...", "info");
            await approveTx.wait();
        }

        showStatus(depositStatus, "Depositing...", "info");
        const depositTx = await vaultContract.deposit(amount, userAddress);
        showStatus(depositStatus, "Confirming...", "info");
        await depositTx.wait();

        showStatus(depositStatus, `Deposited $${amountStr}`, "success");
        depositAmountEl.value = "";
        await refreshAll();

    } catch (error) {
        console.error("Deposit error:", error);
        showStatus(depositStatus, error.reason || error.message, "error");
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
        showStatus(withdrawStatus, "Enter a valid amount", "error");
        return;
    }

    const amount = parseUSDC(amountStr);

    try {
        withdrawBtn.disabled = true;
        hideStatus(withdrawStatus);

        showStatus(withdrawStatus, "Withdrawing...", "info");
        const withdrawTx = await vaultContract.withdraw(amount, userAddress, userAddress);
        showStatus(withdrawStatus, "Confirming...", "info");
        await withdrawTx.wait();

        showStatus(withdrawStatus, `Withdrew $${amountStr}`, "success");
        withdrawAmountEl.value = "";
        await refreshAll();

    } catch (error) {
        console.error("Withdraw error:", error);
        showStatus(withdrawStatus, error.reason || error.message, "error");
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

// =============================================================================
// INITIALIZATION
// =============================================================================

(async function init() {
    initDisclaimer();
    initTabs();
    
    const loaded = await loadABIs();
    if (loaded) {
        abisLoaded = true;
        
        // Always create a read-only provider for vault stats (works without MetaMask)
        try {
            const readOnlyProvider = new ethers.JsonRpcProvider(BASE_SEPOLIA_RPC);
            vaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, readOnlyProvider);
            usdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, readOnlyProvider);
            
            await refreshVaultStats();
            startAutoRefresh();
        } catch (e) {
            console.log("Read-only provider init failed:", e);
        }
        
        // If MetaMask is available, check for existing connection
        if (typeof window.ethereum !== "undefined") {
            try {
                const accounts = await window.ethereum.request({ method: "eth_accounts" });
                if (accounts.length > 0) {
                    connectWallet();
                }
            } catch (e) {
                console.log("Auto-connect check failed:", e);
            }
        }
    }
})();

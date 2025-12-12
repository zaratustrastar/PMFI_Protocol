/**
 * PredictFi pSNIPER V4 - Frontend
 * 
 * Key difference from V3:
 * - Deposit/Withdraw require signed NavData from the oracle
 * - No on-chain NAV updates - users pay gas, oracle pays nothing
 */

// =============================================================================
// CONFIGURATION
// =============================================================================

const VAULT_ADDRESS = ""; // TODO: Set after V4 deployment
const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const USDC_DECIMALS = 6;
const REFRESH_INTERVAL = 30000;
const BASE_MAINNET_RPC = "https://mainnet.base.org";
const BASE_MAINNET_CHAIN_ID = 8453;

// VPS Price API Configuration
// This is REQUIRED for V4 - need to get signed NAV data
const PRICE_API_URL = localStorage.getItem("predictfi_price_api_url") || "";
const PRICE_REFRESH_INTERVAL = 10000;

// =============================================================================
// ABIs
// =============================================================================

let VAULT_ABI = null;
let USDC_ABI = null;

async function loadABIs() {
    try {
        const [vaultResponse, usdcResponse] = await Promise.all([
            fetch("abis/vault-v4.json"),
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
let priceRefreshTimer = null;
let isConnected = false;
let lastPriceData = null;

// =============================================================================
// DOM ELEMENTS
// =============================================================================

const connectBtn = document.getElementById("connectBtn");
const vaultTvlEl = document.getElementById("vaultTvl");
const tvlValueEl = document.getElementById("tvlValue");
const statsSharePriceEl = document.getElementById("statsSharePrice");
const userStatsEl = document.getElementById("userStats");
const positionValueEl = document.getElementById("positionValue");
const sharesBalanceEl = document.getElementById("sharesBalance");
const openDepositBtn = document.getElementById("openDepositBtn");
const depositModal = document.getElementById("depositModal");
const closeDepositModal = document.getElementById("closeDepositModal");
const depositAmountEl = document.getElementById("depositAmount");
const withdrawAmountEl = document.getElementById("withdrawAmount");
const depositBtn = document.getElementById("depositBtn");
const withdrawBtn = document.getElementById("withdrawBtn");
const txStatus = document.getElementById("txStatus");
const disclaimerModal = document.getElementById("disclaimerModal");
const understandCheck = document.getElementById("understandCheck");
const dontShowCheck = document.getElementById("dontShowCheck");
const acceptBtn = document.getElementById("acceptBtn");
const networkWarning = document.getElementById("networkWarning");
const switchNetworkBtn = document.getElementById("switchNetworkBtn");

// =============================================================================
// UTILITY FUNCTIONS
// =============================================================================

function formatUSDC(value, decimals = 2) {
    if (value === null || value === undefined) return "$0.00";
    const num = Number(value) / Math.pow(10, USDC_DECIMALS);
    return "$" + num.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function formatShares(value, decimals = 4) {
    if (value === null || value === undefined) return "0";
    const num = Number(ethers.formatEther(value));
    return num.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function parseUSDC(value) {
    return ethers.parseUnits(value.toString(), USDC_DECIMALS);
}

function showStatus(el, message, type = "info") {
    if (!el) return;
    el.textContent = message;
    el.className = `tx-status ${type}`;
    el.classList.remove("hidden");
}

function hideStatus(el) {
    if (el) el.classList.add("hidden");
}

// =============================================================================
// SIGNED NAV DATA (V4 SPECIFIC)
// =============================================================================

async function getSignedNavData() {
    if (!PRICE_API_URL) {
        throw new Error("Price API URL not configured. Please set it in settings.");
    }
    
    try {
        const response = await fetch(`${PRICE_API_URL}/sign-nav`, {
            method: "GET",
            headers: { "Content-Type": "application/json" },
        });
        
        if (!response.ok) {
            throw new Error(`Failed to get signed NAV: ${response.status}`);
        }
        
        const data = await response.json();
        console.log("Signed NAV data:", data);
        
        return {
            navData: {
                nav: BigInt(data.navData.nav),
                timestamp: BigInt(data.navData.timestamp),
                deadline: BigInt(data.navData.deadline),
                roundId: BigInt(data.navData.roundId),
            },
            signature: data.signature,
            metadata: data.metadata,
        };
    } catch (error) {
        console.error("Error getting signed NAV:", error);
        throw error;
    }
}

// =============================================================================
// PRICE DISPLAY
// =============================================================================

async function fetchPriceFromAPI() {
    if (!PRICE_API_URL) return null;
    
    try {
        const response = await fetch(`${PRICE_API_URL}/price`);
        if (!response.ok) return null;
        return await response.json();
    } catch (error) {
        console.error("Error fetching price:", error);
        return null;
    }
}

function updatePriceDisplay(priceData) {
    if (!priceData) return;
    
    lastPriceData = priceData;
    
    if (statsSharePriceEl) {
        const price = priceData.price_per_share || 1;
        statsSharePriceEl.textContent = "$" + price.toFixed(4);
    }
    
    if (tvlValueEl && priceData.total_assets_usdc !== undefined) {
        tvlValueEl.textContent = "$" + priceData.total_assets_usdc.toFixed(2);
    }
}

async function refreshPriceLoop() {
    const priceData = await fetchPriceFromAPI();
    updatePriceDisplay(priceData);
}

function startPriceRefresh() {
    refreshPriceLoop();
    if (priceRefreshTimer) clearInterval(priceRefreshTimer);
    priceRefreshTimer = setInterval(refreshPriceLoop, PRICE_REFRESH_INTERVAL);
}

// =============================================================================
// NETWORK
// =============================================================================

async function checkNetwork() {
    if (typeof window.ethereum === "undefined") return true;
    
    try {
        const chainId = await window.ethereum.request({ method: "eth_chainId" });
        const currentChainId = parseInt(chainId, 16);
        
        if (currentChainId !== BASE_MAINNET_CHAIN_ID) {
            networkWarning?.classList.remove("hidden");
            return false;
        } else {
            networkWarning?.classList.add("hidden");
            return true;
        }
    } catch (error) {
        console.error("Error checking network:", error);
        return true;
    }
}

async function switchToBase() {
    try {
        await window.ethereum.request({
            method: "wallet_switchEthereumChain",
            params: [{ chainId: "0x2105" }]
        });
        await checkNetwork();
    } catch (switchError) {
        if (switchError.code === 4902) {
            try {
                await window.ethereum.request({
                    method: "wallet_addEthereumChain",
                    params: [{
                        chainId: "0x2105",
                        chainName: "Base",
                        nativeCurrency: { name: "ETH", symbol: "ETH", decimals: 18 },
                        rpcUrls: ["https://mainnet.base.org"],
                        blockExplorerUrls: ["https://basescan.org"]
                    }]
                });
            } catch (addError) {
                console.error("Error adding Base network:", addError);
            }
        }
    }
}

// =============================================================================
// WALLET CONNECTION
// =============================================================================

async function connectWallet() {
    if (typeof window.ethereum === "undefined") {
        alert("Please install a wallet like MetaMask");
        return;
    }
    
    const isCorrectNetwork = await checkNetwork();
    if (!isCorrectNetwork) {
        await switchToBase();
        return;
    }
    
    try {
        provider = new ethers.BrowserProvider(window.ethereum);
        signer = await provider.getSigner();
        userAddress = await signer.getAddress();
        
        vaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);
        usdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, signer);
        
        isConnected = true;
        connectBtn.textContent = userAddress.slice(0, 6) + "..." + userAddress.slice(-4);
        
        await refreshAll();
        startAutoRefresh();
        
        window.ethereum.on("accountsChanged", handleAccountsChanged);
        window.ethereum.on("chainChanged", handleChainChanged);
        
    } catch (error) {
        console.error("Connection error:", error);
        alert("Failed to connect wallet");
    }
}

function disconnectWallet() {
    isConnected = false;
    userAddress = null;
    signer = null;
    connectBtn.textContent = "Connect";
    if (userStatsEl) userStatsEl.classList.add("hidden");
    initReadOnlyProvider();
}

function handleAccountsChanged(accounts) {
    if (accounts.length === 0) {
        disconnectWallet();
    } else {
        userAddress = accounts[0];
        refreshAll();
    }
}

async function handleChainChanged() {
    const isCorrectNetwork = await checkNetwork();
    if (!isCorrectNetwork) {
        disconnectWallet();
    } else {
        location.reload();
    }
}

// =============================================================================
// VAULT STATS
// =============================================================================

async function refreshVaultStats() {
    try {
        if (!vaultContract) return;
        
        const [totalSupply, vaultState] = await Promise.all([
            vaultContract.totalSupply(),
            vaultContract.getVaultState(),
        ]);
        
        const lastNav = vaultState[0];
        const vaultBalance = vaultState[4];
        
        if (tvlValueEl) {
            tvlValueEl.textContent = formatUSDC(vaultBalance);
        }
        
        if (statsSharePriceEl && lastNav) {
            const price = Number(lastNav) / 1e18;
            statsSharePriceEl.textContent = "$" + price.toFixed(4);
        }
        
    } catch (error) {
        console.error("Error refreshing vault stats:", error);
    }
}

async function refreshUserStats() {
    if (!userAddress || !vaultContract || !usdcContract) return;
    
    try {
        const [shares, position, usdcBalance] = await Promise.all([
            vaultContract.balanceOf(userAddress),
            vaultContract.getUserPosition(userAddress),
            usdcContract.balanceOf(userAddress),
        ]);
        
        if (sharesBalanceEl) {
            sharesBalanceEl.textContent = formatShares(shares);
        }
        
        const lastNav = await vaultContract.lastNav();
        const price = Number(lastNav) / 1e18;
        const positionValue = Number(ethers.formatEther(shares)) * price;
        
        if (positionValueEl) {
            positionValueEl.textContent = "$" + positionValue.toFixed(2);
        }
        
        if (userStatsEl) {
            userStatsEl.classList.remove("hidden");
        }
        
    } catch (error) {
        console.error("Error refreshing user stats:", error);
    }
}

async function refreshAll() {
    await refreshVaultStats();
    if (userAddress) {
        await refreshUserStats();
    }
}

function startAutoRefresh() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(refreshAll, REFRESH_INTERVAL);
}

// =============================================================================
// DEPOSIT (V4 - with signed NAV)
// =============================================================================

async function handleDeposit() {
    const amountStr = depositAmountEl.value;
    if (!amountStr || Number(amountStr) <= 0) {
        showStatus(txStatus, "Enter a valid amount", "error");
        return;
    }

    if (!signer || !userAddress) {
        showStatus(txStatus, "Please connect your wallet first", "error");
        return;
    }

    if (!PRICE_API_URL) {
        showStatus(txStatus, "Price API not configured. Set URL in settings.", "error");
        return;
    }

    const amount = parseUSDC(amountStr);

    try {
        depositBtn.disabled = true;
        withdrawBtn.disabled = true;
        hideStatus(txStatus);

        // Get signed NAV data from oracle
        showStatus(txStatus, "Getting signed price...", "info");
        const signedData = await getSignedNavData();
        console.log("Got signed NAV:", signedData);
        
        updatePriceDisplay(signedData.metadata);

        // Check allowance and approve if needed
        const signerUsdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, signer);
        const signerVaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);

        showStatus(txStatus, "Checking allowance...", "info");
        const allowance = await signerUsdcContract.allowance(userAddress, VAULT_ADDRESS);

        if (allowance < amount) {
            showStatus(txStatus, "Approving USDC...", "info");
            const approveTx = await signerUsdcContract.approve(VAULT_ADDRESS, amount);
            showStatus(txStatus, "Waiting for approval...", "info");
            await approveTx.wait();
        }

        // Deposit with signed NAV data
        showStatus(txStatus, "Depositing...", "info");
        const depositTx = await signerVaultContract.deposit(
            amount,
            [
                signedData.navData.nav,
                signedData.navData.timestamp,
                signedData.navData.deadline,
                signedData.navData.roundId,
            ],
            signedData.signature
        );
        showStatus(txStatus, "Confirming...", "info");
        await depositTx.wait();

        showStatus(txStatus, `Deposited $${amountStr} USDC`, "success");
        depositAmountEl.value = "";
        await refreshAll();

    } catch (error) {
        console.error("Deposit error:", error);
        let errorMsg = error.reason || error.message;
        if (errorMsg.includes("NAV expired")) {
            errorMsg = "Price expired. Please try again.";
        } else if (errorMsg.includes("Invalid NAV signer")) {
            errorMsg = "Invalid price signature. Oracle may be misconfigured.";
        } else if (errorMsg.includes("exceeds wallet cap")) {
            errorMsg = "Deposit exceeds your wallet cap.";
        } else if (errorMsg.includes("Exceeds total cap")) {
            errorMsg = "Vault is at capacity.";
        }
        showStatus(txStatus, errorMsg, "error");
    } finally {
        depositBtn.disabled = false;
        withdrawBtn.disabled = false;
    }
}

// =============================================================================
// WITHDRAW (V4 - with signed NAV)
// =============================================================================

async function handleWithdraw() {
    const amountStr = withdrawAmountEl.value;
    if (!amountStr || Number(amountStr) <= 0) {
        showStatus(txStatus, "Enter a valid amount", "error");
        return;
    }

    if (!signer || !userAddress) {
        showStatus(txStatus, "Please connect your wallet first", "error");
        return;
    }

    if (!PRICE_API_URL) {
        showStatus(txStatus, "Price API not configured. Set URL in settings.", "error");
        return;
    }

    // For V4, withdrawAmountEl is shares to redeem (not USDC)
    const shares = ethers.parseEther(amountStr);

    try {
        depositBtn.disabled = true;
        withdrawBtn.disabled = true;
        hideStatus(txStatus);

        // Get signed NAV data from oracle
        showStatus(txStatus, "Getting signed price...", "info");
        const signedData = await getSignedNavData();
        console.log("Got signed NAV:", signedData);

        const signerVaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);

        // Redeem shares with signed NAV data
        showStatus(txStatus, "Withdrawing (1% exit fee applies)...", "info");
        const redeemTx = await signerVaultContract.redeem(
            shares,
            [
                signedData.navData.nav,
                signedData.navData.timestamp,
                signedData.navData.deadline,
                signedData.navData.roundId,
            ],
            signedData.signature
        );
        showStatus(txStatus, "Confirming...", "info");
        await redeemTx.wait();

        showStatus(txStatus, `Redeemed ${amountStr} pSNIPER shares`, "success");
        withdrawAmountEl.value = "";
        await refreshAll();

    } catch (error) {
        console.error("Withdraw error:", error);
        let errorMsg = error.reason || error.message;
        if (errorMsg.includes("NAV expired")) {
            errorMsg = "Price expired. Please try again.";
        } else if (errorMsg.includes("Invalid NAV signer")) {
            errorMsg = "Invalid price signature. Oracle may be misconfigured.";
        } else if (errorMsg.includes("Insufficient liquidity")) {
            errorMsg = "Insufficient liquidity in vault.";
        } else if (errorMsg.includes("Insufficient shares")) {
            errorMsg = "You don't have that many shares.";
        }
        showStatus(txStatus, errorMsg, "error");
    } finally {
        depositBtn.disabled = false;
        withdrawBtn.disabled = false;
    }
}

// =============================================================================
// MODALS
// =============================================================================

function initDisclaimer() {
    const skipDisclaimer = localStorage.getItem("predictfi_skip_disclaimer");
    if (skipDisclaimer === "true") {
        disclaimerModal?.classList.add("hidden");
        return;
    }
    
    disclaimerModal?.classList.remove("hidden");
    
    understandCheck?.addEventListener("change", () => {
        if (acceptBtn) acceptBtn.disabled = !understandCheck.checked;
    });
    
    acceptBtn?.addEventListener("click", () => {
        if (dontShowCheck?.checked) {
            localStorage.setItem("predictfi_skip_disclaimer", "true");
        }
        disclaimerModal?.classList.add("hidden");
    });
}

function initDepositModal() {
    openDepositBtn?.addEventListener("click", () => {
        depositModal?.classList.remove("hidden");
    });
    
    closeDepositModal?.addEventListener("click", () => {
        depositModal?.classList.add("hidden");
    });
    
    depositModal?.addEventListener("click", (e) => {
        if (e.target === depositModal) {
            depositModal.classList.add("hidden");
        }
    });
}

// =============================================================================
// READ-ONLY PROVIDER
// =============================================================================

let readOnlyVaultContract = null;

async function initReadOnlyProvider() {
    if (isConnected && signer) return;
    
    try {
        const readOnlyProvider = new ethers.JsonRpcProvider(BASE_MAINNET_RPC);
        readOnlyVaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, readOnlyProvider);
        
        if (!isConnected) {
            vaultContract = readOnlyVaultContract;
            usdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, readOnlyProvider);
        }
        
        await refreshVaultStats();
        startAutoRefresh();
    } catch (e) {
        console.log("Read-only provider init failed:", e);
    }
}

// =============================================================================
// EVENT LISTENERS
// =============================================================================

connectBtn?.addEventListener("click", connectWallet);
depositBtn?.addEventListener("click", handleDeposit);
withdrawBtn?.addEventListener("click", handleWithdraw);
switchNetworkBtn?.addEventListener("click", switchToBase);

// =============================================================================
// INITIALIZATION
// =============================================================================

(async function init() {
    initDisclaimer();
    initDepositModal();
    
    abisLoaded = await loadABIs();
    if (!abisLoaded) {
        console.error("Failed to load ABIs");
        return;
    }
    
    await checkNetwork();
    await initReadOnlyProvider();
    
    if (PRICE_API_URL) {
        startPriceRefresh();
    }
    
    // Check if already connected
    if (typeof window.ethereum !== "undefined") {
        try {
            const accounts = await window.ethereum.request({ method: "eth_accounts" });
            if (accounts.length > 0) {
                await connectWallet();
            }
        } catch (error) {
            console.log("Auto-connect check failed:", error);
        }
    }
})();

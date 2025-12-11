/**
 * PredictFi pSNIPER - Frontend
 */

// =============================================================================
// CONFIGURATION
// =============================================================================

const VAULT_ADDRESS = "0x5E9Cfd99Cb55cB7981E8C7E8ceC73dC68e60e6C9";
const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const USDC_DECIMALS = 6;
const REFRESH_INTERVAL = 30000;
const BASE_MAINNET_RPC = "https://mainnet.base.org";
const BASE_MAINNET_CHAIN_ID = 8453;

// VPS Price API Configuration
// Set this to your VPS URL where bot.py is running (e.g., "http://your-vps-ip:8080")
// Leave empty to use on-chain data directly
const PRICE_API_URL = localStorage.getItem("predictfi_price_api_url") || "";
const PRICE_REFRESH_INTERVAL = 10000; // 10 seconds for live price updates

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
// NETWORK DETECTION
// =============================================================================

async function checkNetwork() {
    if (typeof window.ethereum === "undefined") return true;
    
    try {
        const chainId = await window.ethereum.request({ method: "eth_chainId" });
        const currentChainId = parseInt(chainId, 16);
        
        if (currentChainId !== BASE_MAINNET_CHAIN_ID) {
            networkWarning.classList.remove("hidden");
            return false;
        } else {
            networkWarning.classList.add("hidden");
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
                await checkNetwork();
            } catch (addError) {
                console.error("Failed to add Base network:", addError);
            }
        }
    }
}

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
// DEPOSIT MODAL
// =============================================================================

function initDepositModal() {
    openDepositBtn.addEventListener("click", () => {
        depositModal.classList.remove("hidden");
        refreshUserStats();
    });
    
    closeDepositModal.addEventListener("click", () => {
        depositModal.classList.add("hidden");
        hideStatus(txStatus);
    });
    
    depositModal.addEventListener("click", (e) => {
        if (e.target === depositModal) {
            depositModal.classList.add("hidden");
            hideStatus(txStatus);
        }
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

function showStatus(element, message, type) {
    element.textContent = message;
    element.className = `status-msg show ${type}`;
}

function hideStatus(element) {
    element.className = "status-msg";
}

// =============================================================================
// PRICE API FUNCTIONS (VPS)
// =============================================================================

async function fetchPriceFromAPI() {
    if (!PRICE_API_URL) return null;
    
    try {
        const response = await fetch(`${PRICE_API_URL}/price`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' },
            mode: 'cors'
        });
        
        if (!response.ok) {
            console.warn("Price API error:", response.status);
            return null;
        }
        
        const data = await response.json();
        lastPriceData = data;
        return data;
    } catch (error) {
        console.warn("Failed to fetch price from API:", error.message);
        return null;
    }
}

async function requestFreshPrice() {
    if (!PRICE_API_URL) return null;
    
    try {
        const response = await fetch(`${PRICE_API_URL}/price/refresh`, {
            method: 'POST',
            headers: { 'Accept': 'application/json' },
            mode: 'cors'
        });
        
        if (!response.ok) {
            console.warn("Price refresh API error:", response.status);
            return null;
        }
        
        const data = await response.json();
        lastPriceData = data;
        return data;
    } catch (error) {
        console.warn("Failed to refresh price from API:", error.message);
        return null;
    }
}


function updatePriceDisplay(priceData) {
    if (!priceData) return;
    
    const sharePrice = priceData.price_per_share || 1.0;
    statsSharePriceEl.textContent = `$${sharePrice.toFixed(4)}`;
    
    const tvl = priceData.total_assets || 0;
    const tvlStr = `$${tvl.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    vaultTvlEl.textContent = tvlStr;
    tvlValueEl.textContent = tvlStr;
    
    // Update last updated timestamp if available
    if (priceData.last_updated) {
        const lastUpdated = new Date(priceData.last_updated * 1000);
        console.log("Price last updated:", lastUpdated.toLocaleTimeString());
    }
}

async function refreshPriceFromAPI() {
    const priceData = await fetchPriceFromAPI();
    if (priceData) {
        updatePriceDisplay(priceData);
        return true;
    }
    return false;
}

function startPriceAutoRefresh() {
    if (priceRefreshTimer) clearInterval(priceRefreshTimer);
    
    if (PRICE_API_URL) {
        console.log("Starting price auto-refresh from VPS every 10 seconds");
        priceRefreshTimer = setInterval(refreshPriceFromAPI, PRICE_REFRESH_INTERVAL);
        // Initial fetch
        refreshPriceFromAPI();
    }
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

        let sharePrice = 1.0;
        if (totalSupply > 0n) {
            sharePrice = Number(totalAssets) / Number(totalSupply);
        }
        
        statsSharePriceEl.textContent = `$${sharePrice.toFixed(2)}`;

        const tvl = Number(totalAssets) / 10 ** USDC_DECIMALS;
        const tvlStr = `$${tvl.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
        vaultTvlEl.textContent = tvlStr;
        tvlValueEl.textContent = tvlStr;

    } catch (error) {
        console.error("Error refreshing vault stats:", error);
    }
}

async function refreshUserStats() {
    if (!userAddress || !vaultContract || !isConnected) {
        userStatsEl.classList.add("hidden");
        return;
    }

    try {
        const shares = await vaultContract.balanceOf(userAddress);
        const sharesNum = Number(shares) / 10 ** USDC_DECIMALS;
        
        sharesBalanceEl.textContent = sharesNum.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        
        if (shares > 0n) {
            const redeemable = await vaultContract.convertToAssets(shares);
            positionValueEl.textContent = `$${formatUSDC(redeemable)}`;
            userStatsEl.classList.remove("hidden");
        } else {
            positionValueEl.textContent = "$0.00";
            userStatsEl.classList.add("hidden");
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

    // If already connected, disconnect
    if (isConnected) {
        disconnectWallet();
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

        const isCorrectNetwork = await checkNetwork();
        if (!isCorrectNetwork) {
            connectBtn.textContent = "Connect Wallet";
            connectBtn.disabled = false;
            return;
        }

        provider = new ethers.BrowserProvider(window.ethereum);
        signer = await provider.getSigner();
        userAddress = await signer.getAddress();

        vaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);
        usdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, signer);

        isConnected = true;
        connectBtn.textContent = "Disconnect";
        connectBtn.classList.add("connected");
        connectBtn.disabled = false;

        openDepositBtn.disabled = false;

        await refreshAll();
        startAutoRefresh();

        window.ethereum.on("accountsChanged", handleAccountsChanged);
        window.ethereum.on("chainChanged", handleChainChanged);

    } catch (error) {
        console.error("Connection error:", error);
        connectBtn.textContent = "Connect Wallet";
        connectBtn.disabled = false;
        alert("Failed to connect: " + error.message);
    }
}

function disconnectWallet() {
    isConnected = false;
    userAddress = null;
    signer = null;
    
    connectBtn.textContent = "Connect Wallet";
    connectBtn.classList.remove("connected");
    
    openDepositBtn.disabled = true;
    userStatsEl.classList.add("hidden");
    
    // Reinitialize with read-only provider
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
// DEPOSIT
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

    const amount = parseUSDC(amountStr);

    try {
        depositBtn.disabled = true;
        withdrawBtn.disabled = true;
        hideStatus(txStatus);

        // Request fresh price from VPS before deposit for accurate share pricing
        if (PRICE_API_URL) {
            showStatus(txStatus, "Fetching latest price...", "info");
            const priceData = await requestFreshPrice();
            if (priceData) {
                console.log("Price refreshed before deposit:", priceData);
                updatePriceDisplay(priceData);
            }
        }

        // Ensure we're using signer-connected contracts
        const signerUsdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, signer);
        const signerVaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);

        showStatus(txStatus, "Checking allowance...", "info");
        console.log("Checking allowance for", userAddress, "to", VAULT_ADDRESS);
        const allowance = await signerUsdcContract.allowance(userAddress, VAULT_ADDRESS);
        console.log("Allowance:", allowance.toString());

        if (allowance < amount) {
            showStatus(txStatus, "Approving USDC...", "info");
            const approveTx = await signerUsdcContract.approve(VAULT_ADDRESS, amount);
            showStatus(txStatus, "Waiting for approval...", "info");
            await approveTx.wait();
        }

        showStatus(txStatus, "Depositing...", "info");
        const depositTx = await signerVaultContract.deposit(amount, userAddress);
        showStatus(txStatus, "Confirming...", "info");
        await depositTx.wait();

        showStatus(txStatus, `Deposited $${amountStr} USDC`, "success");
        depositAmountEl.value = "";
        await refreshAll();

    } catch (error) {
        console.error("Deposit error:", error);
        let errorMsg = error.reason || error.message;
        if (error.code === "CALL_EXCEPTION" || errorMsg.includes("could not decode")) {
            errorMsg = "Contract call failed. Please ensure you're on Base Mainnet.";
        } else if (error.code === "ACTION_REJECTED") {
            errorMsg = "Transaction rejected by user.";
        } else if (errorMsg.includes("exceeds wallet cap")) {
            errorMsg = "Deposit exceeds your 100 USDC wallet cap.";
        } else if (errorMsg.includes("insufficient")) {
            errorMsg = "Insufficient USDC balance.";
        }
        showStatus(txStatus, errorMsg, "error");
    } finally {
        depositBtn.disabled = false;
        withdrawBtn.disabled = false;
    }
}

// =============================================================================
// WITHDRAW
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

    const amount = parseUSDC(amountStr);

    try {
        depositBtn.disabled = true;
        withdrawBtn.disabled = true;
        hideStatus(txStatus);

        // Request fresh price from VPS before withdraw for accurate share pricing
        if (PRICE_API_URL) {
            showStatus(txStatus, "Fetching latest price...", "info");
            const priceData = await requestFreshPrice();
            if (priceData) {
                console.log("Price refreshed before withdraw:", priceData);
                updatePriceDisplay(priceData);
            }
        }

        // Ensure we're using signer-connected contract
        const signerVaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);

        showStatus(txStatus, "Withdrawing...", "info");
        const withdrawTx = await signerVaultContract.withdraw(amount, userAddress, userAddress);
        showStatus(txStatus, "Confirming...", "info");
        await withdrawTx.wait();

        showStatus(txStatus, `Withdrew $${amountStr} USDC`, "success");
        withdrawAmountEl.value = "";
        await refreshAll();

    } catch (error) {
        console.error("Withdraw error:", error);
        let errorMsg = error.reason || error.message;
        if (errorMsg.includes("Insufficient liquidity")) {
            errorMsg = "Insufficient liquidity. Please try a smaller amount or wait for funds to be freed.";
        }
        showStatus(txStatus, errorMsg, "error");
    } finally {
        depositBtn.disabled = false;
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

// Separate read-only contracts for stats display
let readOnlyVaultContract = null;

async function initReadOnlyProvider() {
    // Don't overwrite signer-connected contracts if already connected
    if (isConnected && signer) {
        console.log("Skipping read-only init - already connected with signer");
        return;
    }
    
    try {
        const readOnlyProvider = new ethers.JsonRpcProvider(BASE_MAINNET_RPC);
        readOnlyVaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, readOnlyProvider);
        
        // Only set main contracts if not connected
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

(async function init() {
    initDisclaimer();
    initDepositModal();
    
    if (switchNetworkBtn) {
        switchNetworkBtn.addEventListener("click", switchToBase);
    }
    
    const loaded = await loadABIs();
    if (loaded) {
        abisLoaded = true;
        
        await initReadOnlyProvider();
        
        // Start price auto-refresh from VPS (if configured)
        startPriceAutoRefresh();
        
        if (typeof window.ethereum !== "undefined") {
            checkNetwork();
            
            window.ethereum.on("chainChanged", checkNetwork);
            
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
    
    // Log price API configuration
    if (PRICE_API_URL) {
        console.log("Price API URL configured:", PRICE_API_URL);
    } else {
        console.log("No Price API URL configured. Set via localStorage: localStorage.setItem('predictfi_price_api_url', 'http://your-vps:8080')");
    }
})();

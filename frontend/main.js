/**
 * PredictFi pSNIPER - Frontend
 */

// =============================================================================
// CONFIGURATION
// =============================================================================

// Read from global config (set by HTML template) or use default
const VAULT_ADDRESS = window.PSNIPER_CONFIG?.VAULT_ADDRESS || "0x17C27001929E75D1eBd5FdeE6E986EA5a91de0D1";
const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const USDC_DECIMALS = 6;
const REFRESH_INTERVAL = 30000;
const BASE_MAINNET_RPC = "https://base-mainnet.g.alchemy.com/v2/0tHcICpxKdnmScqh22kL5";
const BASE_MAINNET_CHAIN_ID = 8453;

// VPS Price API Configuration
// Auto-detect from current domain - frontend and API are served from same origin
const PRICE_API_URL = localStorage.getItem("predictfi_price_api_url") || window.location.origin;
const PRICE_REFRESH_INTERVAL = 10000; // 10 seconds for live price updates

// =============================================================================
// ABIs
// =============================================================================

let VAULT_ABI = null;
let USDC_ABI = null;

async function loadABIs() {
    try {
        const cacheBuster = Date.now();
        const [vaultResponse, usdcResponse] = await Promise.all([
            fetch(`abis/vault.json?v=${cacheBuster}`),
            fetch(`abis/usdc.json?v=${cacheBuster}`)
        ]);
        
        if (!vaultResponse.ok || !usdcResponse.ok) {
            throw new Error("Failed to load ABI files");
        }
        
        VAULT_ABI = await vaultResponse.json();
        USDC_ABI = await usdcResponse.json();
        
        console.log("ABIs loaded, VAULT_ABI is array:", Array.isArray(VAULT_ABI));
        return true;
    } catch (error) {
        console.error("Error loading ABIs:", error);
        return false;
    }
}

// =============================================================================
// INVITE GATE
// =============================================================================

const INVITE_STORAGE_KEY = 'pmfi_access';

// Check if wallet has beta access
async function checkBetaAccess(walletAddress) {
    try {
        const res = await fetch('/api/invite/check-wallet', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ wallet: walletAddress })
        });
        const data = await res.json();
        return data.whitelisted === true;
    } catch (err) {
        console.error('Failed to check beta access:', err);
        return false;
    }
}

// Redeem invite code
async function redeemInviteCode(code, walletAddress) {
    try {
        const res = await fetch('/api/invite/redeem', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code, wallet: walletAddress })
        });
        return await res.json();
    } catch (err) {
        console.error('Failed to redeem code:', err);
        return { error: 'Network error. Please try again.' };
    }
}

// Initialize invite gate - New flow:
// 1. Connect wallet first (no code input on step 1)
// 2. Auto-check access - if wallet has access, enter app immediately
// 3. If no access, show code input + request access link
function initInviteGate() {
    const gate = document.getElementById('inviteGate');
    const step1 = document.getElementById('inviteStep1');
    const step2 = document.getElementById('inviteStep2');
    const codeInput = document.getElementById('inviteCodeInput');
    const connectBtn = document.getElementById('inviteConnectBtn');
    const redeemBtn = document.getElementById('inviteRedeemBtn');
    const backBtn = document.getElementById('inviteBackBtn');
    const walletDisplay = document.getElementById('inviteWalletDisplay');
    const error1 = document.getElementById('inviteError');
    const error2 = document.getElementById('inviteError2');
    const success = document.getElementById('inviteSuccess');

    let gateWalletAddress = null;

    // Check cached access
    const cachedAccess = localStorage.getItem(INVITE_STORAGE_KEY);
    if (cachedAccess) {
        try {
            const parsed = JSON.parse(cachedAccess);
            if (parsed.wallet && parsed.expires > Date.now()) {
                // Cached access valid, hide gate
                gate.classList.add('hidden');
                return;
            }
        } catch (e) {}
    }

    // Auto-uppercase code input (in step 2)
    codeInput.addEventListener('input', (e) => {
        e.target.value = e.target.value.toUpperCase();
    });

    // Connect wallet button (Step 1) - just connects wallet and checks access
    connectBtn.addEventListener('click', async () => {
        error1.classList.remove('show');
        connectBtn.disabled = true;
        connectBtn.textContent = 'Connecting...';

        try {
            if (typeof window.ethereum === 'undefined') {
                error1.textContent = 'Please install MetaMask or another wallet';
                error1.classList.add('show');
                return;
            }

            const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
            gateWalletAddress = accounts[0];

            // Update button to show checking status
            connectBtn.textContent = 'Checking access...';

            // Check if already has access
            const hasAccess = await checkBetaAccess(gateWalletAddress);
            if (hasAccess) {
                // Grant access immediately - no code needed
                localStorage.setItem(INVITE_STORAGE_KEY, JSON.stringify({
                    wallet: gateWalletAddress,
                    expires: Date.now() + (7 * 24 * 60 * 60 * 1000) // 7 days
                }));
                gate.classList.add('hidden');
                // Trigger main app connection
                if (typeof connectWallet === 'function') {
                    connectWallet();
                }
                return;
            }

            // No access - show step 2 with code input
            walletDisplay.textContent = `${gateWalletAddress.slice(0, 6)}...${gateWalletAddress.slice(-4)}`;
            step1.classList.remove('active');
            step2.classList.add('active');

        } catch (err) {
            console.error('Wallet connection failed:', err);
            error1.textContent = 'Wallet connection failed. Please try again.';
            error1.classList.add('show');
        } finally {
            connectBtn.disabled = false;
            connectBtn.textContent = 'Connect Wallet';
        }
    });

    // Redeem button (Step 2) - now gets code from step 2 input
    redeemBtn.addEventListener('click', async () => {
        const code = codeInput.value.trim();
        
        if (!code || code.length < 6) {
            error2.textContent = 'Please enter a valid invite code';
            error2.classList.add('show');
            return;
        }

        error2.classList.remove('show');
        success.classList.remove('show');
        redeemBtn.disabled = true;
        redeemBtn.textContent = 'Redeeming...';

        try {
            const result = await redeemInviteCode(code, gateWalletAddress);

            if (result.error) {
                error2.textContent = result.error;
                error2.classList.add('show');
            } else if (result.success) {
                success.textContent = result.message || 'Access granted!';
                success.classList.add('show');

                // Cache access
                localStorage.setItem(INVITE_STORAGE_KEY, JSON.stringify({
                    wallet: gateWalletAddress,
                    expires: Date.now() + (7 * 24 * 60 * 60 * 1000) // 7 days
                }));

                // Hide gate after short delay
                setTimeout(() => {
                    gate.classList.add('hidden');
                    // Trigger main app connection
                    if (typeof connectWallet === 'function') {
                        connectWallet();
                    }
                }, 1500);
            }
        } catch (err) {
            error2.textContent = 'Failed to redeem code. Please try again.';
            error2.classList.add('show');
        } finally {
            redeemBtn.disabled = false;
            redeemBtn.textContent = 'Redeem Code';
        }
    });

    // Back button - go back to step 1
    backBtn.addEventListener('click', () => {
        step2.classList.remove('active');
        step1.classList.add('active');
        error2.classList.remove('show');
        success.classList.remove('show');
        codeInput.value = '';
        gateWalletAddress = null;
    });
}

// Initialize gate on load
document.addEventListener('DOMContentLoaded', initInviteGate);

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
const tvlValueEl = document.getElementById("tvlValue");
const statsSharePriceEl = document.getElementById("statsSharePrice");
const userStatsEl = document.getElementById("userStats");
const positionValueEl = document.getElementById("positionValue");
const sharesBalanceEl = document.getElementById("sharesBalance");
const openDepositBtn = document.getElementById("openDepositBtn");
const openWithdrawBtn = document.getElementById("openWithdrawBtn");
const depositModal = document.getElementById("depositModal");
const withdrawModal = document.getElementById("withdrawModal");
const closeDepositModal = document.getElementById("closeDepositModal");
const closeWithdrawModal = document.getElementById("closeWithdrawModal");
const depositAmountEl = document.getElementById("depositAmount");
const withdrawAmountEl = document.getElementById("withdrawAmount");
const depositBtn = document.getElementById("depositBtn");
const withdrawBtn = document.getElementById("withdrawBtn");
const txStatus = document.getElementById("txStatus");
const withdrawTxStatus = document.getElementById("withdrawTxStatus");
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

    openWithdrawBtn.addEventListener("click", () => {
        withdrawModal.classList.remove("hidden");
        withdrawAmountEl.value = "";
        hideStatus(withdrawTxStatus);
        refreshUserStats();
    });
    
    closeWithdrawModal.addEventListener("click", () => {
        withdrawModal.classList.add("hidden");
        withdrawAmountEl.value = "";
        hideStatus(withdrawTxStatus);
    });
    
    withdrawModal.addEventListener("click", (e) => {
        if (e.target === withdrawModal) {
            withdrawModal.classList.add("hidden");
            withdrawAmountEl.value = "";
            hideStatus(withdrawTxStatus);
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
    statsSharePriceEl.textContent = `$${sharePrice.toFixed(2)}`;
    
    const tvl = priceData.total_assets || 0;
    const tvlStr = `$${tvl.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
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
    // V6 contract doesn't have totalAssets() - it uses off-chain NAV calculation
    // This function is kept for compatibility but returns 0
    console.warn("getTotalAssets: V6 uses off-chain NAV, returning 0");
    return 0n;
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
        // First try to get stats from price API (includes PM positions)
        if (PRICE_API_URL) {
            const priceData = await fetchPriceFromAPI();
            if (priceData) {
                updatePriceDisplay(priceData);
                return;
            }
        }
        
        // Fallback: try to read totalSupply from contract (V6 doesn't have totalAssets)
        try {
            const totalSupply = await getTotalSupply();
            
            // For V6, we can't calculate TVL from contract alone - show supply only
            if (totalSupply > 0n) {
                const supplyNum = Number(totalSupply) / 1e18;
                console.log(`Total supply: ${supplyNum} pSNIPER (TVL requires price API)`);
            }
            
            // Default to $1/share when no API data available
            statsSharePriceEl.textContent = `$1.00`;
            tvlValueEl.textContent = `$0.00`;
        } catch (e) {
            console.warn("Could not read contract state:", e);
            statsSharePriceEl.textContent = `$1.00`;
            tvlValueEl.textContent = `$0.00`;
        }

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
        refreshUserStats(),
        loadPendingWithdrawals()
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
// SIGNED NAV HELPER (V7 compatible)
// =============================================================================

async function getSignedNav(retryCount = 0) {
    if (!PRICE_API_URL) {
        throw new Error("VPS bot URL not configured. Set it in browser console: localStorage.setItem('predictfi_price_api_url', 'http://your-vps-ip:8080')");
    }
    
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 10000); // 10s timeout
    
    try {
        const response = await fetch(`${PRICE_API_URL}/sign-nav`, {
            signal: controller.signal
        });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error("Failed to get signed NAV from bot");
        }
        
        const data = await response.json();
        if (data.error) {
            throw new Error(data.error);
        }
        
        console.log("Got signed NAV:", data);
        return data;
    } catch (error) {
        clearTimeout(timeoutId);
        
        if (error.name === 'AbortError' && retryCount < 1) {
            console.log("Oracle slow, retrying...");
            return getSignedNav(retryCount + 1);
        }
        
        throw error;
    }
}

function parseNavData(signedNav) {
    const nd = signedNav.navData;
    
    if (nd.totalAssets !== undefined) {
        return {
            totalAssets: BigInt(nd.totalAssets),
            creditedCash: BigInt(nd.creditedCash),
            creditedPositions: BigInt(nd.creditedPositions),
            pendingCredit: BigInt(nd.pendingCredit),
            inFlightOnChain: BigInt(nd.inFlightOnChain),
            timestamp: nd.timestamp,
            deadline: nd.deadline,
            roundId: nd.roundId
        };
    }
    
    return {
        nav: BigInt(nd.nav),
        timestamp: nd.timestamp,
        deadline: nd.deadline,
        roundId: nd.roundId
    };
}

function isV7NavData(navData) {
    return navData.totalAssets !== undefined;
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

    if (!PRICE_API_URL) {
        showStatus(txStatus, "VPS bot URL not configured", "error");
        return;
    }

    // Check network FIRST before anything else
    const isCorrectNetwork = await checkNetwork();
    if (!isCorrectNetwork) {
        showStatus(txStatus, "Please switch to Base Mainnet", "error");
        await switchToBase();
        return;
    }

    const amount = parseUSDC(amountStr);

    try {
        depositBtn.disabled = true;
        hideStatus(txStatus);

        // Ensure we're using signer-connected contracts
        const signerUsdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, signer);
        const signerVaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);

        // STEP 1: Handle approval FIRST (no NAV needed)
        showStatus(txStatus, "Checking allowance...", "info");
        console.log("Checking allowance for", userAddress, "to", VAULT_ADDRESS);
        const allowance = await signerUsdcContract.allowance(userAddress, VAULT_ADDRESS);
        console.log("Allowance:", allowance.toString());

        if (allowance < amount) {
            showStatus(txStatus, "Approving USDC...", "info");
            const approveTx = await signerUsdcContract.approve(VAULT_ADDRESS, amount);
            showStatus(txStatus, "Waiting for approval...", "info");
            await approveTx.wait();
            showStatus(txStatus, "Approved! Getting signed price...", "info");
        }

        // STEP 2: Get signed NAV ONLY when ready to deposit
        showStatus(txStatus, "Getting signed price...", "info");
        let signedNav;
        try {
            signedNav = await getSignedNav();
        } catch (navError) {
            if (navError.name === 'AbortError') {
                showStatus(txStatus, "Oracle slow, retrying...", "info");
                signedNav = await getSignedNav(1);
            } else {
                throw navError;
            }
        }
        
        // Extract NavData struct and signature (V7 compatible)
        const navData = parseNavData(signedNav);
        const signature = signedNav.signature;

        // STEP 3: Execute deposit (don't block on estimateGas)
        showStatus(txStatus, "Depositing...", "info");
        console.log("Calling deposit with:", { amount: amount.toString(), navData, signature });
        
        let depositTx;
        try {
            depositTx = await signerVaultContract.deposit(amount, navData, signature);
        } catch (callError) {
            console.error("Deposit call error:", callError);
            // Surface the actual revert reason
            let revertReason = callError.reason || callError.data?.message || callError.message;
            if (revertReason.includes("NAV too old")) {
                revertReason = "Price data expired. Please try again.";
            } else if (revertReason.includes("Invalid signature")) {
                revertReason = "Invalid oracle signature. Contact support.";
            } else if (revertReason.includes("exceeds wallet cap")) {
                revertReason = "Deposit exceeds your wallet cap.";
            }
            throw new Error(revertReason);
        }
        
        showStatus(txStatus, "Confirming...", "info");
        await depositTx.wait();

        showStatus(txStatus, `Deposited $${amountStr} USDC`, "success");
        depositAmountEl.value = "";
        
        // Force refresh the price API to update TVL immediately
        if (PRICE_API_URL) {
            await requestFreshPrice();
        }
        await refreshAll();

    } catch (error) {
        console.error("Deposit error:", error);
        let errorMsg = error.reason || error.message;
        if (error.code === "ACTION_REJECTED") {
            errorMsg = "Transaction rejected by user.";
        } else if (errorMsg.includes("insufficient")) {
            errorMsg = "Insufficient USDC balance.";
        } else if (errorMsg.includes("VPS") || errorMsg.includes("fetch")) {
            errorMsg = "VPS bot not reachable. Check your bot URL.";
        }
        showStatus(txStatus, errorMsg, "error");
    } finally {
        depositBtn.disabled = false;
    }
}

// =============================================================================
// WITHDRAW (V6: requestWithdraw creates a withdrawal request)
// =============================================================================

async function handleWithdraw() {
    const amountStr = withdrawAmountEl.value;
    if (!amountStr || Number(amountStr) <= 0) {
        showStatus(withdrawTxStatus, "Enter a valid pSNIPER amount", "error");
        return;
    }

    const MIN_WITHDRAWAL_USDC = 5.0;
    const shareCount = Number(amountStr);
    const currentPrice = parseFloat(document.getElementById("sharePrice")?.textContent?.replace("$", "") || "1");
    const estimatedUsdc = shareCount * currentPrice;
    if (estimatedUsdc < MIN_WITHDRAWAL_USDC) {
        showStatus(withdrawTxStatus, `Minimum withdrawal is $${MIN_WITHDRAWAL_USDC} (≈${(MIN_WITHDRAWAL_USDC / currentPrice).toFixed(2)} shares at current price)`, "error");
        return;
    }

    if (!signer || !userAddress) {
        showStatus(withdrawTxStatus, "Please connect your wallet first", "error");
        return;
    }

    if (!PRICE_API_URL) {
        showStatus(withdrawTxStatus, "VPS bot URL not configured", "error");
        return;
    }

    // Convert pSNIPER shares to 18 decimals
    const shareAmount = ethers.parseUnits(amountStr, 18);

    try {
        withdrawBtn.disabled = true;
        hideStatus(withdrawTxStatus);

        // Get signed NAV from VPS bot
        showStatus(withdrawTxStatus, "Getting signed price...", "info");
        const signedNav = await getSignedNav();
        
        // Extract NavData struct and signature (V7 compatible)
        const navData = parseNavData(signedNav);
        const signature = signedNav.signature;

        // Ensure we're using signer-connected contract
        const signerVaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);

        showStatus(withdrawTxStatus, "Requesting withdrawal...", "info");
        console.log("Calling requestWithdraw with:", { shareAmount: shareAmount.toString(), navData, signature });
        const withdrawTx = await signerVaultContract.requestWithdraw(shareAmount, navData, signature);
        showStatus(withdrawTxStatus, "Confirming...", "info");
        await withdrawTx.wait();

        showStatus(withdrawTxStatus, `Withdrawal requested for ${amountStr} pSNIPER. Claim when funds available.`, "success");
        withdrawAmountEl.value = "";
        await refreshAll();
        setTimeout(() => {
            withdrawModal.classList.add("hidden");
            hideStatus(withdrawTxStatus);
        }, 3000);

    } catch (error) {
        console.error("Withdraw error:", error);
        let errorMsg = error.reason || error.message;
        if (errorMsg.includes("Insufficient liquidity")) {
            errorMsg = "Insufficient liquidity. Please try a smaller amount or wait for funds to be freed.";
        }
        showStatus(withdrawTxStatus, errorMsg, "error");
    } finally {
        withdrawBtn.disabled = false;
    }
}

// =============================================================================
// PENDING WITHDRAWALS
// =============================================================================

async function loadPendingWithdrawals() {
    if (!userAddress || !vaultContract) return;
    
    const section = document.getElementById("pendingWithdrawalsSection");
    const list = document.getElementById("pendingWithdrawalsList");
    
    try {
        // Get user's withdrawal request IDs
        const requestIds = await vaultContract.getUserWithdrawals(userAddress);
        
        if (!requestIds || requestIds.length === 0) {
            section.classList.add("hidden");
            return;
        }
        
        // Fetch details for each request
        // V7.3: getWithdrawalRequest returns [user, shares, usdcLocked, requestTime, claimed, expired]
        const pendingRequests = [];
        for (const requestId of requestIds) {
            const req = await vaultContract.getWithdrawalRequest(requestId);
            // req = [user, shares, usdcLocked, requestTime, claimed, expired]
            if (!req[4]) { // not claimed
                pendingRequests.push({
                    id: requestId,
                    shares: req[1],
                    usdcLocked: req[2],  // V7.3: Locked USDC amount
                    requestTime: req[3],
                    expired: req[5]
                });
            }
        }
        
        if (pendingRequests.length === 0) {
            section.classList.add("hidden");
            return;
        }
        
        // Render list - V7.3: Use locked USDC value (not estimated from current NAV)
        list.innerHTML = pendingRequests.map(req => {
            const sharesFormatted = Number(ethers.formatUnits(req.shares, 18)).toFixed(2);
            // V7.3: Show exact locked amount (guaranteed payout)
            const lockedUsdc = Number(ethers.formatUnits(req.usdcLocked, 6)).toFixed(2);
            const requestDate = new Date(Number(req.requestTime) * 1000).toLocaleString();
            
            return `
                <div class="pending-item">
                    <div class="pending-info">
                        <div class="pending-shares">${sharesFormatted} pSNIPER</div>
                        <div class="pending-value">$${lockedUsdc} USDC (locked)</div>
                        <div class="pending-time">Requested: ${requestDate}</div>
                    </div>
                    <button class="claim-btn ${req.expired ? 'expired' : ''}" 
                            onclick="handleClaim(${req.id})" 
                            ${req.expired ? 'title="Expired - reclaim shares"' : ''}>
                        ${req.expired ? 'Reclaim' : 'Claim'}
                    </button>
                </div>
            `;
        }).join('');
        
        section.classList.remove("hidden");
        
    } catch (error) {
        console.error("Error loading pending withdrawals:", error);
        section.classList.add("hidden");
    }
}

async function handleClaim(requestId) {
    if (!signer || !userAddress) {
        alert("Please connect your wallet first");
        return;
    }
    
    const claimBtn = event.target;
    const isExpired = claimBtn.classList.contains('expired');
    
    try {
        claimBtn.disabled = true;
        claimBtn.textContent = "Processing...";
        
        const signerVaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);
        
        let tx;
        if (isExpired) {
            // Reclaim expired request - get shares back
            tx = await signerVaultContract.cancelExpiredWithdrawal(requestId);
        } else {
            // V7.3: Simplified claim - no NAV needed, price was locked at request time
            tx = await signerVaultContract.claim(requestId);
        }
        
        await tx.wait();
        
        alert(isExpired ? "Shares reclaimed successfully!" : "Withdrawal claimed successfully!");
        await refreshAll();
        await loadPendingWithdrawals();
        
    } catch (error) {
        console.error("Claim error:", error);
        let errorMsg = error.reason || error.message;
        if (errorMsg.includes("Insufficient buffer") || errorMsg.includes("Not enough USDC")) {
            errorMsg = "Not enough USDC in buffer. Try again later when positions are liquidated.";
        }
        alert("Claim failed: " + errorMsg);
    } finally {
        claimBtn.disabled = false;
        claimBtn.textContent = isExpired ? "Reclaim" : "Claim";
    }
}

// =============================================================================
// EVENT LISTENERS
// =============================================================================

connectBtn.addEventListener("click", connectWallet);
depositBtn.addEventListener("click", handleDeposit);
withdrawBtn.addEventListener("click", handleWithdraw);

const depositMaxBtn = document.getElementById("depositMaxBtn");
const withdrawMaxBtn = document.getElementById("withdrawMaxBtn");

if (depositMaxBtn) {
    depositMaxBtn.addEventListener("click", async () => {
        if (!usdcContract || !userAddress) return;
        try {
            const balance = await usdcContract.balanceOf(userAddress);
            depositAmountEl.value = ethers.formatUnits(balance, 6);
        } catch (e) {
            console.error("Max USDC balance error:", e);
        }
    });
}

if (withdrawMaxBtn) {
    withdrawMaxBtn.addEventListener("click", async () => {
        if (!vaultContract || !userAddress) return;
        try {
            const shares = await vaultContract.balanceOf(userAddress);
            withdrawAmountEl.value = parseFloat(ethers.formatUnits(shares, 18)).toFixed(2);
        } catch (e) {
            console.error("Max pSNIPER balance error:", e);
        }
    });
}

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

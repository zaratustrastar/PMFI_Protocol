/**
 * PredictFi pSNIPER - Frontend
 */

// =============================================================================
// CONFIGURATION
// =============================================================================

// Read from global config (set by HTML template) or use default
const VAULT_ADDRESS         = window.PSNIPER_CONFIG?.VAULT_ADDRESS || "0x17C27001929E75D1eBd5FdeE6E986EA5a91de0D1";
const ARB_VAULT_V2_ADDRESS  = window.PSNIPER_CONFIG?.ARB_VAULT_V2_ADDRESS || "";

// ── pARB vault allowlist ──────────────────────────────────────────────────────
// Only wallets in this list can see and interact with the pARBITRAGE vault.
// To open the vault to everyone, set this to an empty array: []
function _isArbAllowed(_addr) {
    return true;
}

function _updateArbVaultVisibility(_addr) {
    const card = document.getElementById('arbVaultCard');
    if (!card) return;
    card.style.display = '';
}
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
let ARB_VAULT_V2_ABI = null;

async function loadABIs() {
    try {
        const cacheBuster = Date.now();
        const [vaultResponse, usdcResponse, arbVaultV2Response] = await Promise.all([
            fetch(`abis/vault.json?v=${cacheBuster}`),
            fetch(`abis/usdc.json?v=${cacheBuster}`),
            fetch(`abis/arb-vault-v2.json?v=${cacheBuster}`)
        ]);
        
        if (!vaultResponse.ok || !usdcResponse.ok) {
            throw new Error("Failed to load ABI files");
        }
        
        VAULT_ABI = await vaultResponse.json();
        USDC_ABI = await usdcResponse.json();
        if (arbVaultV2Response.ok) ARB_VAULT_V2_ABI = await arbVaultV2Response.json();
        
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

// Invite gate disabled — app is open to everyone
function initInviteGate() {
    const gate = document.getElementById('inviteGate');
    if (gate) gate.classList.add('hidden');
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
let arbVaultV2Contract = null;
let usdcContract = null;
let abisLoaded = false;
let refreshTimer = null;
let priceRefreshTimer = null;
let arbRefreshTimer = null;
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

async function loadArbVaultStats() {
    if (!ARB_VAULT_V2_ADDRESS || !ARB_VAULT_V2_ABI) return;
    try {
        const provider = new ethers.JsonRpcProvider(BASE_MAINNET_RPC);
        const c = new ethers.Contract(ARB_VAULT_V2_ADDRESS, ARB_VAULT_V2_ABI, provider);
        const vs = await c.getVaultState();
        // vs[0]=officialPPS, vs[1]=totalSupply, vs[2]=idleBalance,
        // vs[3]=lastReportedBacking, vs[4]=lossCarryforward,
        // vs[5]=pendingDepositAssets, vs[6]=claimableRedeemAssets,
        // vs[7]=pendingRedeemShares
        const pps          = Number(vs[0]) / 1e6;
        // TVL = officialPPS × totalSupply — this represents the total value of all
        // outstanding shares at the current price per share, regardless of where the
        // underlying capital is deployed (vault idle, servicer wallet, or on platforms).
        // Previous formula used (idleBalance + lastReportedBacking) which drops to ~$1
        // after tend() moves 90% of vault USDC to the servicer wallet.
        // Use BigInt arithmetic: avoid float precision loss on large 1e18-unit values.
        // tvlUsdc6 = pps_1e6 × totalSupply_1e18 / 1e18 → result in USDC×1e6 units.
        const tvlUsdc6 = BigInt(vs[0]) * BigInt(vs[1]) / 1000000000000000000n;
        const tvl      = Number(tvlUsdc6) / 1e6;
        const pendingIn    = Number(vs[5]) / 1e6;
        const pendingOut   = (Number(vs[7]) / 1e18) * pps;

        const elPPS        = document.getElementById('arbStatPPS');
        const elTVL        = document.getElementById('arbStatTVL');
        const elPendingIn  = document.getElementById('arbStatPendingIn');
        const elPendingOut = document.getElementById('arbStatPendingOut');

        if (elPPS)        elPPS.textContent        = '$' + pps.toFixed(4);
        if (elTVL)        elTVL.textContent        = '$' + tvl.toLocaleString('en-US', { maximumFractionDigits: 0 });
        if (elPendingIn)  elPendingIn.textContent  = pendingIn  > 0 ? '$' + pendingIn.toFixed(2)  : '—';
        if (elPendingOut) elPendingOut.textContent = pendingOut > 0 ? '$' + pendingOut.toFixed(2) : '—';
    } catch (e) {
        console.warn('[pARB V2] loadArbVaultStats error:', e);
    }
}

async function refreshAll() {
    await Promise.all([
        refreshVaultStats(),
        refreshUserStats(),
        loadPendingWithdrawals(),
        refreshArbUserStats(),
        loadArbVaultStats(),
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
        if (window.__builderAttribution) signer = window.__builderAttribution.wrapSigner(signer);
        userAddress = await signer.getAddress();

        vaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);
        usdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, signer);
        if (ARB_VAULT_V2_ADDRESS && ARB_VAULT_V2_ABI) {
            arbVaultV2Contract = new ethers.Contract(ARB_VAULT_V2_ADDRESS, ARB_VAULT_V2_ABI, signer);
        }

        isConnected = true;
        connectBtn.textContent = "Disconnect";
        connectBtn.classList.add("connected");
        connectBtn.disabled = false;

        const _arbDepBtn = document.getElementById('openArbDepositBtn');
        if (_arbDepBtn && ARB_VAULT_V2_ADDRESS) _arbDepBtn.disabled = false;

        _updateArbVaultVisibility(userAddress);

        await refreshAll();
        startAutoRefresh();

        // Refresh tab content that depends on wallet
        if (webActiveTab === 'tasks') { webLoadTasks(); webLoadInviteCodes(); }
        if (webActiveTab === 'rankings') webLoadLeaderboard();

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
    arbVaultV2Contract = null;

    connectBtn.textContent = "Connect Wallet";
    connectBtn.classList.remove("connected");
    
    openDepositBtn.disabled = true;
    userStatsEl.classList.add("hidden");
    const _arbDepBtn = document.getElementById('openArbDepositBtn');
    if (_arbDepBtn) _arbDepBtn.disabled = true;
    const _arbStats = document.getElementById('arbUserStats');
    if (_arbStats) _arbStats.classList.add('hidden');
    _updateArbVaultVisibility(null);

    // Reinitialize with read-only provider
    initReadOnlyProvider();
}

function handleAccountsChanged(accounts) {
    if (accounts.length === 0) {
        disconnectWallet();
    } else {
        userAddress = accounts[0];
        _updateArbVaultVisibility(userAddress);
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
// pARBITRAGE VAULT V2 — async request/claim model (no live NAV required)
// =============================================================================

// Request status enum matches contract: 0=PENDING, 1=CLAIMABLE, 2=CLAIMED, 3=CANCELLED
const ARB_REQ_STATUS = { 0: 'Pending', 1: 'Claimable', 2: 'Claimed', 3: 'Cancelled' };

function _arbV2Contract(withSigner = false) {
    const addr = ARB_VAULT_V2_ADDRESS;
    const abi  = ARB_VAULT_V2_ABI;
    if (!addr || !abi) return null;
    if (withSigner && arbVaultV2Contract) return arbVaultV2Contract;
    return new ethers.Contract(addr, abi, withSigner ? signer : new ethers.JsonRpcProvider(BASE_MAINNET_RPC));
}

function _arbActiveAddress() {
    return ARB_VAULT_V2_ADDRESS;
}

async function refreshArbUserStats() {
    if (!userAddress) return;

    // V2 path
    if (ARB_VAULT_V2_ADDRESS && ARB_VAULT_V2_ABI) {
        try {
            const c = _arbV2Contract();
            const [shares, vaultState] = await Promise.all([
                c.balanceOf(userAddress),
                c.getVaultState(),
            ]);
            const arbStats    = document.getElementById('arbUserStats');
            const arbSharesBal = document.getElementById('arbSharesBalance');
            const arbPosVal   = document.getElementById('arbPositionValue');
            if (!arbStats) return;

            const officialPPS = vaultState[0];   // USDC per 1e18 shares
            const sharesNum   = Number(shares) / 1e18;
            const posValueUsdc = sharesNum * Number(officialPPS) / 1e6;

            if (shares === 0n) {
                arbStats.classList.add('hidden');
            } else {
                arbStats.classList.remove('hidden');
                if (arbSharesBal) arbSharesBal.textContent = sharesNum.toFixed(4);
                if (arbPosVal)    arbPosVal.textContent = '$' + posValueUsdc.toFixed(2);
            }
            await loadArbPendingRequests();
        } catch (e) {
            console.warn('[pARB V2] refreshArbUserStats error:', e);
        }
        return;
    }
}

// ── V2 Deposit: requestDeposit → (wait for report) → claimDeposit ──────────

async function handleArbDeposit() {
    const amountEl = document.getElementById('arbDepositAmount');
    const statusEl = document.getElementById('arbDepositStatus');
    const btn      = document.getElementById('arbDepositBtn');
    const amountStr = amountEl?.value;
    if (!amountStr || Number(amountStr) <= 0) { showStatus(statusEl, 'Enter a valid amount', 'error'); return; }
    if (Number(amountStr) < 10)               { showStatus(statusEl, 'Minimum deposit is $10 USDC', 'error'); return; }
    if (!signer || !userAddress)              { showStatus(statusEl, 'Connect your wallet first', 'error'); return; }
    if (!_isArbAllowed(userAddress))          { showStatus(statusEl, 'Deposits restricted to authorized wallets', 'error'); return; }
    if (!_arbActiveAddress())                 { showStatus(statusEl, 'pARB vault not configured', 'error'); return; }
    const isCorrectNetwork = await checkNetwork();
    if (!isCorrectNetwork) { showStatus(statusEl, 'Switch to Base Mainnet', 'error'); await switchToBase(); return; }

    const amount = parseUSDC(amountStr);

    // ── V2 path ──────────────────────────────────────────────────────────────
    if (ARB_VAULT_V2_ADDRESS && ARB_VAULT_V2_ABI) {
        try {
            if (btn) btn.disabled = true;
            hideStatus(statusEl);

            const signerUsdc = new ethers.Contract(USDC_ADDRESS, USDC_ABI, signer);
            showStatus(statusEl, 'Checking allowance...', 'info');
            const allowance = await signerUsdc.allowance(userAddress, ARB_VAULT_V2_ADDRESS);
            if (allowance < amount) {
                showStatus(statusEl, 'Approving USDC...', 'info');
                const approveTx = await signerUsdc.approve(ARB_VAULT_V2_ADDRESS, amount);
                showStatus(statusEl, 'Waiting for approval...', 'info');
                await approveTx.wait();
            }

            showStatus(statusEl, 'Submitting deposit request...', 'info');
            const signerArb = _arbV2Contract(true);
            const tx = await signerArb.requestDeposit(amount, userAddress);
            showStatus(statusEl, 'Confirming...', 'info');
            const receipt = await tx.wait();

            // Parse requestId from DepositRequested event
            let requestId = null;
            for (const log of receipt.logs) {
                try {
                    const parsed = signerArb.interface.parseLog(log);
                    if (parsed?.name === 'DepositRequested') {
                        requestId = parsed.args.requestId.toString();
                    }
                } catch (_) {}
            }

            const msg = requestId !== null
                ? `Deposit request #${requestId} submitted for ${amountStr} USDC. Shares will be issued after the next report (~1 hour).`
                : `Deposit request submitted for ${amountStr} USDC. Shares will be issued after the next report.`;
            showStatus(statusEl, msg, 'success');
            if (amountEl) amountEl.value = '';
            await refreshArbUserStats();
            setTimeout(() => {
                document.getElementById('arbDepositModal')?.classList.add('hidden');
                hideStatus(statusEl);
            }, 5000);
        } catch (e) {
            console.error('[pARB V2] deposit error:', e);
            showStatus(statusEl, e.reason || e.message, 'error');
        } finally {
            if (btn) btn.disabled = false;
        }
        return;
    }
}

// ── V2 Redeem: requestRedeem → (wait for report + liquidity) → claimRedeem ──

async function handleArbWithdrawRequest() {
    const amountEl = document.getElementById('arbWithdrawAmount');
    const statusEl = document.getElementById('arbWithdrawStatus');
    const btn      = document.getElementById('arbWithdrawBtn');
    const amountStr = amountEl?.value;
    if (!amountStr || Number(amountStr) <= 0) { showStatus(statusEl, 'Enter a valid share amount', 'error'); return; }
    if (!signer || !userAddress)              { showStatus(statusEl, 'Connect your wallet first', 'error'); return; }
    if (!_isArbAllowed(userAddress))          { showStatus(statusEl, 'Withdrawals restricted to authorized wallets', 'error'); return; }
    if (!_arbActiveAddress())                 { showStatus(statusEl, 'pARB vault not configured', 'error'); return; }
    const isCorrectNetwork = await checkNetwork();
    if (!isCorrectNetwork) { showStatus(statusEl, 'Switch to Base Mainnet', 'error'); await switchToBase(); return; }

    const shareAmount = ethers.parseUnits(amountStr, 18);

    // ── V2 path ──────────────────────────────────────────────────────────────
    if (ARB_VAULT_V2_ADDRESS && ARB_VAULT_V2_ABI) {
        try {
            if (btn) btn.disabled = true;
            hideStatus(statusEl);
            showStatus(statusEl, 'Submitting redeem request...', 'info');
            const signerArb = _arbV2Contract(true);
            const tx = await signerArb.requestRedeem(shareAmount, userAddress);
            showStatus(statusEl, 'Confirming...', 'info');
            const receipt = await tx.wait();

            let requestId = null;
            for (const log of receipt.logs) {
                try {
                    const parsed = signerArb.interface.parseLog(log);
                    if (parsed?.name === 'RedeemRequested') requestId = parsed.args.requestId.toString();
                } catch (_) {}
            }

            const msg = requestId !== null
                ? `Redeem request #${requestId} submitted for ${amountStr} pARB. USDC claimable after next report when vault has liquidity.`
                : `Redeem request submitted. USDC claimable after next report when vault has liquidity.`;
            showStatus(statusEl, msg, 'success');
            if (amountEl) amountEl.value = '';
            await refreshArbUserStats();
            setTimeout(() => { document.getElementById('arbWithdrawModal')?.classList.add('hidden'); hideStatus(statusEl); }, 5000);
        } catch (e) {
            console.error('[pARB V2] redeem error:', e);
            showStatus(statusEl, e.reason || e.message, 'error');
        } finally {
            if (btn) btn.disabled = false;
        }
        return;
    }
}

// ── V2 Claim deposit (after report() has processed the request) ──────────────

async function handleArbClaimDeposit(requestId) {
    if (!signer || !userAddress || !ARB_VAULT_V2_ADDRESS) return;
    const statusEl = document.getElementById(`arbDepReqStatus_${requestId}`);
    const btn      = document.getElementById(`arbDepReqClaimBtn_${requestId}`);
    try {
        if (btn) btn.disabled = true;
        if (statusEl) statusEl.textContent = 'Claiming shares...';
        const signerArb = _arbV2Contract(true);
        const tx = await signerArb.claimDeposit(BigInt(requestId), userAddress);
        await tx.wait();
        if (statusEl) statusEl.textContent = 'Shares claimed!';
        await refreshArbUserStats();
    } catch (e) {
        console.error('[pARB V2] claimDeposit error:', e);
        if (statusEl) statusEl.textContent = e.reason || e.message;
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ── V2 Claim redeem (after report() + liquidity available) ──────────────────

async function handleArbClaimRedeem(requestId) {
    if (!signer || !userAddress || !ARB_VAULT_V2_ADDRESS) return;
    const statusEl = document.getElementById(`arbRdmReqStatus_${requestId}`);
    const btn      = document.getElementById(`arbRdmReqClaimBtn_${requestId}`);
    try {
        if (btn) btn.disabled = true;
        if (statusEl) statusEl.textContent = 'Claiming USDC...';
        const signerArb = _arbV2Contract(true);
        const tx = await signerArb.claimRedeem(BigInt(requestId), userAddress);
        await tx.wait();
        if (statusEl) statusEl.textContent = 'USDC received!';
        await refreshArbUserStats();
    } catch (e) {
        console.error('[pARB V2] claimRedeem error:', e);
        if (statusEl) statusEl.textContent = e.reason || e.message;
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ── V2 Pending request loader ─────────────────────────────────────────────────

async function loadArbPendingRequests() {
    if (!userAddress || !ARB_VAULT_V2_ADDRESS || !ARB_VAULT_V2_ABI) return;
    const container = document.getElementById('arbPendingRequests');
    if (!container) return;

    try {
        const c = _arbV2Contract();
        const [depIds, rdmIds] = await Promise.all([
            c.getUserDepositRequests(userAddress),
            c.getUserRedeemRequests(userAddress),
        ]);

        const activeDepIds = depIds.filter(id => true); // fetch all, filter by status below
        const activeRdmIds = rdmIds.filter(id => true);

        const depRequests = await Promise.all(activeDepIds.map(id => c.getDepositRequest(id)));
        const rdmRequests = await Promise.all(activeRdmIds.map(id => c.getRedeemRequest(id)));

        // Only show PENDING (0) or CLAIMABLE (1) — hide CLAIMED/CANCELLED
        const activeDeps = depRequests.map((r, i) => ({ id: activeDepIds[i], ...r }))
            .filter(r => r.status === 0n || r.status === 1n);
        const activeRdms = rdmRequests.map((r, i) => ({ id: activeRdmIds[i], ...r }))
            .filter(r => r.status === 0n || r.status === 1n);

        if (activeDeps.length === 0 && activeRdms.length === 0) {
            container.innerHTML = '';
            container.classList.add('hidden');
            return;
        }

        container.classList.remove('hidden');

        let html = '<div class="arb-requests-section">';
        html += '<h4 style="margin-bottom:8px;font-size:13px;color:#9ca3af;">Active Requests</h4>';

        for (const req of activeDeps) {
            const id     = req.id.toString();
            const status = Number(req.status);
            const assets = (Number(req.assets) / 1e6).toFixed(2);
            const estShares = (Number(req.estimatedShares) / 1e18).toFixed(4);
            const canClaim = status === 1;
            html += `
            <div class="arb-request-row" style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1f2937;">
                <span style="font-size:12px;">
                    <span style="color:#6b7280;">Deposit #${id}</span>
                    <span style="color:${canClaim ? '#10b981' : '#f59e0b'};margin-left:6px;">${ARB_REQ_STATUS[status]}</span>
                    <span style="color:#9ca3af;margin-left:6px;">$${assets} USDC → ~${estShares} pARB</span>
                </span>
                ${canClaim ? `<button id="arbDepReqClaimBtn_${id}" onclick="handleArbClaimDeposit('${id}')" class="btn btn-sm btn-primary" style="padding:3px 10px;font-size:11px;">Claim Shares</button>` : '<span style="color:#6b7280;font-size:11px;">Awaiting report</span>'}
                <span id="arbDepReqStatus_${id}" style="font-size:11px;color:#9ca3af;margin-left:6px;"></span>
            </div>`;
        }

        for (const req of activeRdms) {
            const id     = req.id.toString();
            const status = Number(req.status);
            const shares = (Number(req.shares) / 1e18).toFixed(4);
            const estUsdc = (Number(req.estimatedAssets) / 1e6).toFixed(2);
            const canClaim = status === 1;
            html += `
            <div class="arb-request-row" style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1f2937;">
                <span style="font-size:12px;">
                    <span style="color:#6b7280;">Redeem #${id}</span>
                    <span style="color:${canClaim ? '#10b981' : '#f59e0b'};margin-left:6px;">${ARB_REQ_STATUS[status]}</span>
                    <span style="color:#9ca3af;margin-left:6px;">${shares} pARB → ~$${estUsdc}</span>
                </span>
                ${canClaim ? `<button id="arbRdmReqClaimBtn_${id}" onclick="handleArbClaimRedeem('${id}')" class="btn btn-sm btn-primary" style="padding:3px 10px;font-size:11px;">Claim USDC</button>` : '<span style="color:#6b7280;font-size:11px;">Awaiting report + liquidity</span>'}
                <span id="arbRdmReqStatus_${id}" style="font-size:11px;color:#9ca3af;margin-left:6px;"></span>
            </div>`;
        }

        html += '</div>';
        container.innerHTML = html;
    } catch (e) {
        console.warn('[pARB V2] loadArbPendingRequests error:', e);
    }
}

function initArbModals() {
    const depModal = document.getElementById('arbDepositModal');
    const wdModal  = document.getElementById('arbWithdrawModal');
    const openDep  = document.getElementById('openArbDepositBtn');
    const closeDep = document.getElementById('closeArbDepositModal');
    const openWd   = document.getElementById('openArbWithdrawBtn');
    const closeWd  = document.getElementById('closeArbWithdrawModal');
    const depBtn   = document.getElementById('arbDepositBtn');
    const wdBtn    = document.getElementById('arbWithdrawBtn');
    const wdMaxBtn = document.getElementById('arbWithdrawMaxBtn');

    if (openDep)  openDep.addEventListener('click',  () => depModal?.classList.remove('hidden'));
    if (closeDep) closeDep.addEventListener('click', () => depModal?.classList.add('hidden'));
    if (depModal) depModal.addEventListener('click',  e => { if (e.target === depModal) depModal.classList.add('hidden'); });

    if (openWd)  openWd.addEventListener('click',  () => wdModal?.classList.remove('hidden'));
    if (closeWd) closeWd.addEventListener('click', () => wdModal?.classList.add('hidden'));
    if (wdModal) wdModal.addEventListener('click',  e => { if (e.target === wdModal) wdModal.classList.add('hidden'); });

    if (wdMaxBtn) wdMaxBtn.addEventListener('click', async () => {
        const amountEl = document.getElementById('arbWithdrawAmount');
        if (!amountEl || !userAddress) return;
        try {
            const addr = ARB_VAULT_V2_ADDRESS;
            const abi  = ARB_VAULT_V2_ABI;
            if (!addr || !abi) return;
            const contract = new ethers.Contract(addr, abi, new ethers.JsonRpcProvider(BASE_MAINNET_RPC));
            const shares = await contract.balanceOf(userAddress);
            amountEl.value = (Number(shares) / 1e18).toFixed(6);
        } catch (_) {}
    });

    if (depBtn) depBtn.addEventListener('click', handleArbDeposit);
    if (wdBtn)  wdBtn.addEventListener('click',  handleArbWithdrawRequest);
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

// =============================================================================
// TAB NAVIGATION
// =============================================================================

const WEB_TABS = ['home', 'arbitrage', 'rankings', 'tasks'];
let webActiveTab = 'home';

function webSwitchTab(tabName) {
    if (webActiveTab === 'arbitrage' && tabName !== 'arbitrage') {
        if (arbRefreshTimer) { clearTimeout(arbRefreshTimer); arbRefreshTimer = null; }
    }
    WEB_TABS.forEach(t => {
        const panel = document.getElementById('webPanel' + capitalize(t));
        const btn = document.getElementById('webNavBtn' + capitalize(t));
        if (panel) panel.classList.remove('active');
        if (btn) btn.classList.remove('active');
    });
    const panel = document.getElementById('webPanel' + capitalize(tabName));
    const btn = document.getElementById('webNavBtn' + capitalize(tabName));
    if (panel) panel.classList.add('active');
    if (btn) btn.classList.add('active');
    webActiveTab = tabName;
    window.location.hash = tabName === 'home' ? '' : tabName;
    if (tabName === 'arbitrage') webLoadArb();
    if (tabName === 'rankings') webLoadLeaderboard();
    if (tabName === 'tasks') { webLoadTasks(); webLoadInviteCodes(); webRestoreFid(); }
}

function capitalize(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

// =============================================================================
// FARCASTER CONNECT (Tasks tab)
// =============================================================================

let webFcFid = localStorage.getItem('pmfi_web_fid') ? parseInt(localStorage.getItem('pmfi_web_fid')) : null;
let webFcUsername = localStorage.getItem('pmfi_web_fc_username') || null;
let _siwfAbort = null;
let _siwfCallback = null;

function webUpdateFcState() {
    webLoadTasks();
}

function webFollowFarcaster() {
    const warpcastUrl = 'https://warpcast.com/pmfi';
    if (webFcFid) {
        window.open(warpcastUrl, '_blank');
    } else {
        _siwfCallback = () => { window.open(warpcastUrl, '_blank'); };
        webConnectFarcaster();
    }
}

function webDisconnectFc() {
    webFcFid = null;
    webFcUsername = null;
    localStorage.removeItem('pmfi_web_fid');
    localStorage.removeItem('pmfi_web_fc_username');
    if (_siwfAbort) { _siwfAbort(); _siwfAbort = null; }
    webUpdateFcState();
}

function webRestoreFid() {
    const saved = localStorage.getItem('pmfi_web_fid');
    if (saved) webFcFid = parseInt(saved);
    webUpdateFcState();
}

function webSetSiwfStatus(msg) {
    const el = document.getElementById('webFcSiwfStatus');
    if (el) el.textContent = msg;
}

function webCloseSiwfModal() {
    const modal = document.getElementById('siwfModal');
    if (modal) modal.style.display = 'none';
    if (_siwfAbort) { _siwfAbort(); _siwfAbort = null; }
    const qrWrap = document.getElementById('siwfQrWrap');
    if (qrWrap) { qrWrap.style.display = 'none'; qrWrap.innerHTML = '<div id="webFcQrCanvas" style="display:inline-block;background:#fff;padding:8px;border-radius:8px;"></div><p style="font-size:12px;color:rgba(255,255,255,0.35);margin-top:8px;">Scan with Warpcast</p>'; }
    const deeplink = document.getElementById('webFcDeeplink');
    if (deeplink) deeplink.style.display = 'none';
}

async function webConnectFarcaster() {
    const modal = document.getElementById('siwfModal');
    if (!modal) return;
    modal.style.display = 'flex';
    webSetSiwfStatus('Generating sign-in link...');
    const qrWrap = document.getElementById('siwfQrWrap');
    if (qrWrap) qrWrap.style.display = 'none';
    const deeplink = document.getElementById('webFcDeeplink');
    if (deeplink) deeplink.style.display = 'none';

    try {
        const { createAppClient, viemConnector } = await import('https://esm.sh/@farcaster/auth-client@0.3.0');
        const client = createAppClient({ relay: 'https://relay.farcaster.xyz', ethereum: viemConnector() });

        const nonce = Math.random().toString(36).slice(2, 18);
        const { data: channelData, isError: chanErr } = await client.createChannel({
            siweUri: 'https://pmfi.cc',
            domain: 'pmfi.cc',
            nonce,
        });
        if (chanErr || !channelData) { webSetSiwfStatus('Failed to create sign-in channel. Try again.'); return; }

        const { url, channelToken } = channelData;

        const isMobile = /iPhone|iPad|Android/i.test(navigator.userAgent);
        if (isMobile) {
            if (deeplink) { deeplink.href = url; deeplink.style.display = 'block'; }
            webSetSiwfStatus('Tap the button below to open Warpcast');
        } else {
            if (qrWrap) {
                qrWrap.style.display = 'block';
                qrWrap.innerHTML = '<div id="webFcQrCanvas"></div><p style="font-size:12px;color:rgba(255,255,255,0.35);margin-top:8px;">Scan with Warpcast</p>';
                new QRCode(document.getElementById('webFcQrCanvas'), {
                    text: url,
                    width: 220,
                    height: 220,
                    colorDark: '#000000',
                    colorLight: '#ffffff',
                    correctLevel: QRCode.CorrectLevel.M,
                });
            }
            webSetSiwfStatus('Scan the QR code with Warpcast to sign in');
        }

        let aborted = false;
        _siwfAbort = () => { aborted = true; };

        const timeout = setTimeout(() => { if (!aborted) { aborted = true; webSetSiwfStatus('Timed out — please try again.'); } }, 5 * 60 * 1000);

        const { data: statusData, isError: statusErr } = await client.watchStatus({
            channelToken,
            timeout: 5 * 60 * 1000,
            interval: 2000,
            onResponse: ({ data }) => {
                if (aborted) return;
                if (data?.state === 'pending') webSetSiwfStatus('Waiting for approval in Warpcast...');
            },
        });

        clearTimeout(timeout);
        if (aborted) return;

        if (statusErr || !statusData?.fid) {
            webSetSiwfStatus('Sign-in was not completed. Try again.');
            return;
        }

        webFcFid = statusData.fid;
        webFcUsername = statusData.username || null;
        localStorage.setItem('pmfi_web_fid', webFcFid);
        if (webFcUsername) localStorage.setItem('pmfi_web_fc_username', webFcUsername);
        webCloseSiwfModal();
        webUpdateFcState();
        if (_siwfCallback) { const cb = _siwfCallback; _siwfCallback = null; cb(); }

    } catch (e) {
        console.error('[SIWF]', e);
        webSetSiwfStatus('Error: ' + (e.message || 'Something went wrong'));
    }
}

async function webVerifyFollow() {
    if (!webFcFid) return;
    const btn = document.getElementById('webVerifyFollowBtn');
    if (btn) { btn.textContent = 'Checking...'; btn.disabled = true; }
    try {
        const res = await fetch('/api/tasks/verify/follow_fc', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ fid: webFcFid })
        });
        const data = await res.json();
        if (data.verified) {
            if (btn) { btn.textContent = 'Verified ✓'; btn.style.background = 'rgba(126,231,135,0.2)'; btn.style.color = '#7ee787'; }
            return;
        } else if (data.error === 'User not registered') {
            alert('Your Farcaster account is not registered with PMFI yet. Open the PMFI mini app on Warpcast first to create your account.');
        } else {
            alert(data.reason || data.error || 'You are not following PMFI on Farcaster yet. Follow @pmfi first, then verify.');
        }
    } catch (e) {
        alert('Verification failed — check your connection and try again.');
    } finally {
        if (btn) { btn.textContent = 'Verify'; btn.disabled = false; }
    }
}

// =============================================================================
// ARBITRAGE
// =============================================================================

function _toUrlSlug(str) {
    if (!str) return '';
    return String(str)
        .normalize('NFD').replace(/[\u0300-\u036f]/g, '') // strip diacritics
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '');
}

function _oddpoolToCard(op) {
    const venue2 = op.venue2 || 'kalshi';
    const kalshiSide = op.kalshi_side || 'YES';
    const polyPrice = op.poly_yes_ask || 0;
    const otherPrice = op.kalshi_yes_ask || 0;
    const title = op.poly_title || op.pair_id || 'Unknown';
    const outcome = op.kalshi_title || '';   // specific outcome label (e.g. "25bps cut", "Finland")
    const edgePct = op.gross_edge_pct || 0;
    const kalshiTicker = op.kalshi_ticker || '';

    // Polymarket: use event slug directly if available, else search by title
    const polySlug = op.poly_yes_token || '';
    const polyUrl = polySlug
        ? 'https://polymarket.com/event/' + polySlug
        : 'https://polymarket.com/markets?_q=' + encodeURIComponent(title + (outcome ? ' ' + outcome : ''));

    // Opinion Labs: correct domain is app.opinion.trade with slug-based URLs
    // Slug format: {event-title-slug}-{outcome-slug} e.g. "next-president-of-vietnam-to-lam"
    let otherUrl = '';
    if (venue2 === 'opinion') {
        const slug = _toUrlSlug(title) + (outcome ? '-' + _toUrlSlug(outcome) : '');
        otherUrl = 'https://app.opinion.trade/market/' + (slug || op.opinion_market_id || '');
    } else if (kalshiTicker) {
        otherUrl = 'https://kalshi.com/markets/' + kalshiTicker.split('-')[0].toLowerCase() + '#' + kalshiTicker.toLowerCase();
    }

    return {
        pairId: op.pair_id || '',
        title,
        outcome,
        edge: edgePct / 100,
        roi: edgePct,
        expiryTs: op.expiry_ts || 0,
        legs: [
            { venue: 'polymarket', price: polyPrice, side: 'YES' },
            { venue: venue2, price: otherPrice, side: kalshiSide },
        ],
        polyUrl,
        kalshiUrl: venue2 === 'kalshi' ? otherUrl : '',
        opinionUrl: venue2 === 'opinion' ? otherUrl : '',
        source: 'oddpool',
        type: 'opportunity',
        _type: 'opportunity',
    };
}

async function webLoadArb() {
    if (arbRefreshTimer) { clearTimeout(arbRefreshTimer); arbRefreshTimer = null; }
    const el = document.getElementById('webArbOpportunities');
    const statusEl = document.getElementById('webArbStatus');
    if (!el) return;
    try {
        const [oddRes, legacyRes] = await Promise.allSettled([
            fetch('/api/arb-vault/opportunities?limit=200'),
            fetch('/api/arbs?sort=expiry&limit=50'),
        ]);

        let allOpps = [];
        let updatedAt = 0;
        let count = 0;

        let scannerError = null;

        if (oddRes.status === 'fulfilled') {
            if (!oddRes.value.ok) {
                scannerError = 'Scanner offline (' + oddRes.value.status + ')';
            } else {
                const d = await oddRes.value.json();
                if (d.error) {
                    scannerError = d.error;
                } else {
                    allOpps = (d.opportunities || []).map(op => _oddpoolToCard(op));
                    updatedAt = d.updated_at || 0;
                    count = d.count || allOpps.length;
                }
            }
        } else {
            scannerError = 'Scanner offline';
        }

        const seenIds = new Set(allOpps.map(o => o.pairId));
        if (legacyRes.status === 'fulfilled' && legacyRes.value.ok) {
            const d2 = await legacyRes.value.json();
            if (!d2.error) {
                const legacyOpps = (d2.opportunities || []).map(o => ({ ...o, _type: 'opportunity' }));
                const legacyWatch = (d2.watchlist || []).map(o => ({ ...o, _type: 'watchlist' }));
                for (const o of [...legacyOpps, ...legacyWatch]) {
                    if (!seenIds.has(o.pairId)) { allOpps.push(o); seenIds.add(o.pairId); }
                }
                if (!updatedAt && d2.asOf) updatedAt = d2.asOf;
                if (scannerError && allOpps.length > 0) scannerError = null;
            }
        }

        allOpps.sort((a, b) => (a.expiryTs || 0) - (b.expiryTs || 0));

        if (statusEl) {
            if (scannerError && allOpps.length === 0) {
                statusEl.innerHTML = '<span class="arb-dot error"></span>\u26a0\ufe0f ' + scannerError;
            } else {
                const ago = updatedAt ? Math.round(Date.now() / 1000 - updatedAt) : null;
                const agoText = ago !== null ? (ago < 60 ? ago + 's ago' : Math.round(ago / 60) + 'm ago') : '';
                const countText = allOpps.length + ' opportunit' + (allOpps.length === 1 ? 'y' : 'ies');
                statusEl.innerHTML = '<span class="arb-dot live"></span>Live \u00b7 ' + countText +
                    (agoText ? ' \u00b7 Updated ' + agoText : '');
            }
        }

        el.innerHTML = allOpps.length
            ? allOpps.map(o => webRenderArbCard(o)).join('')
            : '<div class="arb-empty">No arbitrage opportunities right now.<br>Scanner checks all platforms every 60s.</div>';

        arbRefreshTimer = setTimeout(() => {
            if (webActiveTab === 'arbitrage') webLoadArb();
        }, 30000);
    } catch (e) {
        if (statusEl) statusEl.innerHTML = '<span class="arb-dot error"></span>Scanner unavailable';
        el.innerHTML = '<div class="arb-empty">Could not load arbitrage data: ' + e.message + '</div>';
        arbRefreshTimer = setTimeout(() => {
            if (webActiveTab === 'arbitrage') webLoadArb();
        }, 60000);
    }
}

function webRenderArbCard(item) {
    const isOpp = item._type === 'opportunity';
    const edgePct = ((item.edge || 0) * 100).toFixed(1);
    const roiPct = (item.roi || 0).toFixed(1);
    const badge = isOpp
        ? `<span class="arb-badge opportunity">${edgePct}%</span>`
        : `<span class="arb-badge watchlist">${edgePct}%</span>`;

    const legs = item.legs || [];
    const polyLeg = legs.find(l => l.venue === 'polymarket');
    const opinionLeg = legs.find(l => l.venue === 'opinion');
    const kalshiLeg = legs.find(l => l.venue === 'kalshi');
    const otherLeg = kalshiLeg || opinionLeg;
    const polyPrice = polyLeg ? (polyLeg.price * 100).toFixed(0) : '?';
    const otherPrice = otherLeg ? (otherLeg.price * 100).toFixed(0) : '?';
    const polySide = polyLeg ? polyLeg.side : '';
    const otherSide = otherLeg ? otherLeg.side : '';
    const otherVenueName = opinionLeg ? 'OPINION' : 'KALSHI';

    let polyUrl = item.polyUrl || '';
    let otherUrl = opinionLeg ? (item.opinionUrl || '') : (item.kalshiUrl || '');
    if (!polyUrl && item.title) polyUrl = 'https://polymarket.com/markets?_q=' + encodeURIComponent(item.title);

    const expiry = item.expiryTs ? new Date(item.expiryTs * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : null;

    const polyVenue = `<${polyUrl ? `a href="${polyUrl}" target="_blank"` : 'div'} class="arb-venue-box">
        <div class="arb-venue-name">POLY ${polySide}</div>
        <div class="arb-venue-price">${polyPrice}\u00a2</div>
    </${polyUrl ? 'a' : 'div'}>`;

    const otherVenue = `<${otherUrl ? `a href="${otherUrl}" target="_blank"` : 'div'} class="arb-venue-box">
        <div class="arb-venue-name">${otherVenueName} ${otherSide}</div>
        <div class="arb-venue-price">${otherPrice}\u00a2</div>
    </${otherUrl ? 'a' : 'div'}>`;

    const outcomeHtml = item.outcome
        ? `<div class="arb-card-outcome">${item.outcome}</div>`
        : '';

    return `<div class="arb-card">
        <div class="arb-card-header">
            <div class="arb-card-title">${item.title || 'Unknown'}</div>
            ${badge}
        </div>
        ${outcomeHtml}
        <div class="arb-card-prices">${polyVenue}${otherVenue}</div>
        <div class="arb-card-meta">
            ${expiry ? `<span>${expiry}</span>` : '<span></span>'}
            <span>ROI ${roiPct}%</span>
        </div>
    </div>`;
}

// =============================================================================
// LEADERBOARD
// =============================================================================

async function webLoadLeaderboard() {
    const el = document.getElementById('webLeaderboardContent');
    if (!el) return;
    el.innerHTML = '<div class="leaderboard-empty">Loading...</div>';
    try {
        const res = await fetch('/api/leaderboard?scope=all&limit=50');
        if (!res.ok) throw new Error('API error');
        const data = await res.json();
        const rows = data.leaderboard || data.rows || [];
        if (!rows.length) { el.innerHTML = '<div class="leaderboard-empty">No data yet</div>'; return; }
        el.innerHTML = rows.map((row, i) => {
            const isCurrent = userAddress && row.wallet && row.wallet.toLowerCase() === userAddress.toLowerCase();
            const medal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : (i + 1);
            const displayName = row.username ? '@' + row.username : (row.wallet ? row.wallet.slice(0, 6) + '...' + row.wallet.slice(-4) : 'FID ' + row.fid);
            return `<div class="leaderboard-row${isCurrent ? ' current-user' : ''}">
                <span class="lb-rank">${medal}</span>
                <span class="lb-user">${displayName}</span>
                <span class="lb-xp">${(row.xp_total || row.xp || 0).toLocaleString()} XP</span>
            </div>`;
        }).join('');
    } catch (e) {
        el.innerHTML = '<div class="leaderboard-empty">Could not load leaderboard</div>';
    }
}

// =============================================================================
// TASKS
// =============================================================================

const WEB_TASK_DEFINITIONS = [
    { id: 'follow_fc', label: 'Follow PMFI on Farcaster', xp: 100, url: 'https://warpcast.com/pmfi' },
    { id: 'deposit_10', label: 'Deposit $10+ USDC into pSNIPER vault', xp: 500, url: null },
    { id: 'invite', label: 'Invite a friend with your code', xp: 250, url: null },
    { id: 'follow_x', label: 'Follow PMFI on X', xp: 100, url: 'https://x.com/pmfi_cc' },
];

function webRenderTaskRow(task, serverTask) {
    const el = document.getElementById('webTasksList');
    const status = serverTask ? serverTask.status : null;
    const locked = serverTask ? serverTask.locked : false;
    let actionsHtml = '';

    if (status === 'COMPLETED') {
        actionsHtml = `<span style="color:#7ee787;font-size:13px;font-weight:600;">Done ✓</span>`;
    } else if (status === 'PENDING_REVIEW') {
        actionsHtml = `<span style="color:#f59e0b;font-size:13px;font-weight:600;">Pending ⏳</span>`;
    } else if (status === 'LOCKED' || locked) {
        actionsHtml = `<span style="color:rgba(255,255,255,0.3);font-size:13px;font-weight:600;">🔒 Locked</span>`;
    } else if (task.id === 'follow_fc') {
        if (webFcFid) {
            const name = webFcUsername ? '@' + webFcUsername : 'FID ' + webFcFid;
            actionsHtml = `<div style="display:flex;align-items:center;gap:6px;">
                <button id="webVerifyFollowBtn" class="task-btn" onclick="webVerifyFollow()">Verify</button>
                <button onclick="webDisconnectFc()" style="background:none;border:none;color:rgba(255,255,255,0.3);cursor:pointer;font-size:11px;padding:2px 4px;" title="${name}">✕</button>
            </div>`;
        } else {
            actionsHtml = `<button class="task-btn" onclick="webFollowFarcaster()">Follow</button>`;
        }
    } else if (task.id === 'deposit_10') {
        if (webFcFid) {
            actionsHtml = `<button class="task-btn" onclick="webVerifyDeposit10(this)">Verify</button>`;
        }
    } else if (task.id === 'invite') {
        if (webFcFid) {
            actionsHtml = `<button class="task-btn" onclick="webVerifyInvite(this)">Verify</button>`;
        }
    } else if (task.id === 'follow_x') {
        if (webFcFid) {
            actionsHtml = `<button class="task-btn" id="webClaimXBtn" onclick="webClaimFollowX(this)" style="background:#f59e0b;color:#000;">Claim</button>`;
        } else {
            actionsHtml = `<a href="${task.url}" target="_blank"><button class="task-btn">Go</button></a>`;
        }
    } else if (task.url) {
        actionsHtml = `<a href="${task.url}" target="_blank"><button class="task-btn">Go</button></a>`;
    }

    return `<div class="task-row">
        <div class="task-info">
            <div class="task-name">${task.label}</div>
            <div class="task-xp">+${task.xp} XP</div>
        </div>
        ${actionsHtml}
    </div>`;
}

function webLoadTasks() {
    const el = document.getElementById('webTasksList');
    if (!el) return;

    const footer = `<div style="margin-top:12px;font-size:12px;color:rgba(255,255,255,0.35);">Open the PMFI mini app on Farcaster to track your XP progress.</div>`;

    if (!webFcFid) {
        el.innerHTML = WEB_TASK_DEFINITIONS.map(task => webRenderTaskRow(task, null)).join('') + footer;
        return;
    }

    el.innerHTML = `<div style="color:rgba(255,255,255,0.35);font-size:13px;padding:12px 0;">Loading tasks...</div>`;

    fetch(`/api/state?fid=${webFcFid}`)
        .then(r => r.json())
        .then(data => {
            const taskMap = {};
            if (data.tasks && Array.isArray(data.tasks)) {
                data.tasks.forEach(t => { taskMap[t.id] = t; });
            }
            el.innerHTML = WEB_TASK_DEFINITIONS.map(task => webRenderTaskRow(task, taskMap[task.id] || null)).join('') + footer;
        })
        .catch(() => {
            el.innerHTML = WEB_TASK_DEFINITIONS.map(task => webRenderTaskRow(task, null)).join('') + footer;
        });
}

async function webClaimFollowX(btn) {
    if (!webFcFid) return;
    btn.disabled = true;
    btn.textContent = 'Claiming...';
    window.open('https://x.com/pmfi_cc', '_blank');
    try {
        const res = await fetch('/api/tasks/claim/follow_x', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ fid: webFcFid })
        });
        const data = await res.json();
        if (data.claimed || data.success) {
            btn.textContent = 'Pending';
            btn.style.background = 'rgba(245,158,11,0.2)';
            btn.style.color = '#f59e0b';
            btn.disabled = true;
        } else if (data.locked) {
            btn.textContent = 'Locked';
            btn.style.background = 'rgba(255,255,255,0.06)';
            btn.style.color = 'rgba(255,255,255,0.4)';
            btn.disabled = true;
            alert(data.reason || 'Complete other tasks first');
        } else {
            btn.textContent = 'Claim';
            btn.style.background = '#f59e0b';
            btn.style.color = '#000';
            btn.disabled = false;
            if (data.reason) alert(data.reason);
        }
    } catch (e) {
        btn.textContent = 'Claim';
        btn.disabled = false;
    }
}

async function webVerifyDeposit10(btn) {
    if (!webFcFid) return;
    btn.disabled = true;
    btn.textContent = 'Checking...';
    try {
        const res = await fetch('/api/tasks/verify/deposit_10', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ fid: webFcFid, wallet: userAddress || null })
        });
        const data = await res.json();
        if (data.verified) {
            btn.textContent = 'Done ✓';
            btn.style.background = 'rgba(126,231,135,0.15)';
            btn.style.color = '#7ee787';
            btn.disabled = true;
            setTimeout(() => webLoadTasks(), 800);
        } else {
            btn.textContent = 'Verify';
            btn.disabled = false;
            alert(data.reason || data.error || 'No deposit of $10+ found yet. Make a deposit first.');
        }
    } catch (e) {
        btn.textContent = 'Verify';
        btn.disabled = false;
        alert('Network error. Please try again.');
    }
}

async function webVerifyInvite(btn) {
    if (!webFcFid) return;
    btn.disabled = true;
    btn.textContent = 'Checking...';
    try {
        const res = await fetch('/api/tasks/verify/invite', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ fid: webFcFid })
        });
        const data = await res.json();
        if (data.verified) {
            btn.textContent = 'Done ✓';
            btn.style.background = 'rgba(126,231,135,0.15)';
            btn.style.color = '#7ee787';
            btn.disabled = true;
            setTimeout(() => webLoadTasks(), 800);
        } else {
            btn.textContent = 'Verify';
            btn.disabled = false;
            const msg = data.reason || data.error || 'No qualified referral found yet.';
            alert(msg);
        }
    } catch (e) {
        btn.textContent = 'Verify';
        btn.disabled = false;
        alert('Network error. Please try again.');
    }
}

function webRenderTask(task) {
    const done = task.completed || task.status === 'completed';
    const pending = task.status === 'PENDING_REVIEW';
    let statusHtml = '';
    let btnHtml = '';
    if (done) {
        statusHtml = '<span class="task-status done">Done</span>';
    } else if (pending) {
        statusHtml = '<span class="task-status pending">Pending Review</span>';
    } else if (task.action_url) {
        btnHtml = `<a href="${task.action_url}" target="_blank"><button class="task-btn">Go</button></a>`;
    }
    return `<div class="task-row">
        <div class="task-info">
            <div class="task-name">${task.label || task.type || 'Task'}</div>
            <div class="task-xp">+${task.xp || 0} XP</div>
        </div>
        ${statusHtml}${btnHtml}
    </div>`;
}

// =============================================================================
// INVITE CODES (web)
// =============================================================================

async function webLoadInviteCodes() {
    const el = document.getElementById('webInviteCodesList');
    if (!el) return;
    if (!userAddress) {
        el.innerHTML = '<div style="color:rgba(255,255,255,0.4);font-size:13px;">Connect wallet to see your codes</div>';
        return;
    }
    el.innerHTML = '<div style="color:rgba(255,255,255,0.4);font-size:13px;">Loading codes...</div>';
    try {
        const res = await fetch('/api/invite/my-codes?wallet=' + userAddress);
        if (!res.ok) throw new Error('API error');
        const data = await res.json();
        const codes = data.codes || [];
        if (!codes.length) {
            el.innerHTML = '<div style="color:rgba(255,255,255,0.4);font-size:13px;">No codes yet</div>';
            return;
        }
        el.innerHTML = codes.map((c, i) => `
            <div class="invite-code-row" id="webCodeRow${i}">
                <span class="invite-code-text">${c.code}</span>
                <span class="invite-code-status ${c.used ? 'used' : 'available'}">${c.used ? 'Used' : 'Available'}</span>
                ${!c.used ? `<button class="invite-copy-btn" onclick="webCopyCode('${c.code}', ${i})">Copy</button>` : ''}
            </div>
        `).join('');
    } catch (e) {
        el.innerHTML = '<div style="color:rgba(255,255,255,0.4);font-size:13px;">Could not load codes</div>';
    }
}

function webCopyCode(code, idx) {
    navigator.clipboard.writeText(code).then(() => {
        const btn = document.querySelector('#webCodeRow' + idx + ' .invite-copy-btn');
        if (btn) { btn.textContent = 'Copied!'; setTimeout(() => { btn.textContent = 'Copy'; }, 2000); }
    }).catch(() => {
        const el = document.createElement('textarea');
        el.value = code; document.body.appendChild(el); el.select(); document.execCommand('copy'); document.body.removeChild(el);
    });
}

// =============================================================================
// INIT
// =============================================================================

(async function init() {
    initDisclaimer();
    initDepositModal();
    initArbModals();
    
    if (switchNetworkBtn) {
        switchNetworkBtn.addEventListener("click", switchToBase);
    }
    
    const loaded = await loadABIs();
    if (loaded) {
        abisLoaded = true;
        
        await initReadOnlyProvider();

        // Load pARB vault stats immediately (no wallet needed — read-only)
        loadArbVaultStats();
        
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
    
    // Read hash to determine initial tab, default to home
    const hashTab = window.location.hash.replace('#', '');
    if (WEB_TABS.includes(hashTab)) {
        webSwitchTab(hashTab);
    } else {
        webSwitchTab('home');
    }
    
    // Log price API configuration
    if (PRICE_API_URL) {
        console.log("Price API URL configured:", PRICE_API_URL);
    } else {
        console.log("No Price API URL configured. Set via localStorage: localStorage.setItem('predictfi_price_api_url', 'http://your-vps:8080')");
    }
})();

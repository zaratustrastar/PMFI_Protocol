/**
 * PredictFi pSNIPER V5 - Frontend
 * 
 * Key V5 features:
 * - 90/10 auto-split (90% goes to Polymarket, 10% buffer)
 * - Async withdrawals: requestWithdraw → wait → claim
 * - Pending withdrawal status tracking
 */

// =============================================================================
// CONFIGURATION
// =============================================================================

const VAULT_ADDRESS = ""; // TODO: Set after V5 deployment
const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const USDC_DECIMALS = 6;
const REFRESH_INTERVAL = 30000;
const BASE_MAINNET_RPC = "https://mainnet.base.org";
const BASE_MAINNET_CHAIN_ID = 8453;

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
            fetch("abis/vault-v5.json"),
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
let userPendingWithdrawals = [];

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
const pendingWithdrawalsEl = document.getElementById("pendingWithdrawals");

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

function formatTimeRemaining(seconds) {
    if (seconds <= 0) return "Expired";
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    if (days > 0) return `${days}d ${hours}h`;
    const mins = Math.floor((seconds % 3600) / 60);
    return `${hours}h ${mins}m`;
}

// =============================================================================
// SIGNED NAV DATA
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
            details: data.details,
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
        const response = await fetch(`${PRICE_API_URL}/nav`);
        if (!response.ok) return null;
        return await response.json();
    } catch (error) {
        console.error("Error fetching price:", error);
        return null;
    }
}

async function updatePriceDisplay() {
    const data = await fetchPriceFromAPI();
    if (!data) return;
    
    lastPriceData = data;
    
    if (statsSharePriceEl) {
        const nav = Number(data.nav) / 1e18;
        statsSharePriceEl.textContent = `$${nav.toFixed(4)}`;
    }
}

// =============================================================================
// WALLET CONNECTION
// =============================================================================

async function connectWallet() {
    if (typeof window.ethereum === "undefined") {
        alert("Please install MetaMask to use this app");
        return;
    }
    
    try {
        provider = new ethers.BrowserProvider(window.ethereum);
        await provider.send("eth_requestAccounts", []);
        signer = await provider.getSigner();
        userAddress = await signer.getAddress();
        
        const network = await provider.getNetwork();
        if (Number(network.chainId) !== BASE_MAINNET_CHAIN_ID) {
            if (networkWarning) networkWarning.classList.remove("hidden");
            return;
        }
        
        if (networkWarning) networkWarning.classList.add("hidden");
        
        if (!abisLoaded) {
            abisLoaded = await loadABIs();
            if (!abisLoaded) {
                throw new Error("Failed to load ABIs");
            }
        }
        
        vaultContract = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signer);
        usdcContract = new ethers.Contract(USDC_ADDRESS, USDC_ABI, signer);
        
        isConnected = true;
        updateUI();
        startRefresh();
        
        if (connectBtn) {
            connectBtn.textContent = `${userAddress.slice(0, 6)}...${userAddress.slice(-4)}`;
        }
        
    } catch (error) {
        console.error("Connection error:", error);
        alert("Failed to connect: " + error.message);
    }
}

async function switchNetwork() {
    try {
        await window.ethereum.request({
            method: "wallet_switchEthereumChain",
            params: [{ chainId: "0x2105" }],
        });
        connectWallet();
    } catch (error) {
        if (error.code === 4902) {
            try {
                await window.ethereum.request({
                    method: "wallet_addEthereumChain",
                    params: [{
                        chainId: "0x2105",
                        chainName: "Base",
                        nativeCurrency: { name: "ETH", symbol: "ETH", decimals: 18 },
                        rpcUrls: [BASE_MAINNET_RPC],
                        blockExplorerUrls: ["https://basescan.org"],
                    }],
                });
                connectWallet();
            } catch (addError) {
                console.error("Failed to add network:", addError);
            }
        }
    }
}

// =============================================================================
// UI UPDATE
// =============================================================================

async function updateUI() {
    if (!isConnected || !vaultContract) return;
    
    try {
        const state = await vaultContract.getVaultState();
        const lastNav = state[0];
        const totalSupply = state[3];
        const vaultBalance = state[4];
        const totalPendingShares = state[6];
        const pendingCount = state[7];
        const depositsThrottled = state[9];
        const targetBuffer = state[10];
        
        if (tvlValueEl) {
            tvlValueEl.textContent = formatUSDC(vaultBalance);
        }
        
        if (statsSharePriceEl) {
            const nav = Number(lastNav) / 1e18;
            statsSharePriceEl.textContent = `$${nav.toFixed(4)}`;
        }
        
        if (userAddress) {
            const userShares = await vaultContract.balanceOf(userAddress);
            const userValue = (BigInt(userShares) * BigInt(lastNav)) / BigInt(1e18);
            
            if (sharesBalanceEl) {
                sharesBalanceEl.textContent = formatShares(userShares);
            }
            if (positionValueEl) {
                positionValueEl.textContent = formatUSDC(userValue);
            }
            
            await updatePendingWithdrawals();
        }
        
        if (depositsThrottled && depositBtn) {
            depositBtn.disabled = true;
            depositBtn.textContent = "Deposits Paused";
        }
        
    } catch (error) {
        console.error("Error updating UI:", error);
    }
}

// =============================================================================
// PENDING WITHDRAWALS
// =============================================================================

async function updatePendingWithdrawals() {
    if (!vaultContract || !userAddress) return;
    
    try {
        const requestIds = await vaultContract.getUserWithdrawals(userAddress);
        userPendingWithdrawals = [];
        
        for (const id of requestIds) {
            const req = await vaultContract.getWithdrawalRequest(id);
            if (!req[3] && !req[4]) {
                userPendingWithdrawals.push({
                    id: Number(id),
                    shares: req[1],
                    requestTime: Number(req[2]),
                    expired: req[4],
                });
            }
        }
        
        renderPendingWithdrawals();
        
    } catch (error) {
        console.error("Error fetching pending withdrawals:", error);
    }
}

function renderPendingWithdrawals() {
    if (!pendingWithdrawalsEl) return;
    
    if (userPendingWithdrawals.length === 0) {
        pendingWithdrawalsEl.innerHTML = "";
        pendingWithdrawalsEl.classList.add("hidden");
        return;
    }
    
    pendingWithdrawalsEl.classList.remove("hidden");
    
    let html = `<h3>Pending Withdrawals</h3>`;
    
    for (const req of userPendingWithdrawals) {
        const expiresIn = (req.requestTime + 7 * 24 * 3600) - Math.floor(Date.now() / 1000);
        
        html += `
            <div class="withdrawal-request">
                <div class="request-info">
                    <span>Request #${req.id}</span>
                    <span>${formatShares(req.shares)} shares</span>
                    <span>Expires: ${formatTimeRemaining(expiresIn)}</span>
                </div>
                <button class="claim-btn" onclick="claimWithdrawal(${req.id})">
                    Claim
                </button>
            </div>
        `;
    }
    
    pendingWithdrawalsEl.innerHTML = html;
}

// =============================================================================
// DEPOSIT
// =============================================================================

async function deposit() {
    if (!isConnected || !vaultContract || !usdcContract) {
        alert("Please connect your wallet first");
        return;
    }
    
    const amount = depositAmountEl?.value;
    if (!amount || parseFloat(amount) <= 0) {
        showStatus(txStatus, "Please enter a valid amount", "error");
        return;
    }
    
    try {
        showStatus(txStatus, "Getting signed NAV...", "info");
        const { navData, signature } = await getSignedNavData();
        
        const usdcAmount = parseUSDC(amount);
        
        showStatus(txStatus, "Checking USDC allowance...", "info");
        const allowance = await usdcContract.allowance(userAddress, VAULT_ADDRESS);
        
        if (allowance < usdcAmount) {
            showStatus(txStatus, "Approving USDC...", "info");
            const approveTx = await usdcContract.approve(VAULT_ADDRESS, ethers.MaxUint256);
            await approveTx.wait();
        }
        
        showStatus(txStatus, "Depositing...", "info");
        const tx = await vaultContract.deposit(
            usdcAmount,
            [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
            signature
        );
        
        showStatus(txStatus, "Waiting for confirmation...", "info");
        await tx.wait();
        
        showStatus(txStatus, "Deposit successful! (90% sent to Polymarket, 10% in buffer)", "success");
        
        if (depositAmountEl) depositAmountEl.value = "";
        updateUI();
        
    } catch (error) {
        console.error("Deposit error:", error);
        showStatus(txStatus, "Deposit failed: " + (error.reason || error.message), "error");
    }
}

// =============================================================================
// REQUEST WITHDRAWAL (V5 - ASYNC)
// =============================================================================

async function requestWithdrawal() {
    if (!isConnected || !vaultContract) {
        alert("Please connect your wallet first");
        return;
    }
    
    const amount = withdrawAmountEl?.value;
    if (!amount || parseFloat(amount) <= 0) {
        showStatus(txStatus, "Please enter a valid amount", "error");
        return;
    }
    
    try {
        showStatus(txStatus, "Getting signed NAV...", "info");
        const { navData, signature } = await getSignedNavData();
        
        const shareAmount = ethers.parseEther(amount);
        
        showStatus(txStatus, "Requesting withdrawal...", "info");
        const tx = await vaultContract.requestWithdraw(
            shareAmount,
            [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
            signature
        );
        
        showStatus(txStatus, "Waiting for confirmation...", "info");
        const receipt = await tx.wait();
        
        const event = receipt.logs.find(log => {
            try {
                const parsed = vaultContract.interface.parseLog(log);
                return parsed?.name === "WithdrawalRequested";
            } catch { return false; }
        });
        
        let requestId = "?";
        if (event) {
            const parsed = vaultContract.interface.parseLog(event);
            requestId = parsed.args[0].toString();
        }
        
        showStatus(txStatus, `Withdrawal requested! Request #${requestId}. Come back to claim once buffer is refilled.`, "success");
        
        if (withdrawAmountEl) withdrawAmountEl.value = "";
        updateUI();
        
    } catch (error) {
        console.error("Withdrawal request error:", error);
        showStatus(txStatus, "Request failed: " + (error.reason || error.message), "error");
    }
}

// =============================================================================
// CLAIM WITHDRAWAL
// =============================================================================

async function claimWithdrawal(requestId) {
    if (!isConnected || !vaultContract) {
        alert("Please connect your wallet first");
        return;
    }
    
    try {
        showStatus(txStatus, "Getting fresh signed NAV...", "info");
        const { navData, signature } = await getSignedNavData();
        
        showStatus(txStatus, "Claiming withdrawal...", "info");
        const tx = await vaultContract.claim(
            requestId,
            [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
            signature
        );
        
        showStatus(txStatus, "Waiting for confirmation...", "info");
        await tx.wait();
        
        showStatus(txStatus, "Withdrawal claimed successfully! (1% tax deducted)", "success");
        updateUI();
        
    } catch (error) {
        console.error("Claim error:", error);
        
        if (error.message?.includes("Insufficient buffer")) {
            showStatus(txStatus, "Buffer not yet refilled. Please wait for protocol to add liquidity.", "error");
        } else {
            showStatus(txStatus, "Claim failed: " + (error.reason || error.message), "error");
        }
    }
}

// =============================================================================
// REFRESH
// =============================================================================

function startRefresh() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(updateUI, REFRESH_INTERVAL);
    
    if (priceRefreshTimer) clearInterval(priceRefreshTimer);
    priceRefreshTimer = setInterval(updatePriceDisplay, PRICE_REFRESH_INTERVAL);
    
    updateUI();
    updatePriceDisplay();
}

// =============================================================================
// SETTINGS
// =============================================================================

function openSettings() {
    const currentUrl = localStorage.getItem("predictfi_price_api_url") || "";
    const newUrl = prompt("Enter Price API URL (e.g., http://YOUR_VPS_IP:5001):", currentUrl);
    
    if (newUrl !== null) {
        localStorage.setItem("predictfi_price_api_url", newUrl);
        location.reload();
    }
}

// =============================================================================
// DISCLAIMER
// =============================================================================

function checkDisclaimer() {
    const accepted = localStorage.getItem("predictfi_disclaimer_accepted");
    if (!accepted && disclaimerModal) {
        disclaimerModal.classList.remove("hidden");
    }
}

function acceptDisclaimer() {
    if (understandCheck?.checked) {
        if (dontShowCheck?.checked) {
            localStorage.setItem("predictfi_disclaimer_accepted", "true");
        }
        if (disclaimerModal) disclaimerModal.classList.add("hidden");
    }
}

// =============================================================================
// EVENT LISTENERS
// =============================================================================

document.addEventListener("DOMContentLoaded", () => {
    checkDisclaimer();
    
    if (connectBtn) connectBtn.addEventListener("click", connectWallet);
    if (switchNetworkBtn) switchNetworkBtn.addEventListener("click", switchNetwork);
    if (depositBtn) depositBtn.addEventListener("click", deposit);
    if (withdrawBtn) {
        withdrawBtn.textContent = "Request Withdrawal";
        withdrawBtn.addEventListener("click", requestWithdrawal);
    }
    if (acceptBtn) acceptBtn.addEventListener("click", acceptDisclaimer);
    if (understandCheck) {
        understandCheck.addEventListener("change", () => {
            if (acceptBtn) acceptBtn.disabled = !understandCheck.checked;
        });
    }
    
    if (openDepositBtn && depositModal) {
        openDepositBtn.addEventListener("click", () => depositModal.classList.remove("hidden"));
    }
    if (closeDepositModal && depositModal) {
        closeDepositModal.addEventListener("click", () => depositModal.classList.add("hidden"));
    }
    
    if (window.ethereum) {
        window.ethereum.on("accountsChanged", () => location.reload());
        window.ethereum.on("chainChanged", () => location.reload());
    }
    
    updatePriceDisplay();
});

window.claimWithdrawal = claimWithdrawal;
window.openSettings = openSettings;

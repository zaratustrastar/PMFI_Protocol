// MILORD frontend SDK (browser ESM, ethers v6). Thin wrappers over the contracts; no business logic
// beyond formatting. Import with:  import * as milord from "./milord-sdk.mjs";
import { BrowserProvider, Contract, AbiCoder, keccak256, formatEther, formatUnits, parseEther } from "https://cdn.jsdelivr.net/npm/ethers@6.13.4/+esm";

const A = {
  fundingRouter: ["function paused() view returns (bool)", "function openExecution() view returns (bool)", "function totalEthSwapped() view returns (uint256)", "function totalAssetFunded() view returns (uint256)", "function lastExecution() view returns (uint64)", "function maxSwapWei() view returns (uint256)", "function dailyCapWei() view returns (uint256)", "function maxSlippageBps() view returns (uint16)", "function spentToday() view returns (uint256)"],
  collector: ["function totalEthForwarded() view returns (uint256)", "function totalMilordBurned() view returns (uint256)"],
  familyLock: ["function lockOf(bytes32) view returns (address beneficiary, uint64 unlockBlock)"],
  token: ["function balanceOf(address) view returns (uint256)", "function totalSupply() view returns (uint256)",
    "function allowance(address,address) view returns (uint256)", "function approve(address,uint256) returns (bool)"],
  nft: ["function ownerOf(uint256) view returns (address)", "function balanceOf(address) view returns (uint256)",
    "function totalMinted() view returns (uint256)", "function activeSupply() view returns (uint256)",
    "function activationCount(uint256) view returns (uint256)", "function getVotes(address) view returns (uint256)",
    "function tokenURI(uint256) view returns (string)", "function isActive(uint256) view returns (bool)",
    "function delegates(address) view returns (address)", "function delegate(address)"],
  conv: ["function entryFeeFor(uint256) view returns (uint256)", "function feeParams() view returns (uint16 entryFeeBps, uint16 exitBurnBps, uint256 minEntryFeeWei)",
    "function entryPaused() view returns (bool)", "function backedCount() view returns (uint256)", "function inventorySize() view returns (uint256)",
    "function maxSimultaneouslyBacked() view returns (uint256)", "function totalEntryFeesWei() view returns (uint256)",
    "function totalMilordBurnedOnExit() view returns (uint256)", "function previewRedeem() view returns (uint256 released, uint256 burned)",
    "function convert((address account,uint256 weiPerMillion,uint64 issuedAt,uint64 expiresAt,bytes32 nonce) q, bytes sig, uint256 maxFee) payable returns (uint256)",
    "function redeem(uint256) returns (uint256)", "function PRINCIPAL() view returns (uint256)"],
  registry: ["function characteristicCount() view returns (uint256)", "function characteristic(uint256) view returns ((string name,bool active))",
    "function strategy(uint256) view returns ((string name,bool enabled,uint256 chainId,address vault,address rewardAsset))",
    "function resolve(uint256,uint256) view returns (uint256)", "function select(uint256,uint256,uint256) returns (uint256)",
    "function currentPreference(uint256) view returns (bool personal,uint256 charA,uint256 charB,uint256 strategyId)"],
  vault: ["function officialPPS() view returns (uint256)", "function balanceOf(address) view returns (uint256)",
    "function requestRedeem(uint256,address) returns (uint256)", "function lastReportTimestamp() view returns (uint256)"],
  dist: ["function claimed(address) view returns (uint256)", "function totalClaimed() view returns (uint256)",
    "function active() view returns (bytes32 root,uint256 totalCumulative,uint64 epoch,uint64 activatesAt,bytes32 dataHash)",
    "function claim(address,uint256,bytes32[]) returns (uint256)"],
  burner: ["function totalBurnedFromBuybacks() view returns (uint256)", "function budget() view returns (uint256)"],
  adapter: ["function totalUsdcDeposited() view returns (uint256)", "function totalSharesMinted() view returns (uint256)"],
  stateView: ["function getSlot0(bytes32) view returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee)"],
  hook: ["function buyTaxBps() view returns (uint16)", "function sellTaxBps() view returns (uint16)", "function commissionBps() view returns (uint16)",
    "function totalBuyTax() view returns (uint256)", "function totalSellTax() view returns (uint256)", "function totalPlatformPaid() view returns (uint256)",
    "function platformAccrued() view returns (uint256)", "function buyVolume() view returns (uint256)", "function sellVolume() view returns (uint256)"],
  router: ["function swap((address,address,uint24,int24,address) key, (bool zeroForOne,int256 amountSpecified,uint160 sqrtPriceLimitX96) params, (bool takeClaims,bool settleUsingBurn) s, bytes hookData) payable returns (int256)"],
  governor: ["function name() view returns (string)", "function proposalThreshold() view returns (uint256)", "function quorum(uint256) view returns (uint256)"],
};

/** Robinhood Chain parameters for wallet_addEthereumChain. Mainnet RPC is from docs.robinhood.com/chain/connecting
 *  (public, rate-limited); the testnet RPC and both explorer URLs are UNVERIFIED and must be checked before launch. */
export const ROBINHOOD_CHAINS = {
  4663: { chainId: "0x1237", chainName: "Robinhood Chain", nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
    rpcUrls: ["https://rpc.mainnet.chain.robinhood.com"], blockExplorerUrls: ["https://robinhoodchain.blockscout.com"] },
  46630: { chainId: "0xb626", chainName: "Robinhood Chain Testnet", nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
    rpcUrls: ["https://rpc.testnet.chain.robinhood.com"], blockExplorerUrls: ["https://explorer.testnet.chain.robinhood.com"] },
};

/** Switch the wallet to the deployment's chain, adding Robinhood Chain if the wallet does not know it. */
export async function switchNetwork(chainId, ethereum = window.ethereum) {
  const hex = "0x" + Number(chainId).toString(16);
  try { await ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: hex }] }); }
  catch (e) {
    if (e.code !== 4902 || !ROBINHOOD_CHAINS[chainId]) throw e;
    await ethereum.request({ method: "wallet_addEthereumChain", params: [ROBINHOOD_CHAINS[chainId]] });
  }
}

export async function connect(deployments, ethereum = window.ethereum) {
  const provider = new BrowserProvider(ethereum);
  const signer = await provider.getSigner();
  const c = (addr, abi) => new Contract(addr, abi, signer);
  const D = deployments;
  return {
    provider, signer, account: await signer.getAddress(), D,
    token: c(D.MILORD, A.token), nft: c(D.Milords, A.nft), conv: c(D.MilordConverter, A.conv),
    registry: c(D.StrategyRegistry, A.registry),
    // pARB vault and distributor live on the same chain (Robinhood Chain): one provider.
    vault: c(D.pARBVault, A.vault), dist: c(D.ShareDistributor, A.dist), adapter: c(D.ParbFundingAdapter, A.adapter),
    burner: c(D.BuybackBurner, A.burner),
    // MILORD/ETH market: price from StateView; tax figures from the pool's tax hook (Hookr's on Robinhood Chain)
    stateView: D.StateView ? c(D.StateView, A.stateView) : null, hook: D.HookrTaxHook ? c(D.HookrTaxHook, A.hook) : null,
    router: D.SwapRouter ? c(D.SwapRouter, A.router) : null, // local only: stand-in for trading on Hookr
    governor: D.MilordGovernor ? c(D.MilordGovernor, A.governor) : null,
    key: D.HookrTaxHook ? ["0x0000000000000000000000000000000000000000", D.MILORD, D.lpFee, D.tickSpacing, D.HookrTaxHook] : null,
    fundingRouter: D.ArbitrageFundingRouter ? c(D.ArbitrageFundingRouter, A.fundingRouter) : null,
    collector: D.LockFeeCollector ? c(D.LockFeeCollector, A.collector) : null,
    familyLock: D.HookrFamilyLock ? c(D.HookrFamilyLock, A.familyLock) : null,
  };
}

/** Automation and lock status: ETH -> USDG router, LP fee collector, founding-position lock. Read-only. */
export async function automation(m) {
  const out = {};
  if (m.fundingRouter) {
    const r = m.fundingRouter;
    const [paused, open, held, swapped, funded, last, maxSwap, daily, slip, spent] = await Promise.all([
      r.paused(), r.openExecution(), m.provider.getBalance(r.target), r.totalEthSwapped(), r.totalAssetFunded(),
      r.lastExecution(), r.maxSwapWei(), r.dailyCapWei(), r.maxSlippageBps(), r.spentToday()]);
    out.router = { paused, open, ethWaiting: formatEther(held), ethSwapped: formatEther(swapped), usdgFunded: formatUnits(funded, 6),
      lastExecution: Number(last), maxSwapEth: formatEther(maxSwap), dailyCapEth: formatEther(daily), maxSlippagePct: Number(slip) / 100, spentTodayEth: formatEther(spent) };
  }
  if (m.collector) {
    const [eth, mil] = await Promise.all([m.collector.totalEthForwarded(), m.collector.totalMilordBurned()]);
    out.lpFees = { ethToBuyback: formatEther(eth), milordBurned: formatEther(mil) };
  }
  if (m.familyLock && m.D.familyId) {
    const [beneficiary, unlockBlock] = await m.familyLock.lockOf(m.D.familyId);
    const locked = beneficiary !== "0x0000000000000000000000000000000000000000";
    out.lock = { locked, forever: unlockBlock === 2n ** 64n - 1n, beneficiary, beneficiaryIsCollector: beneficiary === m.D.LockFeeCollector };
  }
  return out;
}

export async function status(m) {
  const [supply, backed, active, inv, minted, maxBacked, fp, paused, fees, exitBurn, buyBurn, pps, usdcIn, shares, dTotal] = await Promise.all([
    m.token.totalSupply(), m.conv.backedCount(), m.nft.activeSupply(), m.conv.inventorySize(), m.nft.totalMinted(),
    m.conv.maxSimultaneouslyBacked(), m.conv.feeParams(), m.conv.entryPaused(), m.conv.totalEntryFeesWei(),
    m.conv.totalMilordBurnedOnExit(), m.burner.totalBurnedFromBuybacks(), m.vault.officialPPS(),
    m.adapter.totalUsdcDeposited(), m.adapter.totalSharesMinted(), m.dist.totalClaimed()]);
  return {
    milordSupply: formatEther(supply), escrowedNfts: Number(backed), circulating: Number(active), inventory: Number(inv),
    minted: Number(minted), maxSimultaneouslyBacked: Number(maxBacked), entryPaused: paused,
    entryFeeBps: Number(fp.entryFeeBps), exitBurnBps: Number(fp.exitBurnBps), minEntryFeeEth: formatEther(fp.minEntryFeeWei),
    entryFeesEth: formatEther(fees), burnedOnExit: formatEther(exitBurn), burnedByBuyback: formatEther(buyBurn),
    parbPps: formatUnits(pps, 6), feeUsdcDeposited: formatUnits(usdcIn, 6), parbMinted: formatEther(shares), parbClaimed: formatEther(dTotal),
  };
}

/** Plain-language reason for a refused quote, with the service's estimate of when waking reopens. */
export function explainEntryWait(body) {
  const err = String(body?.error ?? "");
  const mins = body?.retryAfter ? Math.max(1, Math.ceil(body.retryAfter / 60)) : null;
  if (/deviates/.test(err)) return `The $MILORD price moved more than 10% from its 30-minute average, so waking pauses until the average catches up${mins ? ` (about ${mins} min if the price holds)` : ""}. A ~20% jump pauses waking for about 14 minutes. Redeeming is not affected.`;
  if (/insufficient price history|liquidity too low/.test(err)) return "Waking opens after the first purchase in the pool. Redeeming is not affected.";
  if (/approved launch price/.test(err)) return "The pool did not open at the approved launch price, so waking waits for 30 minutes of price history.";
  if (/outside the liquidity range/.test(err)) return "The price is outside the pool's liquidity range, so waking is paused. Redeeming is not affected.";
  if (/stale|behind/.test(err)) return "Pricing data is temporarily out of date, so waking waits. Redeeming is not affected.";
  return err || "Pricing is unavailable, so waking waits. Redeeming is not affected.";
}

/** Highest fee this page will let a wallet pay: the fee at the pool's on-chain price + 10%, read through the
 *  user's own wallet RPC, independent of the quote service. A wrong or compromised quote above it is never sent. */
export async function feeCeilingWei(m) {
  const tick = Number((await m.stateView.getSlot0(poolIdOf(m.key))).tick);
  const spotWpm = BigInt(Math.round(1e24 / 1.0001 ** tick));
  return m.conv.entryFeeFor((spotWpm * 110n) / 100n + 1n);
}

async function fetchQuote(m, quoteUrl) {
  const r = await fetch(`${quoteUrl}?account=${m.account}`);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(explainEntryWait(body)), { retryAfter: body.retryAfter ?? null, entryWaits: true });
  return body;
}

/** Ask the quote service, check its fee against the pool-based ceiling, then convert with maxFee = that fee.
 *  The wallet shows the exact ETH amount before signing. Approves MILORD if needed. */
export async function wake(m, quoteUrl) {
  const body = await fetchQuote(m, quoteUrl);
  const fee = await m.conv.entryFeeFor(body.quote.weiPerMillion);
  const ceiling = await feeCeilingWei(m);
  if (fee > ceiling) throw new Error(`Quoted fee ${formatEther(fee)} ETH is above the ${formatEther(ceiling)} ETH this page allows at the current pool price; nothing was sent. Try again in a few minutes.`);
  const principal = await m.conv.PRINCIPAL();
  if ((await m.token.allowance(m.account, m.D.MilordConverter)) < principal) await (await m.token.approve(m.D.MilordConverter, principal)).wait();
  const tx = await m.conv.convert(body.quote, body.signature, fee, { value: fee });
  return tx.wait();
}

export async function myMilords(m, who = m.account) {
  const n = Number(await m.nft.totalMinted());
  const out = [];
  for (let id = 1; id <= n; id++) {
    if ((await m.nft.ownerOf(id)).toLowerCase() !== who.toLowerCase()) continue;
    const p = await m.registry.currentPreference(id);
    out.push({ id, activation: Number(await m.nft.activationCount(id)), personal: p.personal, charA: Number(p.charA), charB: Number(p.charB), strategyId: Number(p.strategyId) });
  }
  return out;
}

export const redeem = async (m, id) => (await m.conv.redeem(id)).wait();
export const choose = async (m, id, a, b) => (await m.registry.select(id, a, b)).wait();

export async function characteristics(m) {
  const n = Number(await m.registry.characteristicCount());
  const list = [];
  for (let i = 1; i <= n; i++) { const c = await m.registry.characteristic(i); list.push({ id: i, name: c.name, active: c.active }); }
  return list;
}

/** epoch = published epoch file {root, claims:{addr:{cumulative, proof}}}. */
export async function rewards(m, epoch, who = m.account) {
  const payTo = who.toLowerCase(); // same chain: pARB is paid to the holder address
  const leaf = epoch?.claims?.[payTo];
  const claimed = await m.dist.claimed(payTo);
  const active = await m.dist.active();
  const live = leaf && active.root === epoch.root;
  const cumulative = leaf ? BigInt(leaf.cumulative) : 0n;
  return { payTo, cumulative: formatEther(cumulative), claimed: formatEther(claimed), claimable: formatEther(live && cumulative > claimed ? cumulative - claimed : 0n), live, leaf };
}

export async function claim(m, epoch, who = m.account) {
  const r = await rewards(m, epoch, who);
  if (!r.leaf || !r.live) throw new Error("no active allocation for this address");
  return (await m.dist.claim(r.payTo, r.leaf.cumulative, r.leaf.proof)).wait();
}

/** Current ETH fee from a fresh quote (display only; the real conversion requests its own quote).
 *  Returns { fee } or { wait: explanation }. */
export async function previewFee(m, quoteUrl) {
  try {
    const { quote } = await fetchQuote(m, quoteUrl);
    return { fee: formatEther(await m.conv.entryFeeFor(quote.weiPerMillion)) };
  } catch (e) {
    return { wait: e.message };
  }
}

// ───────────── market (Hookr pool) ─────────────
const poolIdOf = (key) => keccak256(AbiCoder.defaultAbiCoder().encode(["address", "address", "uint24", "int24", "address"], key));

/** Spot price and fee disclosure. Taxes are the pool's Hookr Tax + Conversion settings, LP fee is separate. */
export async function market(m) {
  if (!m.stateView || !m.hook) return null;
  const s0 = await m.stateView.getSlot0(poolIdOf(m.key));
  const tick = Number(s0.tick);
  const weiPerMillion = 1e24 / 1.0001 ** tick; // display only; quotes use the TWAP service
  const [buy, sell, comm, bt, st, hp, ha, bv, sv] = await Promise.all([m.hook.buyTaxBps(), m.hook.sellTaxBps(), m.hook.commissionBps(),
    m.hook.totalBuyTax(), m.hook.totalSellTax(), m.hook.totalPlatformPaid(), m.hook.platformAccrued(), m.hook.buyVolume(), m.hook.sellVolume()]);
  return {
    ethPerMillion: weiPerMillion / 1e18, buyTaxPct: Number(buy) / 100, sellTaxPct: Number(sell) / 100, hookrSharePct: Number(comm) / 100,
    lpFeePct: Number(m.D.lpFee) / 10000, taxCollectedEth: formatEther(bt + st), hookrCommissionEth: formatEther(hp + ha), volumeEth: formatEther(bv + sv),
  };
}

/** LOCAL ONLY: buy or sell through the local Hookr-style pool. On Robinhood Chain users trade on Hookr. */
export async function localTrade(m, side, amount) {
  if (!m.router) throw new Error("trade on Hookr: no local router in this deployment");
  const MIN = 4295128740n, MAX = 1461446703485210103287273052203988822378723970341n;
  if (side === "buy") {
    const v = parseEther(String(amount));
    return (await m.router.swap(m.key, [true, -v, MIN], [false, false], "0x", { value: v })).wait();
  }
  const v = parseEther(String(amount));
  if ((await m.token.allowance(m.account, m.D.SwapRouter)) < v) await (await m.token.approve(m.D.SwapRouter, v)).wait();
  return (await m.router.swap(m.key, [false, -v, MAX], [false, false], "0x")).wait();
}

// ───────────── strategy, pARB, governance ─────────────
export async function strategyInfo(m, id = 1) {
  const s = await m.registry.strategy(id);
  return { id, name: s.name, enabled: s.enabled, chainId: Number(s.chainId), vault: s.vault };
}

export async function parbPosition(m, who = m.account) {
  const [bal, pps] = await Promise.all([m.vault.balanceOf(who), m.vault.officialPPS()]);
  return { shares: formatEther(bal), pps: formatUnits(pps, 6), valueUsdg: formatUnits((bal * pps) / 10n ** 18n, 6) };
}

export async function governance(m, who = m.account) {
  const [votes, delegatee] = await Promise.all([m.nft.getVotes(who), m.nft.delegates(who)]);
  return { votes: Number(votes), delegatee, selfDelegated: delegatee.toLowerCase() === who.toLowerCase() };
}
export const delegateSelf = async (m) => (await m.nft.delegate(m.account)).wait();

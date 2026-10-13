// MILORD frontend SDK (browser ESM, ethers v6). Thin wrappers over the contracts; no business logic
// beyond formatting. Import with:  import * as milord from "./milord-sdk.mjs";
import { BrowserProvider, Contract, formatEther, formatUnits } from "https://cdn.jsdelivr.net/npm/ethers@6.13.4/+esm";

const A = {
  token: ["function balanceOf(address) view returns (uint256)", "function totalSupply() view returns (uint256)",
    "function allowance(address,address) view returns (uint256)", "function approve(address,uint256) returns (bool)"],
  nft: ["function ownerOf(uint256) view returns (address)", "function balanceOf(address) view returns (uint256)",
    "function totalMinted() view returns (uint256)", "function activeSupply() view returns (uint256)",
    "function activationCount(uint256) view returns (uint256)", "function getVotes(address) view returns (uint256)",
    "function tokenURI(uint256) view returns (string)", "function isActive(uint256) view returns (bool)"],
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
  payout: ["function payoutAddress(address) view returns (address)", "function setPayoutAddress(address)"],
  vault: ["function officialPPS() view returns (uint256)", "function balanceOf(address) view returns (uint256)",
    "function requestRedeem(uint256,address) returns (uint256)", "function lastReportTimestamp() view returns (uint256)"],
  dist: ["function claimed(address) view returns (uint256)", "function totalClaimed() view returns (uint256)",
    "function active() view returns (bytes32 root,uint256 totalCumulative,uint64 epoch,uint64 activatesAt,bytes32 dataHash)",
    "function claim(address,uint256,bytes32[]) returns (uint256)"],
  burner: ["function totalBurnedFromBuybacks() view returns (uint256)", "function budget() view returns (uint256)"],
  adapter: ["function totalUsdcDeposited() view returns (uint256)", "function totalSharesMinted() view returns (uint256)"],
};

export async function connect(deployments, ethereum = window.ethereum) {
  const provider = new BrowserProvider(ethereum);
  const signer = await provider.getSigner();
  const c = (addr, abi) => new Contract(addr, abi, signer);
  const D = deployments;
  return {
    provider, signer, account: await signer.getAddress(), D,
    token: c(D.MILORD, A.token), nft: c(D.Milords, A.nft), conv: c(D.MilordConverter, A.conv),
    registry: c(D.StrategyRegistry, A.registry), payout: c(D.BasePayoutRegistry, A.payout),
    // On mainnet the next four live on Base: build them with a Base provider instead.
    vault: c(D.pARBVault, A.vault), dist: c(D.ShareDistributor, A.dist), adapter: c(D.ParbFundingAdapter, A.adapter),
    burner: c(D.BuybackBurner, A.burner),
  };
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

/** Ask the quote service, then convert with maxFee = quoted fee. Approves MILORD if needed. */
export async function wake(m, quoteUrl) {
  const r = await fetch(`${quoteUrl}?account=${m.account}`);
  const body = await r.json();
  if (!r.ok) throw new Error(body.error ?? "quote unavailable; conversion is paused until pricing returns");
  const principal = await m.conv.PRINCIPAL();
  if ((await m.token.allowance(m.account, m.D.MilordConverter)) < principal) await (await m.token.approve(m.D.MilordConverter, principal)).wait();
  const fee = await m.conv.entryFeeFor(body.quote.weiPerMillion);
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
export const setPayout = async (m, addr) => (await m.payout.setPayoutAddress(addr)).wait();

export async function characteristics(m) {
  const n = Number(await m.registry.characteristicCount());
  const list = [];
  for (let i = 1; i <= n; i++) { const c = await m.registry.characteristic(i); list.push({ id: i, name: c.name, active: c.active }); }
  return list;
}

/** epoch = published epoch file {root, claims:{addr:{cumulative, proof}}}. */
export async function rewards(m, epoch, who = m.account) {
  const payTo = (await m.payout.payoutAddress(who)).toLowerCase();
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

/** Current ETH fee from a fresh quote (display only; the real conversion requests its own quote). */
export async function previewFee(m, quoteUrl) {
  const r = await fetch(`${quoteUrl}?account=${m.account}`);
  if (!r.ok) return null;
  const { quote } = await r.json();
  return formatEther(await m.conv.entryFeeFor(quote.weiPerMillion));
}

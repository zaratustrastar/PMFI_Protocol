// Service configuration: contract addresses from a deployments JSON (DEPLOYMENTS_FILE, same keys as
// sim/deployments.local.json) and everything else from environment variables. Keys come from env only
// (in production: injected by the secret manager); they are never logged.
import fs from "node:fs";
import { JsonRpcProvider, Wallet, Contract, NonceManager } from "ethers";

export const ABI = {
  splitter: ["function undistributed(address) view returns (uint256)", "function distribute(address) returns (uint256)"],
  collector: ["function collect(uint8) returns (uint256,uint256)"],
  burner: ["function budget() view returns (uint256)", "function buybackAndBurn(uint256,uint256,uint256) returns (uint256)"],
  router: ["function executable() view returns (uint256)", "function execute() returns (uint256)", "function paused() view returns (bool)"],
  adapter: [
    "function batchCount() view returns (uint256)",
    "function batch(uint256) view returns (tuple(bytes32 sourceRef,uint256 usdcAmount,uint256 requestId,uint256 sharesMinted,uint64 fundedAt,uint64 settledAt))",
    "function settle(uint256) returns (uint256)",
  ],
  vault: ["function depositRequests(uint256) view returns (address,address,uint256,uint256,uint8 status,uint256)", "function balanceOf(address) view returns (uint256)"],
  distributor: [
    "function pending() view returns (bytes32 root,uint256 totalCumulative,uint64 epoch,uint64 activatesAt,bytes32 dataHash)",
    "function active() view returns (bytes32 root,uint256 totalCumulative,uint64 epoch,uint64 activatesAt,bytes32 dataHash)",
    "function activateRoot()",
    "function proposeRoot(bytes32,uint256,uint64,bytes32)",
    "function totalClaimed() view returns (uint256)",
  ],
  nft: ["event Transfer(address indexed from, address indexed to, uint256 indexed tokenId)"],
  hook: ["function collect()"],
  taxQueue: [
    "function terms() view returns (tuple(address root,address rules,bytes32 poolId,address quote,bool isBuy,address protocolRecipient,uint16 protocolShareBps,bytes32 routeId,address assetRecipient,address recoveryRecipient,uint32 staleRecoveryDelay))",
    "function ledger() view returns (tuple(uint256 protocolOwed,uint256 creatorOwed,uint256 income,uint256 protocolIncome,uint256 creatorIncome,uint256 protocolSwept,uint256 creatorConverted,uint256 creatorPaid,uint256 outputDelivered))",
    "function pending() view returns (uint256)",
    "function accountingInvariant() view returns (bool)",
    "function payCreator() returns (uint256)",
  ],
  launcher: ["function familyOwner(bytes32) view returns (address)", "function principalLockedUntil(bytes32) view returns (uint256)"],
  directionalTax: ["function terms(address root, bytes32 poolId) view returns (tuple(address buyQueue,uint24 buyTaxPips,bool bound,bool tiered,bool decaying,address sellQueue,uint24 sellTaxPips))"],
};

export function loadConfig(env = process.env) {
  const file = env.DEPLOYMENTS_FILE ?? new URL("../../../sim/deployments.local.json", import.meta.url).pathname;
  const D = JSON.parse(fs.readFileSync(file, "utf8"));
  const num = (k, d) => (env[k] === undefined ? d : Number(env[k]));
  return {
    D,
    rpcUrl: env.RPC_URL ?? "http://127.0.0.1:8545",
    chainId: num("CHAIN_ID", D.chainId),
    genesis: num("EPOCH_GENESIS", 0), // Monday 00:00 UTC of the first distribution week
    settlementGrace: num("SETTLEMENT_GRACE_SECS", 6 * 3600),
    confirmations: num("FINALITY_CONFIRMATIONS", 64),
    intervalMs: num("INTERVAL_SECS", 300) * 1000,
    healthPort: num("HEALTH_PORT", 9100),
    stateDir: env.STATE_DIR ?? "./state",
    minBuybackWei: BigInt(env.MIN_BUYBACK_WEI ?? "10000000000000000"),
    buybackMaxBelowTwapBps: num("BUYBACK_MAX_BELOW_TWAP_BPS", 1000),
    startBlock: num("START_BLOCK", D.startBlock ?? 0), // first block to index (deployment block)
    logStep: num("LOG_BLOCK_RANGE", 10_000),
    hookrSettle: env.HOOKR_SETTLE === "mock", // local mock tax hook only; on mainnet the Hookr tax queues below
    minTaxPayoutWei: BigInt(env.MIN_TAX_PAYOUT_WEI ?? "1000000000000000"),
  };
}

export function connect(cfg, keyEnv) {
  if (cfg.hookrSettle && (cfg.chainId === 4663 || cfg.chainId === 46630)) throw new Error("HOOKR_SETTLE=mock is for the local simulation only");
  if (cfg.chainId === 4663 && process.env.ALLOW_MAINNET !== "yes") throw new Error("Robinhood mainnet: set ALLOW_MAINNET=yes only after owner approval");
  const provider = new JsonRpcProvider(cfg.rpcUrl, cfg.chainId, { staticNetwork: true });
  const key = keyEnv ? process.env[keyEnv] : undefined;
  const signer = key ? new NonceManager(new Wallet(key, provider)) : null;
  const c = (addr, abi) => (addr ? new Contract(addr, abi, signer ?? provider) : null);
  const D = cfg.D;
  return {
    provider,
    signer,
    contracts: {
      splitters: [D.EntryFeeSplitter, D.RoyaltySplitter, D.TradingTaxSplitter].filter(Boolean).map((a) => c(a, ABI.splitter)),
      collector: c(D.LockFeeCollector, ABI.collector),
      burner: c(D.BuybackBurner, ABI.burner),
      router: c(D.ArbitrageFundingRouter, ABI.router),
      adapter: c(D.ParbFundingAdapter, ABI.adapter),
      vault: c(D.pARBVault, ABI.vault),
      distributor: c(D.ShareDistributor, ABI.distributor),
      nft: c(D.Milords, ABI.nft),
      hookrSettle: cfg.hookrSettle && D.HookrTaxHook ? () => c(D.HookrTaxHook, ABI.hook).collect() : null,
      taxQueues: [
        D.HookrBuyTaxQueue && { name: "buy", queue: c(D.HookrBuyTaxQueue, ABI.taxQueue) },
        D.HookrSellTaxQueue && { name: "sell", queue: c(D.HookrSellTaxQueue, ABI.taxQueue) },
      ].filter(Boolean),
      lockWatch: D.HookrLauncher && D.MilordFamilyId && D.HookrFamilyLock
        ? { launcher: c(D.HookrLauncher, ABI.launcher), familyId: D.MilordFamilyId, familyLock: D.HookrFamilyLock }
        : null,
    },
  };
}

/** block.number as contracts see it on an Arbitrum-style chain: the parent-chain height (l1BlockNumber). */
export async function parentBlock(provider) {
  const b = await provider.send("eth_getBlockByNumber", ["latest", false]);
  if (b?.l1BlockNumber === undefined) throw new Error("RPC returned no l1BlockNumber: cannot read Robinhood Chain's block.number");
  return BigInt(b.l1BlockNumber);
}

/**
 * The buy and sell HookrTaxQueue clones of the MILORD pool. Taken from the deployments file when recorded
 * (MilordAtomicLaunch.buyQueue/sellQueue after the launch), and always cross-checked against
 * HookrDirectionalTax.terms(root, poolId): a disagreement is a configuration error, never resolved silently.
 */
export async function resolveTaxQueues(D, provider) {
  if (!D.HookrDirectionalTax || !D.HookrRoot || !D.MilordPoolId) {
    if (D.HookrBuyTaxQueue || D.HookrSellTaxQueue) throw new Error("tax queues recorded without HookrDirectionalTax/HookrRoot/MilordPoolId to check them against");
    return null;
  }
  const t = await new Contract(D.HookrDirectionalTax, ABI.directionalTax, provider).terms(D.HookrRoot, D.MilordPoolId);
  if (!t.bound) throw new Error("HookrDirectionalTax has no terms for the MILORD pool");
  for (const [k, onchain] of [["HookrBuyTaxQueue", t.buyQueue], ["HookrSellTaxQueue", t.sellQueue]]) {
    if (D[k] && D[k].toLowerCase() !== onchain.toLowerCase()) throw new Error(`${k} ${D[k]} disagrees with HookrDirectionalTax (${onchain})`);
  }
  return { HookrBuyTaxQueue: t.buyQueue, HookrSellTaxQueue: t.sellQueue };
}

/** Latest block considered final (Robinhood Chain is an Arbitrum Orbit chain: use the 'finalized' tag when served). */
export async function finalizedBlock(provider, confirmations) {
  try {
    const b = await provider.getBlock("finalized");
    if (b) return b;
  } catch {}
  const latest = await provider.getBlockNumber();
  return provider.getBlock(Math.max(0, latest - confirmations));
}

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
    hookrSettle: env.HOOKR_SETTLE === "mock", // local mock tax hook only; on mainnet Hookr's own settlement
  };
}

export function connect(cfg, keyEnv) {
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
    },
  };
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

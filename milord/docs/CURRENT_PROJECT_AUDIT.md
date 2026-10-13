# Current project audit (PMFI → MILORD)

Scope: `zaratustrastar/PMFI_Protocol` at commit `3c8b66e`, the website prototype (claude.ai artifact, three
revisions), and public Hookr documentation. On-chain state on Base and Robinhood Chain could **not** be read
from this environment (RPC hosts blocked), so every "deployed" statement below comes from files in the repo,
not from chain queries.

## 1. What exists

| Area | Location | Status |
|---|---|---|
| pSniper vaults V1–V7 | `contracts/PredictFiSniperVault*.sol`, `deployments/v6-mainnet.json`, `v7-mainnet.json` | Legacy, Base. V7 at `0x5Ae7395Ca8ab044159133E2e23f421aE8f94055F`. Not used by MILORD. |
| pARB vault V1 | `contracts/PMFIArbVaultV1.sol` | Deployed at `0x10f67BA7aB746a0DC8A48f0D74aA3a962328E689` (per repo docs). |
| pARB vault V2 | `contracts/PMFIArbVaultV2.sol`, `docs/pmfi/DEPLOY_PARB_V2.md` | Address **not in the repo**; frontend reads `ARB_VAULT_V2_ADDRESS` from `window.PSNIPER_CONFIG`. |
| Reporter (NAV signer) | `bot/arb_monitor/core/arb_reporter.py`, `arb_nav.py` | Signs `ReportDataV2` for V2. |
| Frontend | `frontend/` (index.html, main.js, admin.html, assets) | pSniper/pARB app. No Milords UI. |
| Milords website | claude.ai artifact (not in repo) | Compiled HTML only, no source project. |
| Milords art / metadata | — | **Not present anywhere.** |
| Hardhat config | `hardhat.config.cjs` | solc 0.8.20, optimizer 200, no via-IR. |

## 2. pARB V1 findings

- NAV is read live and applied on every transaction; no performance fee.
- Owner can call `emergencyWithdraw` at any time (custody risk for depositors).
- Withdrawals are fixed at the request-time NAV; claims may pay up to 1% short when the buffer is low.

## 3. pARB V2 findings (the design MILORD reuses)

Good and reused: async `requestDeposit → report → claim/autoClaim`, FIFO queues, bootstrap dead shares,
signed reports with nonce/deadline/staleness, `tend()` idle-buffer logic, HWM bookkeeping.

Problems (each reproduced in `test/base/LegacyV2Bugs.t.sol` against a locally compiled copy):

| # | Issue | Effect |
|---|---|---|
| V2-1 | `PERF_FEE_BPS = 2000` is a constant | MILORD's required 0% fee needs a new deployment. |
| V2-2 | Outstanding shares subtract `totalPendingRedeemShares` while their assets stay in backing | PPS inflated while redemptions wait (test: PPS 1.00 → 2.00 with zero profit). |
| V2-3 | Processed-but-unclaimed deposit shares are not counted | PPS inflated at the next report (test: 1.00 → 2.00 with zero profit; earlier holders could redeem later depositors' money). |
| V2-4 | `newPPS == 0` is reset to `INITIAL_PPS` ($1) | A total loss is published as $1.00. |
| V2-5 | No check that `reportedAssets` covers the vault's own USDC | A single bad report can zero NAV. |
| V2-6 | No bound on PPS change per report | Single signer key can move price arbitrarily. |
| V2-7 | `paused = true` in shutdown blocks `report()` | Pending redemptions can never be processed after shutdown. |
| V2-8 | `emergencyWithdraw` (shutdown) can take reserved USDC | Claimable redemptions/pending deposits not protected. |
| V2-9 | `MAX_REPORT_INTERVAL` declared, unused | Cosmetic. |
| V2-10 | Committed source does not compile with the repo's Hardhat settings (stack too deep) | Deployed bytecode cannot be reproduced/verified from the repo as-is. |

`src/legacy/PMFIArbVaultV2.sol` is a copy with only a stack-depth refactor (marked `[milord-audit]`), logic
unchanged. **The deployed V2 is not modified.**

## 4. Reporter findings (`arb_reporter.py`)

- `reportedAssets = vault USDC + servicer + Polymarket + Kalshi + Opinion cash + open positions`.
- Open positions are valued at `cost_basis_usdc` although the docstring says "$0.99/share" and "NO open position marks".
- `REPORTED_ASSETS_HAIRCUT = 0.95` is defined but never applied.
- In-flight transfers between venues are not counted (under-reporting risk).
- One-legged (unhedged) positions are valued at cost, not market.

These matter for MILORD because pARB price is the only source of holder value. See `PARBITRAGE_INTEGRATION.md`.

## 5. Website prototype

Sections: hero, intro (token / NFT / engine cards), the cycle, fees calculator, engine diagram,
"What a Milord does", "Holding PMFI on Base?", status. Visual direction is kept; copy conflicts with the spec:

| Prototype says | Spec says |
|---|---|
| "1,010,000 $MILORD in, plus 0.005 ETH"; "990,000 back plus 0.005 ETH" | Exactly 1,000,000 in + ETH fee = max(10% × ETH value, 0.002 ETH); exit 900,000 back, 100,000 burned, no ETH. |
| "The locked tokens and its wallet go with it" | Principal goes with the NFT (correct). The ERC-6551 wallet's assets also move — documented as a risk, not a feature promise. |
| "PMFI earns a planned 20% of new profit" | 0% performance fee on the MILORD pARB instance. |
| "Claim $MILORD … through a contract" (migration) | No migration contract; legacy holders handled manually, documented only. |
| "Revenue distributions … under review" | Protocol fees fund pARB shares distributed weekly; no return is promised. |

## 6. Hookr

See `HOOKR_COMPATIBILITY.md`. Summary: Hookr cannot provide an editable 1.5% buy/sell tax routed to
project Safes, and its native launch is incompatible with a 6.767B supply. This is raised as a decision.

## 7. Missing inputs (cannot be substituted)

1. Milords artwork and metadata (and the trait → characteristic mapping, if any).
2. Website source project (only compiled artifacts exist).
3. The 8 characteristic names.
4. pARB V2 deployed address/bytecode for verification (not needed if V3 is approved).
5. Robinhood Chain mainnet RPC. (Superseded 2026-10-13: the pARB vault now runs on Robinhood Chain with USDG, so fees need no bridge; see ROBINHOOD_VAULT_MIGRATION.)
6. Safe addresses (Team, Arbitrage Funding), quote-signer and report-signer key custody plan.

## 8. Data and execution providers used by the bot

From `bot/arb_monitor` (URLs only; no secrets read). Liveness was **not** checked (network blocked here).

| Purpose | Provider (host) |
|---|---|
| Polymarket order book / markets | `clob.polymarket.com`, `gamma-api.polymarket.com` |
| Kalshi | `api.elections.kalshi.com` |
| Opinion | `proxy.opinion.trade` |
| Cross-market discovery / matching | `api.oddpool.com`, `oddscreeners.com` (plus an AI matcher) |
| Bridging | LI.FI (`li.quest`), Relay (`api.relay.link`) |
| RPC | Base, Polygon, BSC public RPCs |

The project brief says an earlier data provider was deprecated; the discovery providers above are the likely
candidates and need a liveness check from a networked machine. LI.FI and Relay are already integrated and are
the natural candidates for the Robinhood Chain → Base fee bridge (decision D5), subject to Robinhood Chain support.

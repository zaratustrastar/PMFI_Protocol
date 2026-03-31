# Overview

**Brand: PMFI** (formerly PredictFi — all user-facing labels now use PMFI).

This project consists of three primary systems under the PMFI brand: the **pSNIPER Vault (V7.5)** for secure NAV-based share pricing using actual liquid asset values with withdrawal exclusion, the **pARB Vault (V1)** for cross-venue arbitrage (Polymarket × Kalshi × Opinion Labs) using the Oddpool API, and an **Automated Polymarket Trading Bot**. The vaults calculate NAV from real position values; pARB uses guaranteed-spread arb with liquid NAV priced from order-book bids. The trading bot provides fully automated market monitoring, Telegram notifications, and strategic trading for efficient Polymarket participation.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## pSNIPER Vault (V7.5)

The vault calculates NAV using **ACTUAL LIQUID VALUE** with **withdrawal exclusion**.

**V7.5.1 CHANGE (Feb 2026)**: Fixed double-subtraction during withdrawal bridge transit.
- Problem: When servicer bridges USDC from Polygon to Base, funds disappear from totalAssets
  (cash left PM) while usdcLocked exclusion also subtracts them → double-count → temporary depeg
- Fix: `effective_exclusion = max(0, usdcLocked - withdrawal_bridge_in_transit)`
  where `withdrawal_bridge_in_transit` = funds already debited from Polygon, not yet on Base
- Reads servicer's `withdrawal_state.json` for pending bridge amounts
- Observability: NAV breakdown now shows `usdc_locked_total`, `bridge_in_transit`, `effective_exclusion`
- **$5 minimum withdrawal** enforced on both web app and mini app frontends

**V7.5 (still in effect)**: Pending withdrawals excluded from NAV calculation.
- When `requestWithdraw()` is confirmed, shares transfer to vault and `usdcLocked` is recorded
- These are "spoken for" - excluded from both asset and supply sides of NAV
- `effective_assets = totalAssets - effective_exclusion` (adjusted for in-transit bridges)
- `effective_supply = totalSupply - totalPendingShares`
- `NAV = effective_assets / effective_supply`
- **Result**: Remaining LPs see accurate pricing regardless of withdrawal pipeline stage

**V7.4 (still in effect)**: NAV reflects actual position values, not expected deposits.
- `totalAssets = pmCash + positionsLiqValue + vaultBuffer + inFlight` (ACTUAL values only)
- `pendingCredit = 0` for NAV purposes (calculated separately for monitoring bridging delays)

**Safety Valves**:
- Only `maxPendingAge` on in-flight funds (actual bridging delays)
- Removed `maxPendingRatio` since pendingCredit is no longer in NAV

**Previous Fixes (still in effect)**:
- Uses on-chain `expectedAssets` for reference (not cumulative `totalForwarded`)
- Uses position **liquidation value** (mark-to-market) instead of cost basis
- **Reserved excluded from NAV math** - Polygon balanceOf is the source of truth for cash
- Withdrawal servicer reads actual `usdcLocked` from pending requests (V7.3.3 fix)

**Current Contract**: `0x17C27001929E75D1eBd5FdeE6E986EA5a91de0D1` (Base Mainnet, V7.5)

## pARB Vault (V2) — Architecture (current)

Cross-venue arb vault trading Polymarket × Kalshi × Opinion Labs via Oddpool API.
V2 is an async Yearn-style vault: no live NAV required for user flows.

**Contract** (`contracts/PMFIArbVaultV2.sol`):
- `requestDeposit(assets, receiver)` → queued; `claimDeposit(requestId, receiver)` after `report()`
- `requestRedeem(shares, receiver)` → queued; `claimRedeem(requestId, receiver)` after report + liquidity
- `tend()` — permissionless; refills 10% idle buffer from strategy
- `report(reportData, sig)` — keeper-only; updates `officialPPS`, processes queues, 20% perf fee (dilution), loss carryforward high-water-mark
- Domain salt: `keccak256("PMFIArbVaultV2.v1")`

**Reporter** (`bot/arb_monitor/core/arb_reporter.py`):
- Signs `ReportDataV2` with `ARB_NAV_SIGNER_PRIVATE_KEY`
- Conservative `reportedAssets`: cash only (no position marks), 95% haircut
- Sweeps servicer USDC to vault when redemptions pending
- Runs via `run_reporter_tick()` in execution loop each cycle

**NAV Oracle** (`bot/arb_monitor/core/arb_nav.py`):
- Tracks cash on all 3 platforms: `poly_cash` (`POLY_API_KEY`), `kalshi_cash` (RSA auth), `opinion_cash` (`OPINION_API_KEY`)
- Open position liquid value from orderbook bids (not cost basis)
- `totalAssets = poly_cash + kalshi_cash + opinion_cash + open_positions + settled_pnl`
- Separate from pSNIPER oracle (`ORACLE_PRIVATE_KEY`, domain `PredictFiSniperVaultV7.v7`)

**Key env vars** (pARB V2, separate from pSNIPER):
- `ARB_VAULT_V2_ADDRESS` — deployed V2 contract address on Base (activates V2; unset = silent skip)
- `POLY_API_KEY` — pARB's Polymarket API key (not `POLYMARKET_API_KEY` which is pSNIPER's)
- `POLY_PRIVATE_KEY` — pARB trading wallet private key
- `ARB_NAV_SIGNER_PRIVATE_KEY` — signs report payloads for V2 contract
- `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` — Kalshi RSA auth
- `OPINION_API_KEY` — Opinion Labs API key

**Frontend** (`frontend/main.js`):
- Auto-routes to V2 when `ARB_VAULT_V2_ADDRESS` is set in `window.PSNIPER_CONFIG`
- V1 code retained as fallback if only `ARB_VAULT_ADDRESS` is set
- New functions: `handleArbClaimDeposit(requestId)`, `handleArbClaimRedeem(requestId)`, `loadArbPendingRequests()`
- HTML requires `<div id="arbPendingRequests" class="hidden"></div>` in pARB section

**Execution** (`bot/arb_monitor/core/arb_execution_loop.py`):
- Sorted by `pnl_velocity = gross_edge_pct / max(days_to_expiry, 0.5)` descending
- Oddpool slugs resolved to real Polymarket CLOB token IDs via Gamma API (60-min cache)
- `is_display_only=False` when token resolved → execution enabled; `True` → skipped with retry
- Per-pair cap (`ARB_MAX_PAIR_USDC`) and total cap (`ARB_MAX_DEPLOYED_USDC`) enforced
- Live price re-check + slippage guard (50 bps) before every trade
- Auto-unwind leg 1 if leg 2 fails (Kalshi or Opinion)
- `run_reporter_tick()` called before `run_funder_tick()` each cycle

**Current V2 Contract**: `0x9A1dcC11870ff45382E5fe422Cf393Fa81345dEC` (Base Mainnet) — set `ARB_VAULT_V2_ADDRESS` to this in VPS `.env`
**Legacy V1 Contract**: `0x10f67BA7aB746a0DC8A48f0D74aA3a962328E689` — stays live until all V1 holders redeem

## Polymarket Trading Bot

This system automates Polymarket monitoring and trading.

**1. Market Monitoring (Mastra Workflow)**: A cron-triggered workflow (every minute) fetches new Polymarket markets, posts them to Telegram, and queues relevant markets for trading in a PostgreSQL database.

**Market Filtering (Jan 2026 v2 - Reduced False Positives)**:
- **Up/Down Markets**: Filters out short-term markets with keywords like "up or down", "15m", "1h", etc.
- **Duration Filter**: Skips markets that close within 15 hours
- **Crypto/Stock Markets**: Improved hybrid NLP scoring with reduced false positives:
  - **Safe tickers** (+3 pts): BTC, ETH, AAPL, NASDAQ, TSLA, etc. - match with word boundaries
  - **Risky tickers** (sol, ada, dot, link, near, atom, uni, meta, apple, amazon, google): Only count if:
    - $TOKEN format (e.g., $SOL)
    - Full name present (e.g., "sol" + "solana")
    - Hard-finance keyword present
  - **Hard-finance keywords** (+2 pts): etf, sec, futures, halving, approval, market cap, ath
  - **Soft keywords** (+2 pts, only if ticker hit): price, trading, breakout, resistance, support
  - **Tags** (+2 pts): crypto, stocks, defi, finance
  - **Exclusion rule**: Require `ticker_score > 0 AND total_score >= 5`
- **Defense-in-depth**: Both market_monitor.py and auto_trader.py apply same filter logic

**4. XP / Tasks / Referral System (Feb 2026)**:
- Database tables: `xp_users` (fid PK, username, wallet, referrer_fid), `xp_events` (idempotent via unique_key), `referral_earnings` (unique per referrer+referee+source)
- Tasks: follow_fc (100 XP, verified), deposit_10 (500 XP, verified), invite (250 XP, verified), follow_x (100 XP, manual/PENDING_REVIEW, locked until first 3 completed)
- Referral: 10% of all referee XP awarded to referrer automatically; link format `?ref=<fid>`
- Endpoints: POST /api/me, POST /api/referral/attach, GET /api/state?fid=, GET /api/leaderboard?scope=all|weekly
- Helper: `award_xp(fid, type, xp, meta, unique_key)` — idempotent, auto-generates unique_key if missing, auto-awards referral bonus in same transaction

**2. Trading Job Worker (Python)**: A continuous worker polls the `trading_jobs` queue, places laddered buy orders (1¢-3¢ on YES/NO tokens) for new markets, and updates job status. This component requires a residential IP due to Cloudflare blocking datacenter IPs.

**3. Order Monitor (Python)**: Continuously monitors all active orders. It auto-cancels stale orders (>12 hours) to free up capital, places laddered sell orders upon fill, and sends Telegram notifications when sells execute. This also requires a residential IP.

**Sell Ladder Strategy (Feb 2026 Update)**:
- Minimum 25 shares accumulated before any sell orders are placed (`MIN_SHARES_FOR_SELL_LADDER`)
- Reserve 10% of position for resolution (no orders placed, wait for market to resolve)
- Tier 1: 33% of position @ 3x (200% profit)
- Tier 2: 27% of position @ 4x (300% profit)
- Tier 3: 30% of position @ 8x (700% profit)
- **Incremental fills**: If new shares are bought after sells are placed, additional sell orders are placed for the delta (must also meet 25-share threshold)
- Tracks `shares_with_sells` in `accumulated_fills` DB table to detect new fills
- Configuration in `trading_bot/config.py` via `SELL_LADDER_CONFIG`, `SELL_RESERVE_RATIO`, and `MIN_SHARES_FOR_SELL_LADDER`

## Core Framework (Mastra)

The application is built on the Mastra Framework, an AI-powered TypeScript framework providing agent orchestration with LLMs, a graph-based workflow engine for deterministic multi-step processes, a tool system for external interactions, and a three-tier memory management system (Conversation History, Semantic Recall, Working Memory). It supports multi-agent coordination through routing agents and unified model routing for various LLM providers.

## Workflow Architecture

Mastra's graph-based workflows enable deterministic execution with input/output validation (Zod), sequential (`.then()`) and parallel (`.parallel()`) execution, branching (`.map()`), and robust error handling. Workflows support suspend/resume capabilities for human-in-the-loop, external waiting, and event-driven processes. Inngest provides durable workflow execution, step memoization, real-time monitoring, and a publish-subscribe event system.

## Streaming and Triggers

The system supports real-time streaming for incremental response generation from agents and workflows, including text deltas, tool events, and workflow progress. Triggers include webhook integrations (Slack, Telegram) and cron workflows via Inngest for scheduled tasks. Custom API routes are supported for webhook handlers.

## Replit-Specific Architecture

A custom Replit Playground UI offers user interaction and workflow graph visualization. The project uses Mastra's deployer for Replit infrastructure, with OpenTelemetry for observability and a custom build system.

# External Dependencies

## AI Model Providers

-   **OpenAI**: Primary LLM provider (`@ai-sdk/openai`, `openai` SDK).
-   **Anthropic, Google, xAI**: Supported via Mastra's unified routing.
-   **OpenRouter**: For accessing multiple models through a single gateway.
-   **Vercel AI SDK**: Core AI abstractions (`ai` package v4.x).

## Databases and Storage

-   **LibSQL**: Primary local/embedded database with vector support (`@mastra/libsql`).
-   **PostgreSQL**: Production storage with `pgvector` extension (`@mastra/pg`).
-   **Vector Databases**: For semantic recall (LibSQL, Postgres with pgvector, or Upstash Vector).

## External Services

-   **Inngest**: Workflow orchestration for durable execution (`inngest`, `inngest-cli`, `@mastra/inngest`, `@inngest/realtime`).
-   **Slack**: Bot integration (`@slack/web-api`).
-   **Telegram**: Bot webhook integration.
-   **Exa**: Search API integration (`exa-js`).

## Core Libraries

-   **Zod**: Schema validation.
-   **Pino**: Structured logging.
-   **TypeScript**: Type system.
-   **dotenv**: Environment variable management.

## Runtime Requirements

-   Node.js >=20.9.0.
-   Environment variables for API keys and database connections.
-   Webhook tokens for integrations.
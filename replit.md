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
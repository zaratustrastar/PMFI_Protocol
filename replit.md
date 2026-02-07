# Overview

This project consists of two primary systems: the pSNIPER Vault (V7.5) for secure NAV-based share pricing using actual liquid asset values with withdrawal exclusion, and an Automated Polymarket Trading Bot. The vault calculates NAV from real position values (cash + positions liquidation value), excluding pending withdrawal liabilities from both assets and share supply to prevent NAV distortion. The trading bot provides fully automated market monitoring, Telegram notifications, and strategic trading, designed for efficient and timely execution on Polymarket. Together, these systems aim to optimize and automate Polymarket participation with accurate financial tracking and trading capabilities.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## pSNIPER Vault (V7.5)

The vault calculates NAV using **ACTUAL LIQUID VALUE** with **withdrawal exclusion**.

**V7.5 CHANGE (Feb 2026)**: Pending withdrawals excluded from NAV calculation.
- When `requestWithdraw()` is confirmed, shares transfer to vault and `usdcLocked` is recorded
- These are "spoken for" - excluded from both asset and supply sides of NAV
- `effective_assets = totalAssets - total_usdcLocked`
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

**Current Contract**: `0xbF0944893e6bd445F715dE76CD6343B1d551D41B` (Base Mainnet, V7.5)

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

**2. Trading Job Worker (Python)**: A continuous worker polls the `trading_jobs` queue, places laddered buy orders (1¢-3¢ on YES/NO tokens) for new markets, and updates job status. This component requires a residential IP due to Cloudflare blocking datacenter IPs.

**3. Order Monitor (Python)**: Continuously monitors all active orders. It auto-cancels stale orders (>12 hours) to free up capital, places laddered sell orders upon fill, and sends Telegram notifications when sells execute. This also requires a residential IP.

**Sell Ladder Strategy (Jan 2026 Update)**:
- Reserve 10% of position for resolution (held untouched)
- Tier 1: 30% of position @ 3x (200% profit)
- Tier 2: 30% of position @ 4x (300% profit)
- Tier 3: 30% of position @ 5x (400% profit)
- Configuration in `trading_bot/config.py` via `SELL_LADDER_CONFIG` and `SELL_RESERVE_RATIO`

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
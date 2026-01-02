# Overview

This project consists of two primary systems: the pSNIPER Vault (V7.1) for secure, 3-state asset tracking of USDC investments in Polymarket, and an Automated Polymarket Trading Bot. The vault ensures accurate NAV calculation and capital conservation, addressing prior issues with in-flight assets. The trading bot provides fully automated market monitoring, Telegram notifications, and strategic trading, designed for efficient and timely execution on Polymarket. Together, these systems aim to optimize and automate Polymarket participation with robust financial tracking and trading capabilities.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## pSNIPER Vault (V7.3.2)

The vault features a 3-state asset tracking system to accurately manage USDC within Polymarket. Asset states include `inFlightOnChain`, `pendingCredit`, and `creditedAssets`. 

**V7.3.2 FIX (Jan 2026)**: Uses on-chain `expectedAssets` instead of cumulative `totalForwarded`.

BUCKET INVARIANT: Funds live in exactly ONE bucket at any time - no overlap:
- `inFlight`: USDC at deposit address on Base (not yet swept)
- `pendingCredit`: Swept/bridging, not visible yet in PM
- `cash/reserved/costBasis`: Credited inside PM account
- `vaultBuffer`: USDC in vault contract (claimable withdrawals) - **on-chain truth**

Formula: `pendingCredit = max(0, expectedAssets - pmCash - reserved - costBasis - inFlight - vaultBuffer)`

**Why V7.3.2?** The previous V7.3.1 used `totalForwarded` which is cumulative and never decreases. When users claim funds from vaultBuffer, those funds exit the system but `totalForwarded` stayed high, causing massive pending credit inflation and $6/share NAV after claims. `expectedAssets` correctly decreases when claims happen.

**Current Contract**: `0x960eC492C1c9245dAe05bA4027d6e15ce0AD9d3D` (Base Mainnet)

Net Asset Value (NAV) uses liquidation value for positions, while `pendingCredit` uses cost basis for stability. A conservation bound (`totalAssets >= expectedAssets * (1 - maxLossBps)`) replaces a fixed percentage limit, allowing for trading PnL while safeguarding against artificial drops. Safety valves (e.g., `maxPendingAge`, `maxPendingRatio`) pause deposits under adverse conditions.

## Polymarket Trading Bot

This system automates Polymarket monitoring and trading.

**1. Market Monitoring (Mastra Workflow)**: A cron-triggered workflow (every minute) fetches new Polymarket markets, posts them to Telegram, filters out short-term "up/down" markets, and queues relevant markets for trading in a PostgreSQL database.

**2. Trading Job Worker (Python)**: A continuous worker polls the `trading_jobs` queue, places laddered buy orders (1¢-3¢ on YES/NO tokens) for new markets, and updates job status. This component requires a residential IP due to Cloudflare blocking datacenter IPs.

**3. Order Monitor (Python)**: Continuously monitors all active orders. It auto-cancels stale orders (>12 hours) to free up capital, places laddered sell orders upon fill (at 3x-10x profit), and sends Telegram notifications when sells execute. This also requires a residential IP.

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
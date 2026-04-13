# Overview

This project, operating under the PMFI brand, encompasses three main systems: the **pSNIPER Vault (V7.5)**, the **pARB Vault (V2)**, and an **Automated Polymarket Trading Bot**. The pSNIPER Vault provides secure, NAV-based share pricing using actual liquid asset values with withdrawal exclusion. The pARB Vault facilitates cross-venue arbitrage across Polymarket, Kalshi, and Opinion Labs using the Oddpool API, with liquid NAV derived from order-book bids. The Automated Polymarket Trading Bot offers fully automated market monitoring, Telegram notifications, and strategic trading to optimize participation on Polymarket. The overarching goal is to enhance efficiency, accuracy, and profitability in decentralized prediction markets.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Core Framework and Workflow

The application is built on the Mastra Framework, an AI-powered TypeScript framework designed for agent orchestration with LLMs. It utilizes a graph-based workflow engine for deterministic multi-step processes, a tool system for external interactions, and a three-tier memory management system (Conversation History, Semantic Recall, Working Memory). It supports multi-agent coordination and unified model routing. Workflows feature input/output validation (Zod), sequential and parallel execution, branching, and robust error handling with suspend/resume capabilities. Inngest provides durable workflow execution, step memoization, real-time monitoring, and a publish-subscribe event system. The system supports real-time streaming for incremental response generation and triggers via webhooks (Slack, Telegram) and cron workflows.

## pSNIPER Vault (V7.5)

The vault calculates Net Asset Value (NAV) based on actual liquid asset values, explicitly excluding pending withdrawals. This ensures that remaining liquidity providers see accurate pricing. Key features include an adjustment for funds in withdrawal bridge transit to prevent double-counting, a minimum withdrawal amount of $5, and the use of position liquidation values (mark-to-market) rather than cost basis for NAV calculations. The current contract is deployed on Base Mainnet.

## pARB Vault (V2)

This is an asynchronous, Yearn-style vault designed for cross-venue arbitrage. It integrates with Polymarket, Kalshi, and Opinion Labs via the Oddpool API. The vault's contract handles deposit and redemption requests via queuing, features a permissionless `tend()` function to refill idle buffers, and uses a keeper-only `report()` function for updating the official Price Per Share (PPS) and processing queues, applying a performance fee based on a high-water mark mechanism. A dedicated NAV Oracle tracks cash across all three platforms and calculates open position liquid values from orderbook bids. The execution logic prioritizes trades by P&L velocity, uses Polymarket CLOB token IDs, enforces per-pair and total capital caps, includes live price re-checks with slippage guards, and auto-unwinds trades if a leg fails. The current V2 contract is deployed on Base Mainnet.

## Polymarket Trading Bot

This system automates market monitoring and strategic trading on Polymarket. A cron-triggered workflow identifies new markets, posts them to Telegram, and queues relevant ones for trading. Market filtering is sophisticated, focusing on crypto/stock markets and employing NLP scoring to reduce false positives, considering safe/risky tickers, hard-finance keywords, and specific tags. A continuous worker places laddered buy orders (1¢-3¢) for new markets, requiring a residential IP. An order monitor continuously tracks active orders, auto-cancels stale ones, places laddered sell orders upon fill, and sends Telegram notifications. The sell strategy requires a minimum of 25 shares before placing orders, reserves 10% of the position for resolution, and uses a three-tiered laddered selling approach for the remaining shares.

## Replit-Specific Architecture

A custom Replit Playground UI provides user interaction and workflow graph visualization. The project leverages Mastra's deployer for Replit infrastructure, incorporates OpenTelemetry for observability, and utilizes a custom build system.

# External Dependencies

## AI Model Providers

-   **OpenAI**: Primary LLM provider.
-   **Anthropic, Google, xAI**: Supported via Mastra's unified routing.
-   **OpenRouter**: For accessing multiple models.
-   **Vercel AI SDK**: Core AI abstractions.

## Databases and Storage

-   **LibSQL**: Primary local/embedded database with vector support.
-   **PostgreSQL**: Production storage with `pgvector` extension.
-   **Vector Databases**: For semantic recall (LibSQL, Postgres with pgvector, or Upstash Vector).

## External Services

-   **Inngest**: Workflow orchestration for durable execution.
-   **Slack**: Bot integration.
-   **Telegram**: Bot webhook integration.
-   **Exa**: Search API integration.
-   **Oddpool API**: For pARB vault arbitrage data.
-   **Polymarket API**: For market data and trading.
-   **Kalshi API**: For market data and trading (RSA authenticated).
-   **Opinion Labs API**: For market data and CLOB access.

## Core Libraries

-   **Zod**: Schema validation.
-   **Pino**: Structured logging.
-   **TypeScript**: Type system.
-   **dotenv**: Environment variable management.

## Runtime Requirements

-   Node.js >=20.9.0.
-   Environment variables for API keys and database connections.
-   Webhook tokens for integrations.
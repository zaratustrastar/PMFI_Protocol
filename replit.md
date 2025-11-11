# Overview

This is a **Fully Automated Polymarket Monitoring and Trading System** that combines market detection, Telegram notifications, and automated trading in a queue-based architecture.

## Architecture

### 1. Market Monitoring (Mastra Workflow)
- **Trigger**: Time-based cron running every minute (`* * * * *`)
- **Workflow**:
  1. Fetches latest markets from Polymarket API
  2. Identifies new markets not yet seen
  3. Posts notifications to Telegram (@ponnymarket)
  4. **Queues markets for automated trading** in `trading_jobs` table
  5. Marks markets as seen in database
- **Database**: PostgreSQL tracks seen markets and trading jobs

### 2. Trading Job Worker (Python)
- **Mode**: Continuous worker polling `trading_jobs` queue
- **Process**:
  1. Polls database for PENDING jobs
  2. Places buy orders (10 orders × 2 sides = $2 per market)
  3. Marks job as COMPLETED/FAILED
  4. Moves to next job
- **Strategy**: Ladder buys 0.1¢-1¢ on YES and NO tokens ($0.20 per order)
- **Deployment**: Run `trading_bot/start_worker.sh` continuously

### 3. Order Monitor (Python)
- **Mode**: Continuous monitor for ALL active orders
- **Process**:
  1. Monitors all OPEN buy orders → places sell ladder when filled
  2. Monitors all OPEN sell orders → posts Telegram notification when filled
- **Sell Strategy**: Ladder sells at 3x-10x profit
- **Notifications**: Posts to Telegram (@ponnymarket) **ONLY when sells execute**
- **Deployment**: Run `trading_bot/start_monitor.sh` continuously

## Deployment

Three processes run concurrently:
1. **Mastra Workflow** (cron every minute): Detects markets → Posts to Telegram → Queues jobs
2. **Trading Worker** (`auto_trader.py`): Processes queued jobs → Places orders
3. **Order Monitor** (`order_monitor.py`): Monitors fills → Places sells → Notifies Telegram

Mastra is an all-in-one framework for building AI-powered applications with TypeScript, featuring agents that use LLMs and tools, graph-based workflows for orchestrated multi-step processes, and comprehensive memory management for conversation history and context.

The system is specifically configured for the Replit environment with a custom playground UI and Inngest integration for durable workflow execution.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Core Framework

**Mastra Framework**: The application is built on Mastra (`@mastra/core`), an all-in-one TypeScript framework for AI applications that provides:
- Agent orchestration with LLM reasoning capabilities
- Graph-based workflow engine for deterministic multi-step processes
- Tool system for external API calls and custom functions
- Memory management with conversation history and semantic recall

**Runtime Environment**: Node.js >=20.9.0 with ES2022 modules, configured for modern TypeScript with strict type checking.

## Agent Architecture

**Agent System**: Agents use LLMs to solve open-ended tasks through reasoning, tool selection, and iterative execution. Key components:
- System instructions define agent personality and behavior
- Tools extend capabilities beyond text generation
- Memory provides conversation context across interactions
- Guardrails with input/output processors for content moderation

**Agent Networks**: Multi-agent coordination through routing agents that delegate tasks to specialized agents, workflows, and tools based on LLM reasoning rather than predefined sequences.

**Model Routing**: Unified interface supporting 800+ models from 47+ providers (OpenAI, Anthropic, Google, xAI, etc.) through simple `"provider/model-name"` syntax with automatic API key detection.

## Workflow Architecture

**Deterministic Workflows**: Graph-based workflow engine for tasks with clear execution sequences:
- Steps defined with input/output schemas using Zod validation
- Composition through `.then()` for sequential, `.parallel()` for concurrent execution
- Branching with `.map()` for data transformation between steps
- Error handling with configurable retry policies

**Suspend/Resume**: Workflows can pause execution and persist state as snapshots, enabling:
- Human-in-the-loop interactions
- External resource waiting
- Event-driven processes
- Multi-turn conversations

**Inngest Integration**: Durable workflow execution layer providing:
- Step memoization for efficient retries
- Real-time monitoring and observability
- Publish-subscribe event system
- Production-grade infrastructure management

## Memory System

**Three-Tier Memory**:
1. **Conversation History**: Recent messages (configurable, default 10) for short-term context
2. **Semantic Recall**: RAG-based vector search for retrieving relevant past messages
3. **Working Memory**: Persistent user data and preferences (thread or resource-scoped)

**Memory Scoping**: Two-level system with thread identifiers for conversation isolation and resource identifiers for user/entity-level persistence.

**Storage Adapters**: Pluggable storage backends supporting LibSQL, PostgreSQL with pgvector, and Upstash Redis/Vector.

## Tool System

**Tool Architecture**: Functions that extend agent capabilities with:
- Zod schemas for input/output validation
- Custom execution logic for API calls, database queries, or code execution
- Abort signal support for cancellable operations
- Streaming capabilities for incremental results

**Tool Integration**: Tools can be called from agents, workflow steps, or composed directly as workflow steps using `createStep()`.

## Streaming Architecture

**Real-time Streaming**: Incremental response generation for both agents and workflows:
- Text deltas for progressive content rendering
- Tool call/result events for transparency
- Step start/finish events for workflow progress
- Network events for multi-agent orchestration

**Stream Writers**: Writable streams passed to tools and workflow steps enabling custom event emission and agent-to-tool stream piping.

## Trigger System

**Webhook Triggers**: Event-driven automation through connector webhooks:
- Slack message triggers with conversation threading
- Telegram bot integration
- Generic connector pattern in `src/triggers/` for extensibility

**Cron Workflows**: Time-based triggers registered through Inngest for scheduled automations.

**API Routes**: Custom REST endpoints registered via `registerApiRoute()` for webhook handlers.

## Replit-Specific Architecture

**Replit Playground UI**: Custom UI for Replit environment (separate from Mastra Playground):
- User-only interaction interface
- Workflow graph visualization with plain English node descriptions
- Requires `generateLegacy()` for backwards compatibility (not SDK v5 `.generate()`)

**Deployment Layer**: Mastra deployer integration for publishing automations on Replit infrastructure with OpenTelemetry instrumentation for observability.

**Build System**: Custom module resolution and build configuration in `.mastra/` directory with TypeScript compilation to ES2022 modules.

## Logging and Observability

**Logger Architecture**: Pluggable logging system with PinoLogger implementation:
- Structured JSON logging with ISO timestamps
- Configurable log levels (debug, info, warn, error)
- Production-optimized formatting

**Error Handling**: Non-retriable errors via Inngest for critical failures, with configurable retry policies at workflow and step levels.

# External Dependencies

## AI Model Providers

- **OpenAI**: Primary LLM provider via `@ai-sdk/openai` and `openai` SDK
- **Anthropic, Google, xAI**: Additional model providers through Mastra's unified routing
- **OpenRouter**: AI SDK provider for accessing multiple models through single gateway
- **Vercel AI SDK**: Core AI abstractions (`ai` package v4.x for compatibility)

## Databases and Storage

- **LibSQL**: Primary storage adapter via `@mastra/libsql` for local/embedded database with vector support
- **PostgreSQL**: Production storage option via `@mastra/pg` requiring pgvector extension
- **Vector Databases**: Semantic recall requires vector storage (LibSQL, Postgres with pgvector, or Upstash Vector)

## External Services

- **Inngest**: Workflow orchestration platform for durable execution
  - `inngest` client library
  - `inngest-cli` for development
  - `@mastra/inngest` adapter
  - `@inngest/realtime` for streaming/monitoring
- **Slack**: Bot integration via `@slack/web-api`
- **Telegram**: Bot webhook integration (token-based authentication)
- **Exa**: Search API integration via `exa-js`

## Core Libraries

- **Zod**: Schema validation for all inputs/outputs
- **Pino**: Structured logging
- **TypeScript**: Type system and compilation
- **dotenv**: Environment variable management

## Development Tools

- **tsx**: TypeScript execution runtime
- **Prettier**: Code formatting
- **Mastra CLI**: Development server and build tooling

## MCP (Model Context Protocol)

- **@mastra/mcp**: Integration for Model Context Protocol servers

## Runtime Requirements

- Node.js >=20.9.0
- Environment variables for API keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
- Database connection strings when using PostgreSQL or remote LibSQL
- Webhook tokens for Slack/Telegram integrations
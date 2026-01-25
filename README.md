# pSNIPER Vault

An automated prediction market trading vault built on Base. Deposits USDC, trades on Polymarket, returns profits to shareholders.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         SYSTEM OVERVIEW                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐       ┌──────────────┐       ┌──────────────┐ │
│  │   User       │       │   Vault      │       │  Polymarket  │ │
│  │   (Base)     │──────▶│   (Base)     │──────▶│  (Polygon)   │ │
│  │              │       │              │       │              │ │
│  │  Deposit     │       │  pSNIPER     │       │  Trading     │ │
│  │  USDC        │       │  Shares      │       │  Execution   │ │
│  └──────────────┘       └──────────────┘       └──────────────┘ │
│                                                                  │
│  Components:                                                     │
│  • Smart Contract - ERC4626 vault with NAV oracle               │
│  • NAV Bot - Calculates real-time asset values                  │
│  • Withdrawal Servicer - Processes withdrawal requests          │
│  • Trading Bot - Executes market sniping strategy               │
│  • Order Monitor - Tracks fills and profit taking               │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Smart Contract

| Contract | Network | Address |
|----------|---------|---------|
| pSNIPER Vault | Base Mainnet | [`0x960eC492C1c9245dAe05bA4027d6e15ce0AD9d3D`](https://basescan.org/address/0x960eC492C1c9245dAe05bA4027d6e15ce0AD9d3D) |

### Key Features

- **NAV-based pricing** - Fair value calculated from real orderbook depth
- **Locked withdrawals** - Price fixed when you request, no slippage
- **3-state asset tracking** - In-flight, pending, credited funds all counted
- **Conservative valuation** - Illiquid positions valued at $0

## Project Structure

```
├── bot/                 # NAV oracle
│   └── nav_oracle.py    # Price calculation and signing
├── trading_bot/         # Polymarket trading workers
│   ├── withdrawal_servicer.py  # Withdrawal processing
│   ├── auto_trader.py          # Order placement
│   ├── order_monitor.py        # Fill tracking
│   ├── market_monitor.py       # Market discovery
│   └── polymarket_trader.py    # Core trading logic
└── src/mastra/          # Workflow orchestration
    ├── workflows/       # Scheduled market monitoring
    ├── tools/           # Telegram, Twitter, market fetching
    └── agents/          # AI agent for market analysis
```

## Setup

### Prerequisites

- Node.js 20+
- Python 3.11+
- PostgreSQL database
- Polymarket API credentials

### Installation

```bash
# Install Node dependencies
npm install

# Install Python dependencies
pip install -r trading_bot/requirements.txt
```

### Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required environment variables:
- `DATABASE_URL` - PostgreSQL connection string
- `POLYMARKET_PRIVATE_KEY` - Trading wallet private key
- `POLYMARKET_PROXY_ADDRESS` - Polymarket proxy wallet
- `TELEGRAM_BOT_TOKEN` - For notifications

## Running

### NAV Oracle

```bash
cd bot
python nav_oracle.py
```

Runs on port 8080. Provides endpoints:
- `/health` - Health check
- `/price` - Current share price
- `/sign-nav` - Signed NAV data for contract

### Withdrawal Servicer

```bash
cd trading_bot
python withdrawal_servicer.py
```

Monitors pending withdrawals and processes them automatically.

### Trading Bot

```bash
cd trading_bot
python auto_trader.py
```

Places orders on new markets based on configured strategy.

### Order Monitor

```bash
cd trading_bot
python order_monitor.py
```

Tracks order fills and places profit-taking sells.

## License

MIT

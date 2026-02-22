-- ============================================================
-- pSNIPER Trading Database Setup Script
-- Run on VPS local PostgreSQL (psniper database)
-- Usage: psql -U psniper -d psniper -f setup_trading_tables.sql
-- ============================================================

-- Trading jobs queue - markets waiting to be traded
CREATE TABLE IF NOT EXISTS trading_jobs (
    id SERIAL PRIMARY KEY,
    market_id TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    error_message TEXT,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    market_created_at TIMESTAMP,
    market_closed_time TIMESTAMP,
    event_slug TEXT,
    question TEXT,
    clob_token_ids TEXT,
    outcomes TEXT
);

-- Trading positions table
CREATE TABLE IF NOT EXISTS trading_positions (
    id SERIAL PRIMARY KEY,
    market_slug TEXT NOT NULL,
    market_question TEXT,
    order_id TEXT UNIQUE NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    price DECIMAL(10, 6) NOT NULL,
    size DECIMAL(18, 6) NOT NULL,
    status TEXT NOT NULL,
    buy_price DECIMAL(10, 6),
    profit_multiple DECIMAL(10, 2),
    filled_at TIMESTAMP,
    accumulated BOOLEAN DEFAULT FALSE,
    accumulated_amount DECIMAL(18, 6) DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Trading summary table
CREATE TABLE IF NOT EXISTS trading_summary (
    id SERIAL PRIMARY KEY,
    market_slug TEXT NOT NULL UNIQUE,
    total_buys INTEGER DEFAULT 0,
    total_sells INTEGER DEFAULT 0,
    filled_buys INTEGER DEFAULT 0,
    filled_sells INTEGER DEFAULT 0,
    total_invested DECIMAL(18, 2) DEFAULT 0,
    total_returned DECIMAL(18, 2) DEFAULT 0,
    realized_pnl DECIMAL(18, 2) DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Accumulated fills table - tracks total filled shares per token for sell threshold
CREATE TABLE IF NOT EXISTS accumulated_fills (
    id SERIAL PRIMARY KEY,
    market_slug TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    total_shares DECIMAL(18, 6) DEFAULT 0,
    total_cost DECIMAL(18, 6) DEFAULT 0,
    avg_buy_price DECIMAL(10, 6) DEFAULT 0,
    sell_placed BOOLEAN DEFAULT FALSE,
    shares_with_sells DECIMAL(18, 6) DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(market_slug, token_id, side)
);

-- Seen markets table (used by Mastra workflow to avoid duplicate posts)
CREATE TABLE IF NOT EXISTS seen_polymarket_markets (
    market_id TEXT PRIMARY KEY,
    seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Seen events table (for Telegram - one post per event)
CREATE TABLE IF NOT EXISTS seen_polymarket_events (
    event_slug TEXT PRIMARY KEY,
    seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Seen trading conditions table (for trading - track each sub-market)
CREATE TABLE IF NOT EXISTS seen_trading_conditions (
    condition_id TEXT PRIMARY KEY,
    event_slug TEXT,
    seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_trading_jobs_status ON trading_jobs(status);
CREATE INDEX IF NOT EXISTS idx_trading_jobs_created ON trading_jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_trading_positions_status ON trading_positions(status);
CREATE INDEX IF NOT EXISTS idx_trading_positions_market ON trading_positions(market_slug);
CREATE INDEX IF NOT EXISTS idx_accumulated_fills_market ON accumulated_fills(market_slug);

SELECT 'Trading tables initialized successfully' AS result;

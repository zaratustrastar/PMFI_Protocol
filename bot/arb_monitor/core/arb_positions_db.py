"""Arb position database management.

Initializes and manages:
- arb_positions: open/settled arbitrage position tracking
- arb_executions: per-leg execution log
- arb_vault_nav: periodic NAV snapshots

Enforces:
- Per-pair cap: MAX_PAIR_USDC (default $500)
- Total deployed cap: MAX_DEPLOYED_USDC (configurable)
- Capital prioritization: highest pnl_velocity gets capital first
"""

import os
import time
from typing import Optional


def log(msg: str):
    print(f"🗄️ [Arb/PositionsDB] {msg}")


def get_db_conn():
    """Get a database connection."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        log("⚠️ DATABASE_URL not set")
        return None
    try:
        import psycopg2
        return psycopg2.connect(database_url)
    except Exception as e:
        log(f"❌ DB connection error: {e}")
        return None


def init_arb_tables():
    """Create arb-specific DB tables if they don't exist."""
    conn = get_db_conn()
    if not conn:
        log("⚠️ Cannot init tables: no DB connection")
        return False
    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS arb_positions (
                id SERIAL PRIMARY KEY,
                pair_id VARCHAR(255) NOT NULL UNIQUE,
                poly_yes_token VARCHAR(255) NOT NULL,
                poly_no_token VARCHAR(255) DEFAULT '',
                kalshi_ticker VARCHAR(255) NOT NULL,
                shares DECIMAL(20, 8) NOT NULL DEFAULT 0,
                cost_basis_usdc DECIMAL(20, 6) NOT NULL DEFAULT 0,
                expiry_ts BIGINT NOT NULL DEFAULT 0,
                status VARCHAR(20) NOT NULL DEFAULT 'open',
                settled_pnl_usdc DECIMAL(20, 6) DEFAULT 0,
                opened_at TIMESTAMP DEFAULT NOW(),
                settled_at TIMESTAMP,
                poly_title TEXT DEFAULT '',
                kalshi_title TEXT DEFAULT '',
                notes TEXT DEFAULT ''
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS arb_executions (
                id SERIAL PRIMARY KEY,
                pair_id VARCHAR(255) NOT NULL,
                leg INTEGER NOT NULL,
                venue VARCHAR(50) NOT NULL,
                side VARCHAR(20) NOT NULL,
                price DECIMAL(10, 6) NOT NULL DEFAULT 0,
                size DECIMAL(20, 6) NOT NULL DEFAULT 0,
                success BOOLEAN NOT NULL DEFAULT FALSE,
                error TEXT DEFAULT '',
                order_id VARCHAR(255) DEFAULT '',
                executed_at TIMESTAMP DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS arb_vault_nav (
                id SERIAL PRIMARY KEY,
                computed_at TIMESTAMP DEFAULT NOW(),
                total_assets_usdc DECIMAL(20, 6) NOT NULL DEFAULT 0,
                poly_cash DECIMAL(20, 6) NOT NULL DEFAULT 0,
                kalshi_cash DECIMAL(20, 6) NOT NULL DEFAULT 0,
                open_positions_value DECIMAL(20, 6) NOT NULL DEFAULT 0,
                settled_pnl DECIMAL(20, 6) NOT NULL DEFAULT 0,
                round_id BIGINT NOT NULL UNIQUE,
                signature TEXT DEFAULT ''
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_arb_positions_status ON arb_positions(status)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_arb_executions_pair_id ON arb_executions(pair_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_arb_vault_nav_round_id ON arb_vault_nav(round_id)
        """)

        conn.commit()
        cur.close()
        log("✅ Arb tables initialized")
        return True
    except Exception as e:
        log(f"❌ Error initializing arb tables: {e}")
        return False
    finally:
        conn.close()


def get_total_deployed_usdc() -> float:
    """Get total USDC currently deployed in open arb positions."""
    conn = get_db_conn()
    if not conn:
        return 0.0
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(cost_basis_usdc), 0)
            FROM arb_positions
            WHERE status = 'open'
        """)
        row = cur.fetchone()
        cur.close()
        return float(row[0]) if row else 0.0
    except Exception as e:
        log(f"⚠️ Error fetching deployed USDC: {e}")
        return 0.0
    finally:
        conn.close()


def get_pair_deployed_usdc(pair_id: str) -> float:
    """Get USDC deployed in a specific arb pair."""
    conn = get_db_conn()
    if not conn:
        return 0.0
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(cost_basis_usdc, 0)
            FROM arb_positions
            WHERE pair_id = %s AND status = 'open'
        """, (pair_id,))
        row = cur.fetchone()
        cur.close()
        return float(row[0]) if row else 0.0
    except Exception as e:
        log(f"⚠️ Error fetching pair deployed USDC: {e}")
        return 0.0
    finally:
        conn.close()


def can_deploy_capital(pair_id: str, amount_usdc: float) -> tuple[bool, str]:
    """Check if we can deploy capital into a pair.

    Enforces:
    - Per-pair cap (MAX_PAIR_USDC)
    - Total vault cap (MAX_DEPLOYED_USDC)
    """
    from ..config import ARB_MAX_PAIR_USDC, ARB_MAX_DEPLOYED_USDC

    pair_deployed = get_pair_deployed_usdc(pair_id)
    if pair_deployed + amount_usdc > ARB_MAX_PAIR_USDC:
        return False, (
            f"pair_cap_exceeded: pair_id={pair_id} "
            f"current={pair_deployed:.2f} + new={amount_usdc:.2f} > max={ARB_MAX_PAIR_USDC:.2f}"
        )

    total_deployed = get_total_deployed_usdc()
    if total_deployed + amount_usdc > ARB_MAX_DEPLOYED_USDC:
        return False, (
            f"total_cap_exceeded: total_deployed={total_deployed:.2f} + "
            f"new={amount_usdc:.2f} > max={ARB_MAX_DEPLOYED_USDC:.2f}"
        )

    return True, ""


def upsert_position(
    pair_id: str,
    poly_yes_token: str,
    kalshi_ticker: str,
    shares: float,
    cost_basis_usdc: float,
    expiry_ts: int,
    status: str = "open",
    poly_no_token: str = "",
    poly_title: str = "",
    kalshi_title: str = "",
) -> bool:
    """Create or update an arb position record."""
    conn = get_db_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO arb_positions
                (pair_id, poly_yes_token, poly_no_token, kalshi_ticker, shares,
                 cost_basis_usdc, expiry_ts, status, poly_title, kalshi_title)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s, %s)
            ON CONFLICT (pair_id) DO UPDATE SET
                shares = arb_positions.shares + EXCLUDED.shares,
                cost_basis_usdc = arb_positions.cost_basis_usdc + EXCLUDED.cost_basis_usdc
        """, (pair_id, poly_yes_token, poly_no_token, kalshi_ticker, shares,
              cost_basis_usdc, expiry_ts, poly_title, kalshi_title))
        conn.commit()
        cur.close()
        log(f"✅ Position upserted: pair_id={pair_id} shares={shares} cost={cost_basis_usdc}")
        return True
    except Exception as e:
        log(f"❌ Error upserting position: {e}")
        return False
    finally:
        conn.close()


def settle_position(pair_id: str, settled_pnl_usdc: float) -> bool:
    """Mark an arb position as settled with final PnL."""
    conn = get_db_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE arb_positions
            SET status = 'settled', settled_pnl_usdc = %s, settled_at = NOW()
            WHERE pair_id = %s AND status = 'open'
        """, (settled_pnl_usdc, pair_id))
        conn.commit()
        cur.close()
        log(f"✅ Position settled: pair_id={pair_id} pnl={settled_pnl_usdc:.4f} USDC")
        return True
    except Exception as e:
        log(f"❌ Error settling position: {e}")
        return False
    finally:
        conn.close()


def get_open_positions() -> list[dict]:
    """Get all open arb positions."""
    conn = get_db_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pair_id, poly_yes_token, poly_no_token, kalshi_ticker,
                   shares, cost_basis_usdc, expiry_ts, status,
                   COALESCE(settled_pnl_usdc, 0), opened_at,
                   COALESCE(poly_title, ''), COALESCE(kalshi_title, '')
            FROM arb_positions
            WHERE status = 'open'
            ORDER BY opened_at DESC
        """)
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "pair_id": r[0],
                "poly_yes_token": r[1],
                "poly_no_token": r[2],
                "kalshi_ticker": r[3],
                "shares": float(r[4]),
                "cost_basis_usdc": float(r[5]),
                "expiry_ts": int(r[6]),
                "status": r[7],
                "settled_pnl_usdc": float(r[8]),
                "opened_at": r[9].isoformat() if r[9] else None,
                "poly_title": r[10],
                "kalshi_title": r[11],
            }
            for r in rows
        ]
    except Exception as e:
        log(f"❌ Error fetching open positions: {e}")
        return []
    finally:
        conn.close()


def get_executions(pair_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Get recent execution logs."""
    conn = get_db_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        if pair_id:
            cur.execute("""
                SELECT pair_id, leg, venue, side, price, size, success, error, order_id, executed_at
                FROM arb_executions
                WHERE pair_id = %s
                ORDER BY executed_at DESC
                LIMIT %s
            """, (pair_id, limit))
        else:
            cur.execute("""
                SELECT pair_id, leg, venue, side, price, size, success, error, order_id, executed_at
                FROM arb_executions
                ORDER BY executed_at DESC
                LIMIT %s
            """, (limit,))
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "pair_id": r[0],
                "leg": r[1],
                "venue": r[2],
                "side": r[3],
                "price": float(r[4]),
                "size": float(r[5]),
                "success": r[6],
                "error": r[7],
                "order_id": r[8],
                "executed_at": r[9].isoformat() if r[9] else None,
            }
            for r in rows
        ]
    except Exception as e:
        log(f"❌ Error fetching executions: {e}")
        return []
    finally:
        conn.close()

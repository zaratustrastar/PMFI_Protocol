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


_DB_CONNECT_RETRIES = 3
_DB_CONNECT_BACKOFF = 2  # seconds between attempts


def get_db_conn():
    """Get a database connection with retry logic.

    Attempts up to _DB_CONNECT_RETRIES times with _DB_CONNECT_BACKOFF seconds
    between each attempt. Emits a loud ❌ DB DOWN alert if all retries fail so
    operators know state is being lost — callers receive None and must treat
    their write/read as a no-op (same as before, but now it's obvious in logs).
    """
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        log("⚠️ DATABASE_URL not set — no DB connection possible")
        return None
    last_err = None
    for attempt in range(1, _DB_CONNECT_RETRIES + 1):
        try:
            import psycopg2
            conn = psycopg2.connect(database_url)
            if attempt > 1:
                log(f"✅ DB reconnected on attempt {attempt}")
            return conn
        except Exception as e:
            last_err = e
            if attempt < _DB_CONNECT_RETRIES:
                log(f"⚠️ DB connect attempt {attempt}/{_DB_CONNECT_RETRIES} failed: {e} — retrying in {_DB_CONNECT_BACKOFF}s")
                time.sleep(_DB_CONNECT_BACKOFF)
    log(
        f"❌ DB DOWN — all {_DB_CONNECT_RETRIES} connection attempts failed. "
        f"Last error: {last_err}. "
        "Bot is running WITHOUT persistence — position state will be lost on restart. "
        "Restart PostgreSQL and verify DATABASE_URL."
    )
    return None


# Position lifecycle.
#   open            both legs held; valued at cost basis
#   pending_redeem  (robinhood mode) market resolved, winning leg's payout NOT yet observed as
#                   venue cash; still valued at cost basis
#   redeemed        (robinhood mode) payout observed in venue cash; no position value
#   settled         legacy base-mode terminal state (unchanged)
STATUS_OPEN = "open"
STATUS_PENDING_REDEEM = "pending_redeem"
STATUS_REDEEMED = "redeemed"
STATUS_SETTLED = "settled"

_SCHEMA_ADDITIONS_SQL = """
    ALTER TABLE arb_positions
        ADD COLUMN IF NOT EXISTS poly_entry_price DECIMAL(10, 6) DEFAULT 0,
        ADD COLUMN IF NOT EXISTS kalshi_entry_price DECIMAL(10, 6) DEFAULT 0,
        ADD COLUMN IF NOT EXISTS poly_side VARCHAR(3) DEFAULT 'YES',
        ADD COLUMN IF NOT EXISTS resolved_venue VARCHAR(20) DEFAULT '',
        ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS payout_usdc DECIMAL(20, 6)
"""


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
                kalshi_side VARCHAR(3) NOT NULL DEFAULT 'YES',
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
        # Add kalshi_side column to existing tables that may predate this schema
        cur.execute("""
            ALTER TABLE arb_positions
            ADD COLUMN IF NOT EXISTS kalshi_side VARCHAR(3) NOT NULL DEFAULT 'YES'
        """)
        # Columns written by upsert_position() but missing from the original CREATE TABLE,
        # plus the robinhood-mode settlement lifecycle (open -> pending_redeem -> redeemed).
        # Robinhood mode only: the live Base V2 database is left untouched.
        from ..config import is_robinhood_funding
        if is_robinhood_funding():
            cur.execute(_SCHEMA_ADDITIONS_SQL)

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
    kalshi_side: str = "YES",
    poly_entry_price: float = 0.0,
    kalshi_entry_price: float = 0.0,
    poly_side: str = "YES",
) -> bool:
    """Create or update an arb position record.

    kalshi_side: "YES" or "NO" — which side was purchased on Kalshi.
    This is critical for NAV liquid value calculation and unwind logic.
    """
    if kalshi_side not in ("YES", "NO"):
        log(f"⚠️ Invalid kalshi_side={kalshi_side!r}, defaulting to 'YES'")
        kalshi_side = "YES"
    conn = get_db_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        # H3: a pair_id whose previous position is no longer open (settled / redeemed /
        # pending_redeem) must not absorb a NEW trade into a closed row (ON CONFLICT used to
        # add shares to it while leaving status != 'open', so the new capital vanished from
        # NAV). Archive the closed row under a unique id first; it keeps its PnL history.
        cur.execute("""
            UPDATE arb_positions
            SET pair_id = pair_id || '#closed-' || id
            WHERE pair_id = %s AND status <> 'open'
        """, (pair_id,))
        cur.execute("""
            INSERT INTO arb_positions
                (pair_id, poly_yes_token, poly_no_token, kalshi_ticker, kalshi_side, shares,
                 cost_basis_usdc, expiry_ts, status, poly_title, kalshi_title,
                 poly_entry_price, kalshi_entry_price, poly_side)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s)
            ON CONFLICT (pair_id) DO UPDATE SET
                shares = arb_positions.shares + EXCLUDED.shares,
                cost_basis_usdc = arb_positions.cost_basis_usdc + EXCLUDED.cost_basis_usdc,
                kalshi_side = EXCLUDED.kalshi_side,
                poly_entry_price = EXCLUDED.poly_entry_price,
                kalshi_entry_price = EXCLUDED.kalshi_entry_price,
                poly_side = EXCLUDED.poly_side,
                status = 'open'
        """, (pair_id, poly_yes_token, poly_no_token, kalshi_ticker, kalshi_side, shares,
              cost_basis_usdc, expiry_ts, poly_title, kalshi_title,
              poly_entry_price, kalshi_entry_price, poly_side))
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


def mark_position_resolved(pair_id: str, resolved_venue: str, expected_payout_usdc: float) -> bool:
    """open -> pending_redeem (robinhood mode). The position keeps being valued at cost basis
    until mark_position_redeemed() records that its payout is in venue cash."""
    if resolved_venue not in ("polymarket", "kalshi"):
        raise ValueError(f"resolved_venue must be polymarket|kalshi, got {resolved_venue!r}")
    conn = get_db_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE arb_positions
            SET status = 'pending_redeem', resolved_venue = %s, resolved_at = NOW(),
                payout_usdc = %s
            WHERE pair_id = %s AND status = 'open'
        """, (resolved_venue, expected_payout_usdc, pair_id))
        changed = cur.rowcount
        conn.commit()
        cur.close()
        log(f"✅ Position resolved (pending redeem): pair_id={pair_id} venue={resolved_venue} "
            f"expected_payout={expected_payout_usdc:.4f}")
        return changed > 0
    except Exception as e:
        log(f"❌ Error marking position resolved: {e}")
        return False
    finally:
        conn.close()


def mark_position_redeemed(pair_id: str, payout_usdc: float) -> bool:
    """pending_redeem -> redeemed. Call ONLY once the payout is visible in the venue cash
    balance the reporter reads (otherwise NAV drops by the cost basis until it lands).
    settled_pnl_usdc = payout - cost basis."""
    conn = get_db_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE arb_positions
            SET status = 'redeemed', payout_usdc = %s,
                settled_pnl_usdc = %s - cost_basis_usdc, settled_at = NOW()
            WHERE pair_id = %s AND status = 'pending_redeem'
        """, (payout_usdc, payout_usdc, pair_id))
        changed = cur.rowcount
        conn.commit()
        cur.close()
        log(f"✅ Position redeemed: pair_id={pair_id} payout={payout_usdc:.4f}")
        return changed > 0
    except Exception as e:
        log(f"❌ Error marking position redeemed: {e}")
        return False
    finally:
        conn.close()


def get_positions_by_status(statuses: tuple) -> list[dict]:
    """Minimal rows (pair_id, status, venue, shares, cost basis, tokens) for the given statuses."""
    conn = get_db_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        placeholders = ", ".join(["%s"] * len(statuses))
        cur.execute(f"""
            SELECT pair_id, status, COALESCE(resolved_venue, ''), shares, cost_basis_usdc,
                   poly_yes_token, kalshi_ticker, COALESCE(kalshi_side, 'YES'), expiry_ts
            FROM arb_positions
            WHERE status IN ({placeholders})
        """, tuple(statuses))
        rows = cur.fetchall()
        cur.close()
        return [{"pair_id": r[0], "status": r[1], "resolved_venue": r[2], "shares": float(r[3]),
                 "cost_basis_usdc": float(r[4]), "poly_yes_token": r[5], "kalshi_ticker": r[6],
                 "kalshi_side": r[7], "expiry_ts": int(r[8] or 0)} for r in rows]
    except Exception as e:
        log(f"❌ Error fetching positions by status: {e}")
        return []
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
                   COALESCE(kalshi_side, 'YES'),
                   shares, cost_basis_usdc, expiry_ts, status,
                   COALESCE(settled_pnl_usdc, 0), opened_at,
                   COALESCE(poly_title, ''), COALESCE(kalshi_title, ''),
                   COALESCE(poly_entry_price, 0), COALESCE(kalshi_entry_price, 0),
                   COALESCE(poly_side, 'YES')
            FROM arb_positions
            WHERE status = 'open'
            ORDER BY opened_at DESC
        """)
        rows = cur.fetchall()
        cur.close()
        import time as _time
        now = _time.time()
        results = []
        for r in rows:
            poly_price = float(r[13])
            kalshi_price = float(r[14])
            shares_val = float(r[5])
            expiry = int(r[7])
            edge = round(1.0 - poly_price - kalshi_price, 4) if (poly_price > 0 and kalshi_price > 0) else 0.0
            days_to_expiry = max(0.0, (expiry - now) / 86400) if expiry > now else 0.0
            cost_per_pair = poly_price + kalshi_price
            apr = round((edge / cost_per_pair) * (365.0 / max(days_to_expiry, 1.0)) * 100, 2) if cost_per_pair > 0 else 0.0
            locked_pnl = round(shares_val * edge, 4)
            results.append({
                "pair_id": r[0],
                "poly_yes_token": r[1],
                "poly_no_token": r[2],
                "kalshi_ticker": r[3],
                "kalshi_side": r[4],
                "shares": shares_val,
                "cost_basis_usdc": float(r[6]),
                "expiry_ts": expiry,
                "status": r[8],
                "settled_pnl_usdc": float(r[9]),
                "opened_at": r[10].isoformat() if r[10] else None,
                "poly_title": r[11],
                "kalshi_title": r[12],
                "poly_entry_price": poly_price,
                "kalshi_entry_price": kalshi_price,
                "poly_side": r[15],
                "edge": edge,
                "edge_pct": round(edge * 100, 2),
                "apr": apr,
                "locked_pnl_usdc": locked_pnl,
                "days_to_expiry": round(days_to_expiry, 1),
            })
        return results
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

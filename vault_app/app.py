"""
pSNIPER / pARB Vault Flask App
Run: python app.py
"""

import os
import json
import time
import datetime
import requests
from flask import Flask, render_template, send_from_directory, redirect, request, jsonify
from flask_cors import CORS
from invite_routes import invite_bp
import psycopg2
import psycopg2.extras

app = Flask(__name__,
            template_folder='templates',
            static_folder='static')
CORS(app)

app.register_blueprint(invite_bp)

DATABASE_URL = os.getenv('DATABASE_URL')
ODDPOOL_API_KEY = os.getenv('ODDPOOL_API_KEY', os.getenv('OPINION_API_KEY', ''))
ODDPOOL_BASE_URL = 'https://oddpool.ai/api'


def _get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_database():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                id SERIAL PRIMARY KEY,
                code_hash VARCHAR(64) NOT NULL UNIQUE,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by VARCHAR(255),
                expires_at TIMESTAMP,
                redeemed_by VARCHAR(42),
                redeemed_at TIMESTAMP,
                note TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS invite_audit_logs (
                id SERIAL PRIMARY KEY,
                action VARCHAR(50) NOT NULL,
                code_hash VARCHAR(64),
                wallet_address VARCHAR(42),
                ip_address VARCHAR(45),
                success BOOLEAN NOT NULL,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS whitelisted_wallets (
                wallet_address VARCHAR(42) PRIMARY KEY,
                code_id INTEGER REFERENCES invite_codes(id),
                whitelisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        cur.close()
        conn.close()
        print("✅ Database tables initialized")
    except Exception as e:
        print(f"❌ Database init error: {e}")


def is_wallet_whitelisted(wallet):
    if not wallet:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT wallet_address FROM whitelisted_wallets WHERE wallet_address = %s", (wallet.lower(),))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result is not None
    except:
        return False


# ── Page routes ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/vault')
def vault():
    return render_template('vault.html',
        vault_address=os.getenv('VAULT_V7_ADDRESS', '0x17C27001929E75D1eBd5FdeE6E986EA5a91de0D1')
    )


@app.route('/arbitrage')
def arbitrage():
    return render_template('arbitrage.html',
        vault_address=os.getenv('ARB_VAULT_V2_ADDRESS', os.getenv('ARB_VAULT_V1_ADDRESS', 'None'))
    )


@app.route('/admin')
def admin():
    return render_template('admin.html')


@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)


# ── pARB API: NAV ─────────────────────────────────────────────────────────────

@app.route('/api/arb-vault/nav')
def arb_nav():
    """Return latest NAV snapshot including TVL, share price, APR, APY."""
    try:
        conn = _get_db()
        cur = conn.cursor()

        # Latest NAV snapshot
        cur.execute("""
            SELECT total_assets_usdc, poly_cash, kalshi_cash, open_positions_value,
                   settled_pnl, round_id, signature, computed_at
            FROM arb_vault_nav
            ORDER BY id DESC
            LIMIT 1
        """)
        row = cur.fetchone()

        if not row:
            conn.close()
            return jsonify({
                "ok": False,
                "total_assets_usdc": 0,
                "tvl_usdc": 0,
                "poly_cash": 0,
                "kalshi_cash": 0,
                "open_positions_value": 0,
                "settled_pnl": 0,
                "share_price": 1.0,
                "apr": None,
                "apy": None,
                "timestamp": int(time.time()),
                "deadline": int(time.time()) + 300,
                "round_id": 0,
                "signature": "",
                "message": "No NAV data yet"
            })

        total_assets = float(row['total_assets_usdc'])
        computed_ts = int(row['computed_at'].timestamp()) if row['computed_at'] else int(time.time())

        # APR / APY from settled positions
        apr = _compute_apr(cur)
        apy = ((1 + apr / 100 / 365) ** 365 - 1) * 100 if apr else None

        # Historical PPS rows for share price trend (last 30 rows)
        cur.execute("""
            SELECT total_assets_usdc, computed_at
            FROM arb_vault_nav
            ORDER BY id DESC
            LIMIT 30
        """)
        history = cur.fetchall()
        conn.close()

        return jsonify({
            "ok": True,
            "total_assets_usdc": total_assets,
            "tvl_usdc": total_assets,
            "poly_cash": float(row['poly_cash']),
            "kalshi_cash": float(row['kalshi_cash']),
            "open_positions_value": float(row['open_positions_value']),
            "settled_pnl": float(row['settled_pnl']),
            "share_price": 1.0,
            "apr": round(apr, 2) if apr is not None else None,
            "apy": round(apy, 2) if apy is not None else None,
            "timestamp": computed_ts,
            "deadline": computed_ts + 300,
            "round_id": int(row['round_id']),
            "signature": row['signature'] or "",
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "total_assets_usdc": 0,
                        "tvl_usdc": 0, "poly_cash": 0, "kalshi_cash": 0,
                        "open_positions_value": 0, "settled_pnl": 0,
                        "share_price": 1.0, "apr": None, "apy": None,
                        "timestamp": int(time.time()), "deadline": int(time.time()) + 300,
                        "round_id": 0, "signature": ""})


def _compute_apr(cur):
    """Compute annualised return % from settled positions."""
    try:
        # From settled positions: total realized PnL / total cost * annualised
        cur.execute("""
            SELECT
                SUM(settled_pnl_usdc)                        AS total_pnl,
                SUM(cost_basis_usdc)                         AS total_cost,
                MIN(opened_at)                               AS first_open,
                MAX(COALESCE(settled_at, NOW()))             AS last_settled,
                COUNT(*)                                     AS n
            FROM arb_positions
            WHERE status = 'settled' AND cost_basis_usdc > 0
        """)
        row = cur.fetchone()
        if row and row['n'] and row['total_cost'] and float(row['total_cost']) > 0:
            total_pnl = float(row['total_pnl'] or 0)
            total_cost = float(row['total_cost'])
            first_open = row['first_open']
            last_settled = row['last_settled']
            if first_open and last_settled:
                days = max(1, (last_settled - first_open).total_seconds() / 86400)
                apr = (total_pnl / total_cost) * (365 / days) * 100
                return apr

        # Fallback: estimate from open positions (expected edge / remaining days)
        cur.execute("""
            SELECT shares, cost_basis_usdc, expiry_ts
            FROM arb_positions
            WHERE status = 'open' AND shares > 0 AND cost_basis_usdc > 0 AND expiry_ts > 0
        """)
        open_rows = cur.fetchall()
        now_ts = time.time()
        estimates = []
        for r in open_rows:
            shares = float(r['shares'])
            cost = float(r['cost_basis_usdc'])
            expiry = float(r['expiry_ts'])
            days_remaining = max(1, (expiry - now_ts) / 86400)
            expected_payout = shares * 1.0
            edge_pct = (expected_payout - cost) / cost if cost > 0 else 0
            apr_est = edge_pct / days_remaining * 365 * 100
            estimates.append(apr_est)
        if estimates:
            return sum(estimates) / len(estimates)

        return None
    except Exception:
        return None


# ── pARB API: Open Positions ──────────────────────────────────────────────────

@app.route('/api/arb-vault/positions')
def arb_positions():
    """Return open arbitrage positions with edge, avg price, PnL, expiry, platforms."""
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.pair_id,
                   p.poly_yes_token, p.kalshi_ticker, p.kalshi_side,
                   p.shares, p.cost_basis_usdc, p.expiry_ts, p.status,
                   p.poly_title, p.kalshi_title, p.opened_at,
                   e_poly.venue  AS poly_venue,
                   e_poly.price  AS poly_price,
                   e_poly.size   AS poly_size,
                   e_poly.side   AS poly_side,
                   e_k.venue     AS venue2,
                   e_k.price     AS k_price,
                   e_k.size      AS k_size,
                   e_k.side      AS k_side
            FROM arb_positions p
            LEFT JOIN LATERAL (
                SELECT venue, price, size, side FROM arb_executions
                WHERE pair_id = p.pair_id AND leg = 1 AND success = true
                ORDER BY id DESC LIMIT 1
            ) e_poly ON true
            LEFT JOIN LATERAL (
                SELECT venue, price, size, side FROM arb_executions
                WHERE pair_id = p.pair_id AND leg = 2 AND success = true
                ORDER BY id DESC LIMIT 1
            ) e_k ON true
            WHERE p.status = 'open'
            ORDER BY p.opened_at DESC
        """)
        rows = cur.fetchall()
        conn.close()

        now_ts = time.time()
        result = []
        for r in rows:
            shares = float(r['shares'] or 0)
            cost = float(r['cost_basis_usdc'] or 0)
            expiry = int(r['expiry_ts'] or 0)

            # Expected payout: true arb — one leg always pays $1 per contract
            expected_payout = shares * 1.0

            # Edge % = (guaranteed payout - cost) / cost
            edge_pct = ((expected_payout - cost) / cost * 100) if cost > 0 else 0

            # Avg price paid per pair (both legs combined, per contract)
            avg_price_per_pair = (cost / shares) if shares > 0 else 0

            # Unrealized PnL: value at $0.99/share - cost
            current_value = shares * 0.99
            unrealized_pnl = current_value - cost

            # Days remaining
            days_left = max(0, (expiry - now_ts) / 86400) if expiry > 0 else 0

            # Platform label
            poly_venue = r['poly_venue'] or 'polymarket'
            venue2 = r['venue2'] or ('kalshi' if r['kalshi_ticker'] else 'unknown')
            platform_label = f"{_venue_label(poly_venue)} × {_venue_label(venue2)}"

            # Per-leg data from arb_executions
            poly_price = float(r['poly_price'] or 0)
            poly_size  = float(r['poly_size']  or 0)
            poly_side  = r['poly_side'] or 'YES'
            k_price    = float(r['k_price'] or 0)
            k_size     = float(r['k_size']  or 0)
            k_side     = r['k_side'] or (r['kalshi_side'] or 'NO')

            # Edge: what's left of $1 after paying for both legs
            edge_cents = 1.0 - poly_price - k_price
            edge_pct_legs = edge_cents * 100  # %

            result.append({
                "pair_id": r['pair_id'],
                "market": r['poly_title'] or r['pair_id'] or '—',
                "kalshi_title": r['kalshi_title'] or '',
                "kalshi_ticker": r['kalshi_ticker'] or '',
                "kalshi_side": r['kalshi_side'] or 'YES',
                "platform_label": platform_label,
                "poly_venue": poly_venue,
                "venue2": venue2,
                "shares": shares,
                "cost_basis_usdc": cost,
                "avg_price_per_pair": round(avg_price_per_pair, 4),
                "edge_pct": round(edge_pct_legs if poly_price > 0 else edge_pct, 2),
                "expected_payout": round(expected_payout, 2),
                "unrealized_pnl": round(unrealized_pnl, 4),
                "current_value": round(current_value, 2),
                "expiry_ts": expiry,
                "days_left": round(days_left, 1),
                "opened_at": r['opened_at'].isoformat() if r['opened_at'] else None,
                "total_liquid_value": round(current_value, 2),
                # Per-leg breakdown
                "poly_price": round(poly_price, 4),
                "poly_shares": round(poly_size, 2),
                "poly_side": poly_side,
                "kalshi_price": round(k_price, 4),
                "kalshi_shares": round(k_size, 2),
                "kalshi_side_exec": k_side,
                "venue2_label": _venue_label(venue2),
            })

        return jsonify({"ok": True, "positions": result, "count": len(result)})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "positions": []})


def _venue_label(venue: str) -> str:
    mapping = {
        "polymarket": "Polymarket",
        "kalshi": "Kalshi",
        "opinion": "Opinion",
    }
    return mapping.get((venue or '').lower(), venue.title() if venue else '—')


# ── pARB API: Trade History ───────────────────────────────────────────────────

@app.route('/api/arb-vault/trade-history')
def arb_trade_history():
    """Return all positions (open + settled) as trade history."""
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.pair_id, p.kalshi_ticker, p.kalshi_side,
                   p.shares, p.cost_basis_usdc, p.expiry_ts, p.status,
                   p.settled_pnl_usdc, p.poly_title, p.kalshi_title,
                   p.opened_at, p.settled_at,
                   e_poly.venue  AS poly_venue,
                   e_k.venue     AS venue2,
                   e_poly.price  AS poly_price,
                   e_k.price     AS venue2_price
            FROM arb_positions p
            LEFT JOIN LATERAL (
                SELECT venue, price FROM arb_executions
                WHERE pair_id = p.pair_id AND leg = 1 AND success = true
                ORDER BY id DESC LIMIT 1
            ) e_poly ON true
            LEFT JOIN LATERAL (
                SELECT venue, price FROM arb_executions
                WHERE pair_id = p.pair_id AND leg = 2 AND success = true
                ORDER BY id DESC LIMIT 1
            ) e_k ON true
            ORDER BY p.opened_at DESC
            LIMIT 200
        """)
        rows = cur.fetchall()
        conn.close()

        now_ts = time.time()
        result = []
        for r in rows:
            shares = float(r['shares'] or 0)
            cost = float(r['cost_basis_usdc'] or 0)
            expiry = int(r['expiry_ts'] or 0)
            expected_profit = max(0, shares * 1.0 - cost)
            edge_pct = (expected_profit / cost * 100) if cost > 0 else 0
            avg_price = (cost / shares) if shares > 0 else 0
            poly_price = float(r['poly_price'] or 0)
            venue2_price = float(r['venue2_price'] or 0)

            poly_venue = r['poly_venue'] or 'polymarket'
            venue2 = r['venue2'] or 'kalshi'
            leg_label = f"{_venue_label(poly_venue)} × {_venue_label(venue2)}"

            days_left = max(0, (expiry - now_ts) / 86400) if expiry > 0 else 0

            result.append({
                "pair_id": r['pair_id'],
                "title": r['poly_title'] or r['pair_id'] or '—',
                "kalshi_title": r['kalshi_title'] or '',
                "kalshi_ticker": r['kalshi_ticker'] or '',
                "kalshi_side": r['kalshi_side'] or 'YES',
                "leg_label": leg_label,
                "platform_label": leg_label,
                "shares": shares,
                "cost_usdc": round(cost, 2),
                "avg_price_per_pair": round(avg_price, 4),
                "poly_price": round(poly_price, 4),
                "venue2_price": round(venue2_price, 4),
                "edge_pct": round(edge_pct, 2),
                "expected_profit": round(expected_profit, 2),
                "status": r['status'],
                "settled_pnl": float(r['settled_pnl_usdc'] or 0),
                "expiry_ts": expiry,
                "days_left": round(days_left, 1),
                "opened_at": r['opened_at'].isoformat() if r['opened_at'] else None,
                "settled_at": r['settled_at'].isoformat() if r['settled_at'] else None,
            })

        return jsonify({"ok": True, "trades": result, "count": len(result)})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "trades": []})


# ── pARB API: Live Opportunities (Oddpool proxy) ──────────────────────────────

@app.route('/api/arb-vault/opportunities')
def arb_opportunities():
    """Proxy to Oddpool /arb-current endpoint."""
    try:
        headers = {}
        if ODDPOOL_API_KEY:
            headers['Authorization'] = f'Bearer {ODDPOOL_API_KEY}'
            headers['X-API-Key'] = ODDPOOL_API_KEY

        resp = requests.get(
            f'{ODDPOOL_BASE_URL}/arb-current',
            headers=headers,
            timeout=10,
        )

        if resp.status_code == 200:
            data = resp.json()
            opps = data if isinstance(data, list) else data.get('opportunities', data.get('data', []))
            return jsonify({"ok": True, "opportunities": opps, "count": len(opps)})

        return jsonify({"ok": False, "opportunities": [],
                        "error": f"Oddpool returned HTTP {resp.status_code}"})

    except Exception as e:
        return jsonify({"ok": False, "opportunities": [], "error": str(e)})


if __name__ == '__main__':
    init_database()

    admin_token = os.getenv('PMFI_ADMIN_TOKEN')
    if admin_token:
        print(f"\n🔐 Admin Token: {admin_token[:8]}...")
    else:
        print("\n⚠️  No PMFI_ADMIN_TOKEN set")

    print("\n🚀 Starting pARB/pSNIPER Vault on http://0.0.0.0:8080")
    app.run(host='0.0.0.0', port=8080, debug=False)

"""Arb NAV Tracker - computes liquid NAV for pArbitrage vault positions.

NAV = servicer_poly_cash + servicer_kalshi_cash + sum(open_positions_liquid_value) + sum(settled_pnl)

Liquid value per position uses orderbook BIDS (not asks, not cost basis) to reflect
real liquidation value: liquid_value = poly_yes_bid + kalshi_yes_bid per share.

Signing follows the same ABI-encoded struct hash pattern as the PMFI pARB Vault contract (PMFIArbVaultV1.sol):
  keccak256(abi.encode(NAV_TYPEHASH, totalAssets, polyCash, kalshiCash,
                       openPositionsValue, settledPnl, timestamp, deadline,
                       roundId, vault, chainId, domainSalt))
then prefixed with eth_sign ("\x19Ethereum Signed Message:\n32").
"""

import os
import time
from dataclasses import dataclass
from typing import Optional
from ..adapters.polymarket import get_best_prices as poly_get_best_prices
from ..adapters.kalshi import get_best_prices as kalshi_get_best_prices


def log(msg: str):
    print(f"📈 [Arb/NAV] {msg}")


ARB_VAULT_DOMAIN_SALT = "PMFIArbVaultV1.v1"
NAV_VALIDITY_WINDOW = 30  # 30-second deadline window (contract MAX_NAV_AGE = 30)


@dataclass
class ArbPosition:
    pair_id: str
    poly_yes_token: str
    kalshi_ticker: str
    shares: float
    cost_basis_usdc: float
    expiry_ts: int
    status: str
    settled_pnl_usdc: float = 0.0
    # Which side was bought on Kalshi ("YES" or "NO")
    # Determines whether to use kalshi_yes_bid or kalshi_no_bid for liquid value.
    kalshi_side: str = "YES"


@dataclass
class LiquidPositionValue:
    pair_id: str
    poly_yes_bid: Optional[float]
    kalshi_yes_bid: Optional[float]
    liquid_value_per_share: float
    total_liquid_value: float
    shares: float
    warning: str = ""


def fetch_position_liquid_value(position: ArbPosition) -> LiquidPositionValue:
    """Fetch live bids (not asks) for both legs of an open arb position.

    Uses bids because we want liquidation value (what we can sell for today).
    Uses the CORRECT bid side per leg:
    - Poly leg: always YES (we always buy Poly YES for the arb)
    - Kalshi leg: YES bid or NO bid depending on position.kalshi_side
      (e.g. if we bought Kalshi NO, the liquidation bid is kalshi_no_bid)
    """
    log(f"Fetching liquid value for pair_id={position.pair_id} (kalshi_side={position.kalshi_side})")

    poly_prices = poly_get_best_prices(position.poly_yes_token)
    poly_yes_bid = poly_prices.get("best_bid")

    kalshi_prices = kalshi_get_best_prices(position.kalshi_ticker)

    # Determine Kalshi liquidation bid based on which side was bought
    if position.kalshi_side == "NO":
        kalshi_leg_bid = kalshi_prices.get("no_best_bid")
        kalshi_bid_label = "no_bid"
    else:
        kalshi_leg_bid = kalshi_prices.get("yes_best_bid")
        kalshi_bid_label = "yes_bid"

    warning = ""
    if poly_yes_bid is None:
        warning += "poly_bid_missing "
        poly_yes_bid = 0.0
        log(f"⚠️ poly_bid_missing for {position.pair_id}")
    if kalshi_leg_bid is None:
        warning += f"kalshi_{kalshi_bid_label}_missing "
        kalshi_leg_bid = 0.0
        log(f"⚠️ kalshi_{kalshi_bid_label}_missing for {position.pair_id}")

    liquid_value_per_share = poly_yes_bid + kalshi_leg_bid
    total_liquid_value = liquid_value_per_share * position.shares

    log(
        f"Position {position.pair_id}: "
        f"poly_yes_bid={poly_yes_bid}, kalshi_{kalshi_bid_label}={kalshi_leg_bid}, "
        f"liquid_per_share={liquid_value_per_share:.4f}, "
        f"shares={position.shares}, total={total_liquid_value:.4f} USDC"
    )

    return LiquidPositionValue(
        pair_id=position.pair_id,
        poly_yes_bid=poly_yes_bid,
        kalshi_yes_bid=kalshi_leg_bid,
        liquid_value_per_share=liquid_value_per_share,
        total_liquid_value=total_liquid_value,
        shares=position.shares,
        warning=warning.strip(),
    )


def _load_open_positions() -> list[ArbPosition]:
    """Load open arb positions from the database."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        log("⚠️ DATABASE_URL not set — returning empty position list")
        return []
    try:
        import psycopg2
        conn = psycopg2.connect(database_url)
        cur = conn.cursor()
        cur.execute("""
            SELECT pair_id, poly_yes_token, kalshi_ticker, shares, cost_basis_usdc,
                   expiry_ts, status, COALESCE(settled_pnl_usdc, 0),
                   COALESCE(kalshi_side, 'YES')
            FROM arb_positions
            WHERE status = 'open'
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        positions = []
        for row in rows:
            positions.append(ArbPosition(
                pair_id=row[0],
                poly_yes_token=row[1],
                kalshi_ticker=row[2],
                shares=float(row[3]),
                cost_basis_usdc=float(row[4]),
                expiry_ts=int(row[5]),
                status=row[6],
                settled_pnl_usdc=float(row[7]),
                kalshi_side=row[8] if row[8] in ("YES", "NO") else "YES",
            ))
        log(f"Loaded {len(positions)} open positions from DB")
        return positions
    except Exception as e:
        log(f"❌ Error loading positions: {e}")
        return []


def _get_servicer_balances() -> tuple[float, float]:
    """Get servicer wallet cash balances on Polymarket and Kalshi.

    Returns (poly_cash_usdc, kalshi_cash_usdc).
    These are queried from the respective APIs and represent uninvested USDC.
    """
    poly_cash = 0.0
    kalshi_cash = 0.0

    poly_api_key = os.environ.get("POLY_API_KEY", "")
    if poly_api_key:
        try:
            import requests
            clob_url = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
            resp = requests.get(
                f"{clob_url}/balance",
                headers={"Authorization": f"Bearer {poly_api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                poly_cash = float(data.get("balance", data.get("usdc", 0)))
                log(f"Poly servicer cash: {poly_cash} USDC")
        except Exception as e:
            log(f"⚠️ Error fetching poly servicer balance: {e}")
    else:
        log("ℹ️ POLY_API_KEY not set — poly_cash = 0")

    from .kalshi_auth import get_kalshi_headers, kalshi_auth_available
    if kalshi_auth_available():
        try:
            import requests
            from ..config import KALSHI_BASE_URL
            url = f"{KALSHI_BASE_URL}/portfolio/balance"
            headers = get_kalshi_headers("GET", url)
            if headers:
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    kalshi_cash = float(data.get("balance", 0)) / 100.0
                    log(f"Kalshi servicer cash: {kalshi_cash} USDC")
                else:
                    log(f"⚠️ Kalshi balance HTTP {resp.status_code}: {resp.text[:100]}")
            else:
                log("⚠️ Kalshi RSA signing failed — kalshi_cash = 0")
        except Exception as e:
            log(f"⚠️ Error fetching kalshi servicer balance: {e}")
    else:
        log("ℹ️ Kalshi credentials not configured (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH) — kalshi_cash = 0")

    return poly_cash, kalshi_cash


def _get_settled_pnl() -> float:
    """Get sum of all settled PnL from closed positions."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return 0.0
    try:
        import psycopg2
        conn = psycopg2.connect(database_url)
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(settled_pnl_usdc), 0)
            FROM arb_positions
            WHERE status = 'settled'
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception as e:
        log(f"⚠️ Error fetching settled PnL: {e}")
        return 0.0


def _sign_nav_abi_encoded(
    total_assets_usdc: float,
    poly_cash: float,
    kalshi_cash: float,
    open_positions_value: float,
    settled_pnl: float,
    round_id: int,
    timestamp: int,
    deadline: int,
) -> Optional[str]:
    """Sign NAV payload using ECDSA over ABI-encoded ArbNavDataV1 struct hash.

    Matches the encoding verified on-chain in PMFIArbVaultV1._verifyAndApplyNav:
      keccak256(abi.encode(NAV_TYPEHASH, totalAssets, polyCash, kalshiCash,
                           openPositionsValue, settledPnl, timestamp, deadline,
                           roundId, vault, chainId, domainSalt))
      .toEthSignedMessageHash()

    Returns hex-encoded signature string or None if signing key not available.
    """
    nav_signer_key = os.environ.get("ARB_NAV_SIGNER_PRIVATE_KEY", "")
    if not nav_signer_key:
        log("⚠️ ARB_NAV_SIGNER_PRIVATE_KEY not set — NAV will be unsigned")
        return None

    vault_address = os.environ.get("ARB_VAULT_V1_ADDRESS", "0x0000000000000000000000000000000000000000")
    chain_id = int(os.environ.get("ARB_CHAIN_ID", "8453"))  # Base mainnet

    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        import eth_utils
        from eth_abi import encode

        NAV_TYPEHASH = eth_utils.keccak(text=(
            "ArbNavDataV1("
            "uint256 totalAssets,"
            "uint256 polyCash,"
            "uint256 kalshiCash,"
            "uint256 openPositionsValue,"
            "uint256 settledPnl,"
            "uint256 timestamp,"
            "uint256 deadline,"
            "uint256 roundId,"
            "address vault,"
            "uint256 chainId,"
            "bytes32 domainSalt"
            ")"
        ))

        DOMAIN_SALT_BYTES32 = eth_utils.keccak(text=ARB_VAULT_DOMAIN_SALT)

        def to_usdc6(v: float) -> int:
            return int(round(v * 1_000_000))

        total_int = to_usdc6(total_assets_usdc)
        poly_int = to_usdc6(poly_cash)
        kalshi_int = to_usdc6(kalshi_cash)
        open_int = to_usdc6(open_positions_value)
        settled_int = to_usdc6(settled_pnl)

        encoded = encode(
            ["bytes32", "uint256", "uint256", "uint256", "uint256", "uint256",
             "uint256", "uint256", "uint256", "address", "uint256", "bytes32"],
            [NAV_TYPEHASH,
             total_int, poly_int, kalshi_int, open_int, settled_int,
             timestamp, deadline, round_id,
             vault_address, chain_id, DOMAIN_SALT_BYTES32]
        )

        struct_hash = eth_utils.keccak(encoded)
        msg = encode_defunct(primitive=struct_hash)
        signed = Account.sign_message(msg, private_key=nav_signer_key)
        sig = signed.signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        log(f"✅ NAV signed: roundId={round_id} sig={sig[:20]}...")
        return sig

    except ImportError as ie:
        log(f"⚠️ Missing dependency for NAV signing ({ie}) — NAV will be unsigned")
        return None
    except Exception as e:
        log(f"❌ NAV signing error: {e}")
        return None


_round_id_counter: int = 0
_round_id_lock = None


def _get_round_id_lock():
    global _round_id_lock
    if _round_id_lock is None:
        import threading
        _round_id_lock = threading.Lock()
    return _round_id_lock


def _read_on_chain_last_round_id() -> int:
    """Read the contract's lastRoundId from the blockchain.

    contract.claim() increments lastRoundId on-chain, so we must read it
    before signing a NAV payload to ensure our round_id strictly exceeds
    what the contract has already seen.
    Returns 0 if contract address not set or chain is unreachable.
    """
    vault_address = os.environ.get("ARB_VAULT_V1_ADDRESS", "")
    rpc_url = os.environ.get("RPC_URL", os.environ.get("BASE_RPC_URL", "https://mainnet.base.org"))
    if not vault_address or vault_address == "0x0000000000000000000000000000000000000000":
        log("ℹ️ ARB_VAULT_V1_ADDRESS not set — on-chain lastRoundId = 0")
        return 0
    try:
        import requests as _req
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {
                    "to": vault_address,
                    # lastRoundId() selector = keccak256("lastRoundId()")[:4] = 0x388ca80f
                    "data": "0x388ca80f",
                },
                "latest",
            ],
            "id": 1,
        }
        resp = _req.post(rpc_url, json=payload, timeout=5)
        if resp.status_code == 200:
            result = resp.json().get("result", "0x0")
            on_chain_id = int(result, 16) if result and result != "0x" else 0
            log(f"✅ On-chain lastRoundId={on_chain_id}")
            return on_chain_id
    except Exception as e:
        log(f"⚠️ Could not read on-chain lastRoundId: {e}")
    return 0


def _get_next_round_id() -> int:
    """Get a strictly monotonically increasing round ID for the NAV oracle.

    Sources:
    1. On-chain contract.lastRoundId (which claim() can increment)
    2. Off-chain DB max (highest previously issued NAV round_id)
    3. In-memory counter (for within-process monotonicity)

    Takes max of all three + 1, so the returned ID is always > everything the
    contract has seen and > any previously signed payload.
    """
    global _round_id_counter
    lock = _get_round_id_lock()

    with lock:
        # Read on-chain value on every call to stay ahead of claim() increments
        on_chain = _read_on_chain_last_round_id()

        # Seed/refresh from DB
        db_max = 0
        database_url = os.environ.get("DATABASE_URL", "")
        if database_url:
            try:
                import psycopg2
                conn = psycopg2.connect(database_url)
                cur = conn.cursor()
                cur.execute("SELECT COALESCE(MAX(round_id), 0) FROM arb_vault_nav")
                row = cur.fetchone()
                cur.close()
                conn.close()
                db_max = int(row[0]) if row else 0
            except Exception as e:
                log(f"⚠️ Could not read DB max round_id: {e}")

        # Take max of all sources and increment to exceed all of them
        new_counter = max(on_chain, db_max, _round_id_counter) + 1
        _round_id_counter = new_counter
        log(f"🔢 round_id={new_counter} (on_chain={on_chain}, db_max={db_max})")
        return new_counter


def compute_nav() -> dict:
    """Compute current vault NAV and return signed payload.

    NAV = poly_cash + kalshi_cash + sum(open_positions_liquid_value) + sum(settled_pnl)
    Uses actual orderbook bids for open positions (liquidation value, not cost basis).

    Returns a payload dict that the frontend can pass directly into deposit()/requestWithdraw()
    after restructuring into the ArbNavDataV1 tuple.
    """
    log("Computing arb vault NAV...")
    start = time.time()

    poly_cash, kalshi_cash = _get_servicer_balances()

    positions = _load_open_positions()
    position_values = []
    total_liquid = 0.0

    for pos in positions:
        lv = fetch_position_liquid_value(pos)
        position_values.append(lv)
        total_liquid += lv.total_liquid_value

    settled_pnl = _get_settled_pnl()

    total_assets = poly_cash + kalshi_cash + total_liquid + settled_pnl

    log(
        f"NAV breakdown: poly_cash={poly_cash:.4f}, kalshi_cash={kalshi_cash:.4f}, "
        f"open_positions={total_liquid:.4f}, settled_pnl={settled_pnl:.4f}, "
        f"TOTAL={total_assets:.4f} USDC"
    )

    timestamp = int(time.time())
    round_id = _get_next_round_id()
    deadline = timestamp + NAV_VALIDITY_WINDOW

    signature = _sign_nav_abi_encoded(
        total_assets_usdc=total_assets,
        poly_cash=poly_cash,
        kalshi_cash=kalshi_cash,
        open_positions_value=total_liquid,
        settled_pnl=settled_pnl,
        round_id=round_id,
        timestamp=timestamp,
        deadline=deadline,
    )

    elapsed_ms = int((time.time() - start) * 1000)

    _save_nav_snapshot(
        total_assets_usdc=total_assets,
        poly_cash=poly_cash,
        kalshi_cash=kalshi_cash,
        open_positions_value=total_liquid,
        settled_pnl=settled_pnl,
        round_id=round_id,
        signature=signature or "",
    )

    payload = {
        "total_assets_usdc": round(total_assets, 6),
        "poly_cash": round(poly_cash, 6),
        "kalshi_cash": round(kalshi_cash, 6),
        "open_positions_value": round(total_liquid, 6),
        "settled_pnl": round(settled_pnl, 6),
        "round_id": round_id,
        "timestamp": timestamp,
        "deadline": deadline,
        "domain_salt": ARB_VAULT_DOMAIN_SALT,
        "signature": signature,
        "computed_in_ms": elapsed_ms,
        "open_positions": [
            {
                "pair_id": lv.pair_id,
                "shares": lv.shares,
                "poly_yes_bid": lv.poly_yes_bid,
                "kalshi_yes_bid": lv.kalshi_yes_bid,
                "liquid_value_per_share": round(lv.liquid_value_per_share, 6),
                "total_liquid_value": round(lv.total_liquid_value, 6),
                "warning": lv.warning,
            }
            for lv in position_values
        ],
    }

    log(f"NAV computed in {elapsed_ms}ms: total={total_assets:.4f} USDC, roundId={round_id}")
    return payload


def _save_nav_snapshot(
    total_assets_usdc: float,
    poly_cash: float,
    kalshi_cash: float,
    open_positions_value: float,
    settled_pnl: float,
    round_id: int,
    signature: str,
):
    """Persist NAV snapshot to arb_vault_nav table."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(database_url)
        cur = conn.cursor()
        cur.execute("""
            UPDATE arb_vault_nav
            SET total_assets_usdc=%s, poly_cash=%s, kalshi_cash=%s,
                open_positions_value=%s, settled_pnl=%s, signature=%s
            WHERE round_id=%s
        """, (total_assets_usdc, poly_cash, kalshi_cash, open_positions_value,
              settled_pnl, signature, round_id))
        if cur.rowcount == 0:
            cur.execute("""
                INSERT INTO arb_vault_nav
                    (computed_at, total_assets_usdc, poly_cash, kalshi_cash,
                     open_positions_value, settled_pnl, round_id, signature)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s)
            """, (total_assets_usdc, poly_cash, kalshi_cash, open_positions_value,
                  settled_pnl, round_id, signature))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log(f"⚠️ Error saving NAV snapshot: {e}")

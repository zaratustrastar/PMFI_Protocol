"""Configuration for arb-monitor loaded from environment variables."""

import os

KALSHI_BASE_URL = os.environ.get("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")

# Kalshi RSA authentication (API v2 — replaces simple Bearer token)
# Get your API key ID and private key from: https://kalshi.com/account/api
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")  # path to .pem file
KALSHI_PRIVATE_KEY_PEM = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "")   # PEM content directly

POLY_GAMMA_URL = os.environ.get("POLY_GAMMA_URL", "https://gamma-api.polymarket.com")
POLY_CLOB_URL = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")

OPINION_BASE_URL = os.environ.get("OPINION_BASE_URL", "https://proxy.opinion.trade:8443/openapi")
OPINION_API_KEY = os.environ.get("OPINION_API_KEY", "")
OPINION_MAX_PAGES = int(os.environ.get("OPINION_MAX_PAGES", "10"))
OPINION_ORDERBOOK_DELAY = float(os.environ.get("OPINION_ORDERBOOK_DELAY", "0.08"))

SCAN_INTERVAL_SECONDS = int(os.environ.get("ARB_SCAN_INTERVAL", "60"))
CACHE_TTL_SECONDS = int(os.environ.get("ARB_CACHE_TTL", "45"))

ARB_MAX_PAGES_KALSHI = int(os.environ.get("ARB_MAX_PAGES_KALSHI", "10"))
ARB_MAX_PAGES_POLY = int(os.environ.get("ARB_MAX_PAGES_POLY", "10"))
ARB_PAGE_SIZE_POLY = int(os.environ.get("ARB_PAGE_SIZE_POLY", "100"))
ARB_EXPIRY_WINDOW_DAYS = int(os.environ.get("ARB_EXPIRY_WINDOW_DAYS", "365"))

PMXT_DISCOVERY_QUERIES = os.environ.get(
    "PMXT_DISCOVERY_QUERIES",
    "Trump,Fed,election,bitcoin,crypto,pope,tariff,AI,Musk,Israel,Ukraine,congress,senate,governor"
).split(",")
PMXT_QUERY_LIMIT = int(os.environ.get("PMXT_QUERY_LIMIT", "50"))
PMXT_KALSHI_RATE_DELAY = float(os.environ.get("PMXT_KALSHI_RATE_DELAY", "1.5"))
PMXT_POLY_RATE_DELAY = float(os.environ.get("PMXT_POLY_RATE_DELAY", "0.3"))

SEED_PAIRS_PATH = os.environ.get(
    "SEED_PAIRS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "seedPairs.json")
)

MIN_PRICE_THRESHOLD = float(os.environ.get("MIN_PRICE_THRESHOLD", "0.02"))
NEAR_ARB_MAX_COST = float(os.environ.get("NEAR_ARB_MAX_COST", "1.01"))

MIN_EDGE_DEFAULT = 0.01
MAX_RESULTS_DEFAULT = 50

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

ARB_USE_ODDPOOL_ONLY = os.environ.get("ARB_USE_ODDPOOL_ONLY", "true").lower() == "true"
ODDPOOL_BASE_URL = os.environ.get("ODDPOOL_BASE_URL", "https://api.oddpool.com")
ODDPOOL_API_KEY = os.environ.get("ODDPOOL_API_KEY", "")
ODDPOOL_POLL_INTERVAL = int(os.environ.get("ODDPOOL_POLL_INTERVAL", "30"))

ARB_MIN_EDGE_PCT = float(os.environ.get("ARB_MIN_EDGE_PCT", "0.025"))
ARB_SLIPPAGE_GUARD_BPS = int(os.environ.get("ARB_SLIPPAGE_GUARD_BPS", "50"))
ARB_MAX_PAIR_USDC = float(os.environ.get("ARB_MAX_PAIR_USDC", "500"))
ARB_MAX_DEPLOYED_USDC = float(os.environ.get("ARB_MAX_DEPLOYED_USDC", "10000"))

# Profit-maximising scorer tuning
# ARB_RISK_BUFFER_PCT: extra cost buffer subtracted from net_cents to build a conservative
#   net_edge estimate. Absorbs residual execution risk (partial fills, spread widening, etc.)
#   Units: percent (0.1 = 0.1%, i.e. 10 bps). Default 0.1%.
ARB_RISK_BUFFER_PCT = float(os.environ.get("ARB_RISK_BUFFER_PCT", "0.1"))

# ARB_FILLABLE_FRACTION: fraction of the thinner leg's reported liquidity we assume is
#   realistically fillable at the quoted spread. 0.10 = 10% of listed liquidity.
#   Lowering this makes the scorer more conservative; raising it makes it more aggressive.
ARB_FILLABLE_FRACTION = float(os.environ.get("ARB_FILLABLE_FRACTION", "0.10"))
AI_MATCH_CACHE_PATH = os.environ.get("AI_MATCH_CACHE_PATH", "/tmp/arb_ai_match_cache.json")
AI_MATCH_CACHE_TTL = int(os.environ.get("AI_MATCH_CACHE_TTL", str(7 * 86400)))
AI_MATCH_MIN_CONFIDENCE = int(os.environ.get("AI_MATCH_MIN_CONFIDENCE", "60"))
AI_MATCH_MIN_RULES_SCORE = int(os.environ.get("AI_MATCH_MIN_RULES_SCORE", "40"))

SPORTS_KEYWORDS = {
    "esports": [
        "cs2", "counter-strike", "dota", "lol", "league of legends",
        "valorant", "vct", "iem", "blast", "major",
        "overwatch", "call of duty", "fortnite", "pubg", "rainbow six",
        "rocket league", "worlds", "champions", "lcs", "lec", "lck", "lpl",
        "esports", "csgo",
    ],
    "nba": [
        "nba", "lakers", "celtics", "warriors", "knicks", "nuggets", "heat",
        "playoffs", "finals", "mvp",
        "basketball", "bucks", "76ers", "suns", "mavericks", "clippers",
        "nets", "grizzlies", "cavaliers", "thunder", "timberwolves",
        "pacers", "hawks", "bulls", "rockets", "spurs", "pistons",
        "hornets", "wizards", "magic", "blazers", "kings", "pelicans",
        "raptors", "jazz",
    ],
    "nfl": [
        "nfl", "super bowl", "touchdown", "quarterback",
        "chiefs", "eagles", "cowboys", "49ers", "ravens", "bills",
        "dolphins", "lions", "packers", "bengals", "steelers",
    ],
    "soccer": [
        "soccer", "premier league", "la liga", "bundesliga", "serie a",
        "champions league", "world cup", "mls",
    ],
    "mma": [
        "ufc", "mma", "boxing", "fight night", "bellator",
    ],
    "sports": [
        "mlb", "baseball", "nhl", "hockey", "tennis", "f1", "formula",
        "golf", "pga", "cricket", "rugby", "olympics", "atp", "wta",
        "ncaa", "college basketball", "college football",
    ],
}

ALL_SPORT_KEYWORDS = []
for kw_list in SPORTS_KEYWORDS.values():
    ALL_SPORT_KEYWORDS.extend(kw_list)

# ---------------------------------------------------------------------------
# pARB V2 Liquidity Management
# ---------------------------------------------------------------------------

# Target idle USDC in vault as basis points of total assets. Default 10%.
ARB_IDLE_TARGET_BPS = int(os.environ.get("ARB_IDLE_TARGET_BPS", "1000"))

# Fixed safety buffer (USDC) always reserved on top of pending redeem value.
ARB_SAFETY_BUFFER_USDC = float(os.environ.get("ARB_SAFETY_BUFFER_USDC", "50"))

# Minimum shortfall (USDC) before the waterfall sweeper activates.
ARB_WATERFALL_MIN_SHORTFALL = float(os.environ.get("ARB_WATERFALL_MIN_SHORTFALL", "10"))

# Early report: trigger ahead of cooldown when pending_redeem_value exceeds
# this multiple of idle_available. Default 0.8 (80% of idle spoken for).
ARB_EARLY_REPORT_PRESSURE_RATIO = float(os.environ.get("ARB_EARLY_REPORT_PRESSURE_RATIO", "0.8"))

# Minimum seconds elapsed since last report before an early report fires.
# Prevents rapid-fire reports when a wave of small redeems arrives.
ARB_EARLY_REPORT_MIN_ELAPSED = int(os.environ.get("ARB_EARLY_REPORT_MIN_ELAPSED", "300"))

# ---------------------------------------------------------------------------
# pARB Auto-Funder (USDC distributor to trading platforms)
# ---------------------------------------------------------------------------

# Minimum servicer wallet USDC balance that triggers a distribution cycle.
# The funder skips distribution if the deployable capital is below this threshold
# to avoid sending dust transactions that cost more in gas than they're worth.
# Lowered from 20 → 5 USDC so small initial post-tend balances are not silently skipped.
# Set to 0 to distribute every cycle (not recommended due to gas costs).
ARB_MIN_FUND_AMOUNT = float(os.environ.get("ARB_MIN_FUND_AMOUNT", "5"))

# Proportional allocation of servicer USDC across the three platforms.
# Must sum to 100 (enforced proportionally if they don't — remainder goes to Opinion).
ARB_FUND_POLY_PCT = float(os.environ.get("ARB_FUND_POLY_PCT", "40"))
ARB_FUND_KALSHI_PCT = float(os.environ.get("ARB_FUND_KALSHI_PCT", "40"))
ARB_FUND_OPINION_PCT = float(os.environ.get("ARB_FUND_OPINION_PCT", "20"))

# Minimum ETH kept in the servicer wallet for gas. Distribution is skipped if
# the ETH balance falls below this threshold.
ARB_SERVICER_GAS_RESERVE_ETH = float(os.environ.get("ARB_SERVICER_GAS_RESERVE_ETH", "0.01"))

# Platform deposit addresses for auto-funder.
# Polymarket and Kalshi accept USDC directly on Base.
# Opinion Trade requires USDC on BSC (bridged via LI.FI).
POLY_BASE_DEPOSIT_ADDR = os.environ.get("POLY_BASE_DEPOSIT_ADDR", "")
KALSHI_BASE_DEPOSIT_ADDR = os.environ.get("KALSHI_BASE_DEPOSIT_ADDR", "")
OPINION_BSC_DEPOSIT_ADDR = os.environ.get("OPINION_BSC_DEPOSIT_ADDR", "")

"""Configuration for arb-monitor loaded from environment variables."""

import os

KALSHI_BASE_URL = os.environ.get("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
POLY_GAMMA_URL = os.environ.get("POLY_GAMMA_URL", "https://gamma-api.polymarket.com")
POLY_CLOB_URL = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")

SCAN_INTERVAL_SECONDS = int(os.environ.get("ARB_SCAN_INTERVAL", "60"))
CACHE_TTL_SECONDS = int(os.environ.get("ARB_CACHE_TTL", "45"))

ARB_MAX_PAGES_KALSHI = int(os.environ.get("ARB_MAX_PAGES_KALSHI", "10"))
ARB_MAX_PAGES_POLY = int(os.environ.get("ARB_MAX_PAGES_POLY", "5"))
ARB_EXPIRY_WINDOW_DAYS = int(os.environ.get("ARB_EXPIRY_WINDOW_DAYS", "30"))

MIN_EDGE_DEFAULT = 0.01
MAX_RESULTS_DEFAULT = 50

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

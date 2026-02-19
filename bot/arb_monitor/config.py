"""Configuration for arb-monitor loaded from environment variables."""

import os

OPINION_API_KEY = os.environ.get("OPINION_API_KEY", "")
OPINION_BASE_URL = os.environ.get("OPINION_BASE_URL", "https://openapi.opinion.trade/openapi")
POLY_GAMMA_URL = os.environ.get("POLY_GAMMA_URL", "https://gamma-api.polymarket.com")
POLY_CLOB_URL = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")

SCAN_INTERVAL_SECONDS = int(os.environ.get("ARB_SCAN_INTERVAL", "60"))
CACHE_TTL_SECONDS = int(os.environ.get("ARB_CACHE_TTL", "45"))

MIN_EDGE_DEFAULT = 0.01
MAX_RESULTS_DEFAULT = 50

SPORTS_KEYWORDS = {
    "nba": ["nba", "basketball", "lakers", "celtics", "warriors", "bucks", "nuggets", "76ers",
            "knicks", "heat", "suns", "mavericks", "clippers", "nets", "grizzlies", "cavaliers",
            "thunder", "timberwolves", "pacers", "hawks", "bulls", "rockets", "spurs", "pistons",
            "hornets", "wizards", "magic", "blazers", "kings", "pelicans", "raptors", "jazz"],
    "esports": ["esports", "league of legends", "lol", "dota", "cs2", "csgo", "counter-strike",
                "valorant", "overwatch", "call of duty", "fortnite", "pubg", "rainbow six",
                "rocket league", "worlds", "major", "champions", "lcs", "lec", "lck", "lpl"],
    "sports": ["nfl", "football", "soccer", "mlb", "baseball", "nhl", "hockey", "ufc", "mma",
               "boxing", "tennis", "f1", "formula", "golf", "pga", "cricket", "rugby",
               "premier league", "la liga", "bundesliga", "serie a", "champions league",
               "world cup", "olympics", "super bowl", "atp", "wta"],
}

ALL_SPORT_KEYWORDS = []
for kw_list in SPORTS_KEYWORDS.values():
    ALL_SPORT_KEYWORDS.extend(kw_list)

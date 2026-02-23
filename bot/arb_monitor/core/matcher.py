"""Matcher - finds matching markets across Polymarket and Kalshi.

Matching strategy (hardened v3 — investor-safe):
  1. Tokenize with expanded stopwords + boilerplate removal.
  2. Drop pure year/time tokens unless both sides share a non-time anchor.
  3. Topic classification (crypto, geopolitics, companies, politics, sports) — block cross-topic.
  4. Predicate gating with expanded verb classes (acquire≠expel, nominate≠invade, etc.).
  5. STRONG anchor requirement: >=2 strong entity overlaps, or 1 strong + 1 strong concept.
     Never accept if only generic tokens overlap.
  6. Subject entity gate: if both titles have a primary subject and they differ, reject.
  7. Date compatibility gate: month/day references must be compatible.
  8. Contract-type gate: range/inequality vs point/strike ladder detection.
  9. Tariff domain rule: tariff+rate requires exact counterparty entity + date match.
  10. Fast pass: token Jaccard on titles to build top-K shortlist.
  11. Refine: Levenshtein on shortlist only (expensive, constrained to top candidates).
  12. Combined score: 0.6 * Jaccard + 0.4 * Levenshtein.
  13. Team key boost: if both have matching team_key, boost to max(score, 0.90).
"""

import re
from ..models import NormalizedMarket


def log(msg: str):
    print(f"🔗 [Arb/Matcher] {msg}")


EXPIRY_GATES = {
    "esports": 12 * 3600,
    "nba": 24 * 3600,
    "nfl": 24 * 3600,
    "soccer": 24 * 3600,
    "mma": 24 * 3600,
    "sports": 24 * 3600,
}

DEFAULT_EXPIRY_GATE = 72 * 3600

JACCARD_SHORTLIST_K = 5
MIN_JACCARD_FOR_LEVENSHTEIN = 0.15

STOPWORDS = frozenset({
    "will", "the", "a", "an", "to", "in", "of", "for", "on", "at", "by",
    "is", "be", "before", "end", "any", "member", "during", "after",
    "or", "and", "not", "no", "yes", "if", "than", "that", "this",
    "it", "its", "has", "have", "had", "do", "does", "did", "was",
    "were", "been", "being", "are", "am", "with", "from", "as",
    "but", "so", "just", "more", "most", "some", "other", "each",
    "all", "both", "few", "many", "much", "very", "also", "how",
    "what", "which", "who", "whom", "when", "where", "why",
    "about", "between", "through", "into", "over", "under",
    "again", "once", "here", "there", "then", "up", "down",
    "out", "off", "above", "below",
})

PURE_YEAR_RE = re.compile(r'^20[2-3]\d$')
TIME_TOKENS = frozenset({
    "year", "month", "week", "day", "hour",
    "january", "jan", "february", "feb", "march", "mar",
    "april", "apr", "may", "june", "jun", "july", "jul",
    "august", "aug", "september", "sep", "october", "oct",
    "november", "nov", "december", "dec",
    "q1", "q2", "q3", "q4",
})

# --- STRONG vs GENERIC entity classification ---
# Strong entities: countries, regions, major assets, named persons/institutions, places
STRONG_ENTITIES = frozenset({
    # Countries / regions
    "china", "eu", "canada", "mexico", "russia", "ukraine", "taiwan", "greenland",
    "israel", "gaza", "iran", "korea", "japan", "india", "brazil", "turkey",
    "uk", "germany", "france", "australia", "saudi", "arabia",
    # Major assets
    "bitcoin", "btc", "ethereum", "eth", "solana", "xrp", "dogecoin", "doge",
    "gold", "silver", "oil", "nasdaq", "sp500",
    # Named people
    "trump", "biden", "harris", "musk", "bezos", "powell", "warsh", "yellen",
    "pope", "leo", "xiv", "swift", "taylor", "obama", "putin", "xi",
    "zuckerberg", "altman", "desantis", "newsom", "vance",
    # Major institutions / orgs
    "fed", "nato", "congress", "senate", "supreme", "court", "sec", "fbi",
    "cia", "pentagon", "un", "who", "imf", "ecb",
    # Companies
    "openai", "tesla", "spacex", "google", "apple", "amazon", "microsoft",
    "meta", "nvidia", "tiktok", "twitter",
    # Places
    "mars", "moon", "antarctica",
    # Major events/concepts with specific meaning
    "olympics", "fifa", "super", "bowl", "nba", "nfl", "mlb", "nhl", "ufc",
    "recession", "inflation", "gdp", "agi",
})

# Generic tokens that alone should never anchor a match
GENERIC_TOKENS = frozenset({
    "rate", "tariff", "meet", "talk", "visit", "reach", "hit", "drop",
    "rise", "fall", "increase", "decrease", "change", "price", "level",
    "market", "trade", "deal", "agreement", "announce", "announcement",
    "new", "next", "first", "last", "top", "high", "low", "close",
    "open", "start", "begin", "happen", "occur", "likely", "possible",
    "chance", "probability", "odds",
})

TOPIC_KEYWORDS = {
    "crypto": {
        "bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "xrp",
        "dogecoin", "doge", "cardano", "ada", "polygon", "matic",
        "defi", "nft", "blockchain", "halving", "stablecoin", "usdc",
        "usdt", "binance", "coinbase", "etf", "altcoin",
        "litecoin", "ripple", "avalanche", "chainlink",
    },
    "geopolitics": {
        "ukraine", "russia", "china", "taiwan", "greenland", "nato",
        "eu", "gaza", "israel", "iran", "korea", "war", "invasion",
        "ceasefire", "sanctions", "annex", "independence", "sovereignty",
        "nuclear", "missile", "troops", "military", "peace",
        "territory", "border", "occupation",
    },
    "companies": {
        "openai", "tesla", "spacex", "google", "apple", "amazon",
        "microsoft", "meta", "nvidia", "tiktok", "twitter",
        "acquired", "acquire", "acquisition", "merger", "ipo",
        "ceo", "founder", "valuation", "stock", "shares",
        "revenue", "earnings", "profit",
    },
    "politics": {
        "trump", "biden", "harris", "congress", "senate", "house",
        "president", "election", "vote", "poll", "democrat",
        "republican", "gop", "governor", "mayor", "nominee",
        "impeach", "expelled", "expel", "resign", "indicted",
        "cabinet", "veto", "legislation", "bill", "law",
        "fed", "chair", "warsh", "powell", "yellen",
        "tariff", "recession", "inflation",
    },
    "sports": {
        "nba", "nfl", "mlb", "nhl", "ufc", "mma", "boxing",
        "fifa", "premier league", "champions league", "olympics",
        "tennis", "golf", "f1", "formula", "ncaa",
        "playoff", "finals", "championship", "mvp", "draft",
        "super bowl", "world cup", "world series",
    },
}

# --- Predicate verb classes ---
_PREDICATE_ENDORSE = "ENDORSE"
_PREDICATE_WIN_PRIMARY = "WIN_PRIMARY"
_PREDICATE_WIN_GENERAL = "WIN_GENERAL"
_PREDICATE_ACQUIRE = "ACQUIRE"
_PREDICATE_EXPEL = "EXPEL"
_PREDICATE_NOMINATE = "NOMINATE"
_PREDICATE_INDEPENDENCE = "INDEPENDENCE"
_PREDICATE_INVADE = "INVADE"
_PREDICATE_RESIGN = "RESIGN"
_PREDICATE_BAN = "BAN"
_PREDICATE_APPROVE = "APPROVE"
_PREDICATE_MEET = "MEET"
_PREDICATE_OTHER = "OTHER"

_PREDICATE_VERB_CLASSES = {
    _PREDICATE_ACQUIRE: {"acquire", "acquired", "acquisition", "buy", "bought", "purchase", "merge", "merger"},
    _PREDICATE_EXPEL: {"expel", "expelled", "expelling", "expulsion", "remove", "removed", "oust", "ousted", "eject"},
    _PREDICATE_NOMINATE: {"nominate", "nominated", "nomination", "appoint", "appointed", "appointment", "pick", "select"},
    _PREDICATE_INDEPENDENCE: {"independence", "independent", "secede", "secession", "sovereignty", "autonomous"},
    _PREDICATE_INVADE: {"invade", "invaded", "invasion", "annex", "annexed", "annexation", "occupy", "occupied", "seize"},
    _PREDICATE_RESIGN: {"resign", "resigned", "resignation", "quit"},
    _PREDICATE_BAN: {"ban", "banned", "banning", "prohibit", "prohibited", "restrict"},
    _PREDICATE_APPROVE: {"approve", "approved", "approval", "ratify", "ratified", "enact"},
    _PREDICATE_ENDORSE: {"endorse", "endorsed", "endorsement", "endors", "backing"},
    _PREDICATE_MEET: {"meet", "meeting", "talk", "talks", "speak", "visit", "visiting", "conversation"},
    _PREDICATE_WIN_PRIMARY: {"win", "winner", "primary", "nominee", "nomination", "runoff"},
    _PREDICATE_WIN_GENERAL: {"election", "general", "electoral"},
}

_INCOMPATIBLE_PREDICATES = {
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_EXPEL}),
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_NOMINATE}),
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_INDEPENDENCE}),
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_INVADE}),
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_RESIGN}),
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_BAN}),
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_MEET}),
    frozenset({_PREDICATE_EXPEL, _PREDICATE_NOMINATE}),
    frozenset({_PREDICATE_EXPEL, _PREDICATE_INDEPENDENCE}),
    frozenset({_PREDICATE_EXPEL, _PREDICATE_APPROVE}),
    frozenset({_PREDICATE_EXPEL, _PREDICATE_MEET}),
    frozenset({_PREDICATE_NOMINATE, _PREDICATE_INVADE}),
    frozenset({_PREDICATE_NOMINATE, _PREDICATE_EXPEL}),
    frozenset({_PREDICATE_NOMINATE, _PREDICATE_BAN}),
    frozenset({_PREDICATE_NOMINATE, _PREDICATE_MEET}),
    frozenset({_PREDICATE_INDEPENDENCE, _PREDICATE_INVADE}),
    frozenset({_PREDICATE_INDEPENDENCE, _PREDICATE_ACQUIRE}),
    frozenset({_PREDICATE_RESIGN, _PREDICATE_NOMINATE}),
    frozenset({_PREDICATE_RESIGN, _PREDICATE_APPROVE}),
    frozenset({_PREDICATE_BAN, _PREDICATE_APPROVE}),
    frozenset({_PREDICATE_ENDORSE, _PREDICATE_WIN_PRIMARY}),
    frozenset({_PREDICATE_ENDORSE, _PREDICATE_WIN_GENERAL}),
    frozenset({_PREDICATE_ENDORSE, _PREDICATE_EXPEL}),
    frozenset({_PREDICATE_ENDORSE, _PREDICATE_ACQUIRE}),
    frozenset({_PREDICATE_ENDORSE, _PREDICATE_INVADE}),
    frozenset({_PREDICATE_ENDORSE, _PREDICATE_MEET}),
}

# --- Date extraction patterns ---
_MONTH_MAP = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "october": 10, "oct": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_DATE_FULL_RE = re.compile(
    r'\b(?:' + '|'.join(_MONTH_MAP.keys()) + r')\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?\b',
    re.IGNORECASE
)
_DATE_MONTH_ONLY_RE = re.compile(
    r'\b(?:in|by|before|after|during)?\s*(?:' + '|'.join(_MONTH_MAP.keys()) + r')\b',
    re.IGNORECASE
)

# --- Contract type patterns ---
_RANGE_WORDS = re.compile(
    r'\b(?:between|less\s+than|more\s+than|at\s+least|at\s+most|under|over|above|below)\b',
    re.IGNORECASE
)
_RANGE_PATTERN = re.compile(r'\d+%?\s*(?:and|to|-)\s*\d+%?')
_STRIKE_SUFFIX_RE = re.compile(r'-\d+$')

# --- Known subject entities for extraction ---
_KNOWN_PEOPLE = {
    "trump": "trump", "donald trump": "trump",
    "biden": "biden", "joe biden": "biden",
    "harris": "harris", "kamala harris": "harris",
    "musk": "musk", "elon musk": "musk",
    "bezos": "bezos", "jeff bezos": "bezos",
    "powell": "powell", "jerome powell": "powell",
    "warsh": "warsh", "kevin warsh": "warsh",
    "yellen": "yellen", "janet yellen": "yellen",
    "swift": "swift", "taylor swift": "swift",
    "pope": "pope", "pope leo": "pope", "pope leo xiv": "pope",
    "pope francis": "pope",
    "obama": "obama", "putin": "putin", "xi": "xi", "xi jinping": "xi",
    "zuckerberg": "zuckerberg", "altman": "altman", "sam altman": "altman",
    "desantis": "desantis", "newsom": "newsom", "vance": "vance",
}

_KNOWN_COUNTRY_ENTITIES = {
    "china": "china", "eu": "eu", "european union": "eu",
    "canada": "canada", "mexico": "mexico",
    "russia": "russia", "ukraine": "ukraine", "taiwan": "taiwan",
    "greenland": "greenland", "israel": "israel", "iran": "iran",
    "korea": "korea", "north korea": "korea", "south korea": "korea",
    "japan": "japan", "india": "india", "turkey": "turkey",
    "saudi arabia": "saudi",
}


def _normalize_vs(text: str) -> str:
    t = re.sub(r'\s+(?:vs\.?|versus|@)\s+', ' vs ', text, flags=re.IGNORECASE)
    return t


def _tokenize(text: str) -> set[str]:
    t = text.lower().strip()
    t = re.sub(r'[^\w\s%$]', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    tokens = {w for w in t.split() if w not in STOPWORDS and len(w) > 1}
    return tokens


def _is_time_token(token: str) -> bool:
    if PURE_YEAR_RE.match(token):
        return True
    return token in TIME_TOKENS


def _filter_time_tokens(tokens_a: set[str], tokens_b: set[str]) -> tuple[set[str], set[str]]:
    non_time_a = {t for t in tokens_a if not _is_time_token(t)}
    non_time_b = {t for t in tokens_b if not _is_time_token(t)}
    return non_time_a, non_time_b


def _classify_topic(tokens: set[str], title_lower: str) -> str | None:
    best_topic = None
    best_hits = 0

    for topic, keywords in TOPIC_KEYWORDS.items():
        hits = 0
        for kw in keywords:
            if " " in kw:
                if kw in title_lower:
                    hits += 1
            elif kw in tokens:
                hits += 1
        if hits > best_hits:
            best_hits = hits
            best_topic = topic

    return best_topic if best_hits >= 1 else None


def _topics_compatible(topic_a: str | None, topic_b: str | None) -> bool:
    if topic_a is None or topic_b is None:
        return True
    return topic_a == topic_b


def _extract_predicate(title: str) -> str:
    t = title.lower()
    for pred, verb_set in _PREDICATE_VERB_CLASSES.items():
        for verb in verb_set:
            if " " in verb:
                if verb in t:
                    return pred
            elif re.search(r'\b' + re.escape(verb) + r'\b', t):
                return pred
    return _PREDICATE_OTHER


def _predicates_compatible(pred_a: str, pred_b: str) -> bool:
    if pred_a == _PREDICATE_OTHER or pred_b == _PREDICATE_OTHER:
        return True
    if pred_a == pred_b:
        return True
    pair = frozenset({pred_a, pred_b})
    return pair not in _INCOMPATIBLE_PREDICATES


# --- Gate A: Strong anchor quality ---
def _classify_token_strength(token: str) -> str:
    """Classify a token as STRONG or GENERIC."""
    if token in STRONG_ENTITIES:
        return "STRONG"
    if token in GENERIC_TOKENS:
        return "GENERIC"
    # Tokens >= 4 chars that aren't in the generic list count as STRONG
    # (likely proper nouns, specific terms)
    if len(token) >= 4 and not _is_time_token(token):
        return "STRONG"
    return "GENERIC"


def _check_anchor_quality(tokens_a: set[str], tokens_b: set[str],
                           sport_a: str | None, sport_b: str | None) -> tuple[bool, set[str], set[str]]:
    """Check if shared anchors meet the strong-entity requirement.

    Returns (passed, strong_shared, generic_shared).
    Requires >= 2 STRONG overlaps, or 1 STRONG overlap if it's a curated strong entity.
    Sports bypass this check.
    """
    if sport_a or sport_b:
        shared = tokens_a & tokens_b
        strong = {t for t in shared if _classify_token_strength(t) == "STRONG"}
        generic = {t for t in shared if _classify_token_strength(t) == "GENERIC"}
        return True, strong, generic

    shared = tokens_a & tokens_b
    non_time_shared = {t for t in shared if not _is_time_token(t)}

    strong_shared = set()
    generic_shared = set()
    for t in non_time_shared:
        strength = _classify_token_strength(t)
        if strength == "STRONG":
            strong_shared.add(t)
        else:
            generic_shared.add(t)

    # Curated strong entities (in the STRONG_ENTITIES set) count double
    curated_strong = strong_shared & STRONG_ENTITIES
    # Accept if: >=2 strong tokens, or 1 curated strong + any other strong
    if len(strong_shared) >= 2:
        return True, strong_shared, generic_shared
    if len(curated_strong) >= 1 and len(strong_shared) >= 1:
        return True, strong_shared, generic_shared
    # Single curated strong entity with specific meaning is OK
    if len(curated_strong) >= 1:
        return True, strong_shared, generic_shared

    return False, strong_shared, generic_shared


# --- Gate B: Subject entity extraction ---
def _extract_subject_entity(title: str) -> str | None:
    """Extract the primary subject entity (person or country) from a title.

    Checks multi-word patterns first (e.g. "Taylor Swift"), then single words.
    Returns a normalized canonical name or None.
    """
    t_lower = title.lower()

    # Check multi-word patterns first (longest match wins)
    found_entities = []
    for pattern, canonical in sorted(_KNOWN_PEOPLE.items(), key=lambda x: -len(x[0])):
        if " " in pattern:
            if pattern in t_lower:
                found_entities.append(canonical)
                break
    if not found_entities:
        for pattern, canonical in _KNOWN_PEOPLE.items():
            if " " not in pattern:
                if re.search(r'\b' + re.escape(pattern) + r'\b', t_lower):
                    found_entities.append(canonical)
                    break

    # Also check country entities
    for pattern, canonical in sorted(_KNOWN_COUNTRY_ENTITIES.items(), key=lambda x: -len(x[0])):
        if " " in pattern:
            if pattern in t_lower:
                found_entities.append(canonical)
                break
        elif re.search(r'\b' + re.escape(pattern) + r'\b', t_lower):
            found_entities.append(canonical)
            break

    return found_entities[0] if found_entities else None


def _extract_all_subjects(title: str) -> list[str]:
    """Extract all subject entities from a title (may have multiple)."""
    t_lower = title.lower()
    found = []
    seen = set()

    # People
    for pattern, canonical in sorted(_KNOWN_PEOPLE.items(), key=lambda x: -len(x[0])):
        if canonical in seen:
            continue
        if " " in pattern:
            if pattern in t_lower:
                found.append(canonical)
                seen.add(canonical)
        elif re.search(r'\b' + re.escape(pattern) + r'\b', t_lower):
            found.append(canonical)
            seen.add(canonical)

    # Countries
    for pattern, canonical in sorted(_KNOWN_COUNTRY_ENTITIES.items(), key=lambda x: -len(x[0])):
        if canonical in seen:
            continue
        if " " in pattern:
            if pattern in t_lower:
                found.append(canonical)
                seen.add(canonical)
        elif re.search(r'\b' + re.escape(pattern) + r'\b', t_lower):
            found.append(canonical)
            seen.add(canonical)

    return found


def _subjects_compatible(subjects_a: list[str], subjects_b: list[str]) -> tuple[bool, str | None]:
    """Check if the primary subjects are compatible.

    If both titles have identified subjects, they must be the same set.
    Having one entity in common isn't enough if they differ on other entities
    (e.g. "Trump talk to Pope" vs "Taylor Swift meet Pope" — pope overlaps but
    Trump ≠ Swift, so reject).
    Returns (compatible, reason_if_rejected).
    """
    if not subjects_a or not subjects_b:
        return True, None

    set_a = set(subjects_a)
    set_b = set(subjects_b)

    # All entities from both sides must match
    only_in_a = set_a - set_b
    only_in_b = set_b - set_a

    if only_in_a and only_in_b:
        return False, f"entity_mismatch: {sorted(only_in_a)} vs {sorted(only_in_b)}"

    # One side is a subset of the other — acceptable
    return True, None


# --- Gate C: Date compatibility ---
def _extract_date_info(title: str) -> dict | None:
    """Extract date/month references from a title.

    Returns dict with month (int), day (int or None), year (int or None),
    or None if no date found.
    """
    t_lower = title.lower()

    # Try full date: "March 31", "July 1, 2025"
    match = _DATE_FULL_RE.search(t_lower)
    if match:
        month_str = match.group(0).split()[0].lower()
        month = _MONTH_MAP.get(month_str)
        day = int(match.group(1)) if match.group(1) else None
        year = int(match.group(2)) if match.group(2) else None
        return {"month": month, "day": day, "year": year,
                "summary": f"{month_str.title()} {day}" + (f", {year}" if year else "")}

    # Try month-only: "in February", "by September"
    match = _DATE_MONTH_ONLY_RE.search(t_lower)
    if match:
        month_str = None
        for m_name in _MONTH_MAP:
            if m_name in match.group(0).lower():
                month_str = m_name
                break
        if month_str:
            month = _MONTH_MAP[month_str]
            return {"month": month, "day": None, "year": None,
                    "summary": month_str.title()}

    return None


def _dates_compatible(date_a: dict | None, date_b: dict | None) -> tuple[bool, str | None]:
    """Check if two date references are compatible.

    Both must have dates for this gate to apply.
    Same month required for month-only. For exact dates, allow within 7 days.
    """
    if date_a is None or date_b is None:
        return True, None

    m_a, d_a = date_a["month"], date_a.get("day")
    m_b, d_b = date_b["month"], date_b.get("day")

    if m_a is None or m_b is None:
        return True, None

    # Different months — hard reject
    if m_a != m_b:
        return False, f"date_mismatch: {date_a['summary']} vs {date_b['summary']}"

    # Same month, check days if both present
    if d_a is not None and d_b is not None:
        if abs(d_a - d_b) > 7:
            return False, f"date_mismatch: {date_a['summary']} vs {date_b['summary']}"

    return True, None


# --- Gate D: Contract type ---
def _detect_contract_type(title: str, market_id: str = "") -> str:
    """Detect if a market is a range/inequality contract or point/strike.

    Returns "range", "point", or "unknown".
    """
    t_lower = title.lower()

    # Range/inequality language
    if _RANGE_WORDS.search(t_lower):
        return "range"
    if _RANGE_PATTERN.search(t_lower):
        return "range"

    # Strike suffix in market ID (e.g., KXTARIFF-27, KXTARIFF-30)
    if market_id and _STRIKE_SUFFIX_RE.search(market_id):
        # Kalshi ladder markets often have numeric suffixes
        if any(w in t_lower for w in ("rate", "price", "level", "percent", "%")):
            return "point"

    return "unknown"


def _contract_types_compatible(type_a: str, type_b: str) -> tuple[bool, str | None]:
    """Check if contract types are compatible.

    Range vs point is almost always a mismatch.
    """
    if type_a == "unknown" or type_b == "unknown":
        return True, None
    if type_a == type_b:
        return True, None
    return False, f"contract_type_mismatch: {type_a} vs {type_b}"


# --- Gate: Tariff domain rule ---
def _check_tariff_domain(tokens_a: set[str], tokens_b: set[str],
                          title_a: str, title_b: str) -> tuple[bool, str | None]:
    """Special tariff+rate domain rule.

    If both titles mention tariff+rate, require:
    1. Exact counterparty entity match (china, eu, canada, etc.)
    2. Date gate passes strictly
    """
    tariff_words = {"tariff", "tariffs"}
    a_has_tariff = bool(tokens_a & tariff_words)
    b_has_tariff = bool(tokens_b & tariff_words)

    if not a_has_tariff or not b_has_tariff:
        return True, None

    # Both are tariff markets — apply strict rules
    counterparties = {"china", "eu", "canada", "mexico", "japan", "india",
                      "korea", "taiwan", "russia", "uk", "germany", "france",
                      "brazil", "turkey", "vietnam", "indonesia"}

    cp_a = tokens_a & counterparties
    cp_b = tokens_b & counterparties

    if cp_a and cp_b and cp_a != cp_b:
        return False, f"tariff_counterparty_mismatch: {cp_a} vs {cp_b}"

    # Strict date check for tariff markets
    date_a = _extract_date_info(title_a)
    date_b = _extract_date_info(title_b)
    if date_a and date_b:
        compatible, reason = _dates_compatible(date_a, date_b)
        if not compatible:
            return False, f"tariff_{reason}"

    return True, None


# --- Core matching functions ---
def token_jaccard(a: str, b: str) -> float:
    ta = _tokenize(a)
    tb = _tokenize(b)
    if not ta or not tb:
        return 0.0

    ta, tb = _filter_time_tokens(ta, tb)
    if not ta or not tb:
        return 0.0

    intersection = ta & tb
    union = ta | tb
    return len(intersection) / len(union) if union else 0.0


def _levenshtein_similarity(s1: str, s2: str) -> float:
    s1 = s1.lower()
    s2 = s2.lower()
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    prev = list(range(len2 + 1))
    for i in range(1, len1 + 1):
        curr = [i] + [0] * len2
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr

    max_len = max(len1, len2)
    return 1.0 - prev[len2] / max_len


def combined_similarity(a: str, b: str) -> float:
    jaccard = token_jaccard(a, b)
    lev = _levenshtein_similarity(a, b)
    return 0.6 * jaccard + 0.4 * lev


def compute_similarity(pm: NormalizedMarket, km: NormalizedMarket) -> float:
    pm_title = _normalize_vs(pm.title)
    km_title = _normalize_vs(km.title)
    score = combined_similarity(pm_title, km_title)
    if pm.team_key and km.team_key and pm.team_key == km.team_key:
        score = max(score, 0.90)
    return score


def _expiry_close_enough(pm: NormalizedMarket, km: NormalizedMarket) -> bool:
    if pm.expiryTs <= 0 or km.expiryTs <= 0:
        return True
    sport = pm.sport or km.sport or ""
    gate = EXPIRY_GATES.get(sport, DEFAULT_EXPIRY_GATE)
    return abs(pm.expiryTs - km.expiryTs) <= gate


def _evaluate_candidate(pm_title_norm: str, km_title_norm: str,
                         pm: NormalizedMarket, km: NormalizedMarket,
                         pm_pred: str, km_pred: str) -> dict:
    """Evaluate a candidate pair with all hardening gates.

    Returns debug dict with full scoring details and accept/reject reasons.
    """
    tokens_a = _tokenize(pm_title_norm)
    tokens_b = _tokenize(km_title_norm)
    tokens_a_filtered, tokens_b_filtered = _filter_time_tokens(tokens_a, tokens_b)

    topic_a = _classify_topic(tokens_a, pm_title_norm.lower())
    topic_b = _classify_topic(tokens_b, km_title_norm.lower())

    # Compute raw scores
    intersection = tokens_a_filtered & tokens_b_filtered
    union = tokens_a_filtered | tokens_b_filtered
    jaccard = len(intersection) / len(union) if union else 0.0
    lev = _levenshtein_similarity(pm_title_norm, km_title_norm)
    final_score = 0.6 * jaccard + 0.4 * lev

    if pm.team_key and km.team_key and pm.team_key == km.team_key:
        final_score = max(final_score, 0.90)

    # Extract gate inputs
    subjects_a = _extract_all_subjects(pm_title_norm)
    subjects_b = _extract_all_subjects(km_title_norm)
    date_a = _extract_date_info(pm_title_norm)
    date_b = _extract_date_info(km_title_norm)
    contract_a = _detect_contract_type(pm_title_norm, pm.marketId)
    contract_b = _detect_contract_type(km_title_norm, km.marketId)

    # Gate A: Anchor quality
    anchor_ok, strong_shared, generic_shared = _check_anchor_quality(
        tokens_a_filtered, tokens_b_filtered, pm.sport, km.sport
    )

    reject_reasons = []
    accept_reasons = []

    # Gate: Predicate compatibility
    if not _predicates_compatible(pm_pred, km_pred):
        reject_reasons.append(f"predicate_conflict: {pm_pred} vs {km_pred}")
        final_score = 0.0

    # Gate: Topic compatibility
    if not _topics_compatible(topic_a, topic_b):
        reject_reasons.append(f"topic_mismatch: {topic_a} vs {topic_b}")
        final_score = 0.0

    # Gate A: Strong anchor quality
    if not anchor_ok:
        reject_reasons.append(f"weak_anchors_only: shared_generic={sorted(generic_shared)}")
        final_score = 0.0

    # Gate B: Subject entity match
    subj_ok, subj_reason = _subjects_compatible(subjects_a, subjects_b)
    if not subj_ok:
        reject_reasons.append(subj_reason)
        final_score = 0.0

    # Gate C: Date compatibility
    date_ok, date_reason = _dates_compatible(date_a, date_b)
    if not date_ok:
        reject_reasons.append(date_reason)
        final_score = 0.0

    # Gate D: Contract type
    ct_ok, ct_reason = _contract_types_compatible(contract_a, contract_b)
    if not ct_ok:
        reject_reasons.append(ct_reason)
        final_score = 0.0

    # Tariff domain rule
    tariff_ok, tariff_reason = _check_tariff_domain(
        tokens_a_filtered, tokens_b_filtered, pm_title_norm, km_title_norm
    )
    if not tariff_ok:
        reject_reasons.append(tariff_reason)
        final_score = 0.0

    # Build accept reasons
    if final_score > 0 and not reject_reasons:
        if pm.team_key and km.team_key and pm.team_key == km.team_key:
            accept_reasons.append("team_key_match")
        if strong_shared:
            accept_reasons.append(f"strong_anchors: {', '.join(sorted(strong_shared)[:5])}")
        if topic_a and topic_a == topic_b:
            accept_reasons.append(f"same_topic: {topic_a}")
        if subjects_a and subjects_b and set(subjects_a) & set(subjects_b):
            accept_reasons.append(f"same_subject: {list(set(subjects_a) & set(subjects_b))}")

    return {
        "tokensA": sorted(tokens_a),
        "tokensB": sorted(tokens_b),
        "sharedAnchors": sorted(strong_shared),
        "genericShared": sorted(generic_shared),
        "topicA": topic_a,
        "topicB": topic_b,
        "predicateA": pm_pred,
        "predicateB": km_pred,
        "subjectA": subjects_a if subjects_a else None,
        "subjectB": subjects_b if subjects_b else None,
        "dateA": date_a["summary"] if date_a else None,
        "dateB": date_b["summary"] if date_b else None,
        "contractTypeA": contract_a,
        "contractTypeB": contract_b,
        "jaccard": round(jaccard, 4),
        "levenshtein": round(lev, 4),
        "finalScore": round(final_score, 4),
        "whyAccepted": "; ".join(accept_reasons) if accept_reasons else None,
        "whyRejected": "; ".join(reject_reasons) if reject_reasons else None,
    }


def _apply_hard_gates(pm_title_norm: str, km_title_norm: str,
                       pm: NormalizedMarket, km: NormalizedMarket,
                       tokens_a_filtered: set[str], tokens_b_filtered: set[str]) -> str | None:
    """Apply all hard gates quickly. Returns rejection reason or None if passed."""

    # Gate A: anchor quality
    anchor_ok, _, _ = _check_anchor_quality(
        tokens_a_filtered, tokens_b_filtered, pm.sport, km.sport
    )
    if not anchor_ok:
        return "weak_anchors"

    # Gate B: subject entity
    subjects_a = _extract_all_subjects(pm_title_norm)
    subjects_b = _extract_all_subjects(km_title_norm)
    subj_ok, _ = _subjects_compatible(subjects_a, subjects_b)
    if not subj_ok:
        return "entity_mismatch"

    # Gate C: date compatibility
    date_a = _extract_date_info(pm_title_norm)
    date_b = _extract_date_info(km_title_norm)
    date_ok, _ = _dates_compatible(date_a, date_b)
    if not date_ok:
        return "date_mismatch"

    # Gate D: contract type
    ct_a = _detect_contract_type(pm_title_norm, pm.marketId)
    ct_b = _detect_contract_type(km_title_norm, km.marketId)
    ct_ok, _ = _contract_types_compatible(ct_a, ct_b)
    if not ct_ok:
        return "contract_type_mismatch"

    # Tariff domain rule
    tariff_ok, _ = _check_tariff_domain(
        tokens_a_filtered, tokens_b_filtered, pm_title_norm, km_title_norm
    )
    if not tariff_ok:
        return "tariff_rule"

    return None


def find_pairs(poly_markets: list[NormalizedMarket], kalshi_markets: list[NormalizedMarket],
               min_similarity: float = 0.35) -> list[dict]:
    pairs = []
    used_k: set[int] = set()

    for pm in poly_markets:
        if not pm.title:
            continue

        pm_title_norm = _normalize_vs(pm.title)
        pm_pred = _extract_predicate(pm.title)
        pm_tokens = _tokenize(pm_title_norm)
        pm_tokens_filtered, _ = _filter_time_tokens(pm_tokens, pm_tokens)
        pm_topic = _classify_topic(pm_tokens, pm_title_norm.lower())

        candidates = []
        for i, km in enumerate(kalshi_markets):
            if i in used_k:
                continue
            if not km.title:
                continue

            if not _expiry_close_enough(pm, km):
                continue

            km_pred = _extract_predicate(km.title)
            if not _predicates_compatible(pm_pred, km_pred):
                continue

            km_title_norm = _normalize_vs(km.title)
            km_tokens = _tokenize(km_title_norm)
            km_topic = _classify_topic(km_tokens, km_title_norm.lower())

            if not _topics_compatible(pm_topic, km_topic):
                continue

            pm_filt, km_filt = _filter_time_tokens(pm_tokens, km_tokens)

            # Apply all hard gates early (cheap reject)
            gate_reject = _apply_hard_gates(
                pm_title_norm, km_title_norm, pm, km, pm_filt, km_filt
            )
            if gate_reject:
                continue

            intersection = pm_filt & km_filt
            union = pm_filt | km_filt
            jaccard = len(intersection) / len(union) if union else 0.0

            if pm.team_key and km.team_key and pm.team_key == km.team_key:
                jaccard = max(jaccard, 0.50)

            if jaccard >= MIN_JACCARD_FOR_LEVENSHTEIN:
                candidates.append((i, km, jaccard, km_title_norm))

        candidates.sort(key=lambda x: x[2], reverse=True)
        top_candidates = candidates[:JACCARD_SHORTLIST_K]

        best_match = None
        best_score = 0.0

        for i, km, jaccard, km_title_norm in top_candidates:
            lev = _levenshtein_similarity(pm_title_norm, km_title_norm)
            score = 0.6 * jaccard + 0.4 * lev

            if pm.team_key and km.team_key and pm.team_key == km.team_key:
                score = max(score, 0.90)

            km_pred = _extract_predicate(km.title)
            if pm_pred != km_pred and pm_pred != _PREDICATE_OTHER and km_pred != _PREDICATE_OTHER:
                if score < 0.75:
                    continue

            if score > best_score and score >= min_similarity:
                best_score = score
                best_match = (i, km)

        if best_match:
            idx, km = best_match
            used_k.add(idx)

            if not pm.yesTokenId or not pm.noTokenId:
                log(f"Skipping pair (Polymarket tokens incomplete): {pm.marketId}")
                continue

            if not km.yesTokenId or not km.noTokenId:
                log(f"Skipping pair (Kalshi tokens incomplete): {km.marketId}")
                continue

            pair_id = f"polymarket:{pm.marketId}___kalshi:{km.marketId}"
            sport = pm.sport or km.sport
            expiry = pm.expiryTs or km.expiryTs

            pairs.append({
                "pair_id": pair_id,
                "title": pm.title,
                "kalshi_title": km.title,
                "sport": sport,
                "expiry_ts": expiry or 0,
                "similarity": round(best_score, 3),
                "polymarket_url": pm.meta.get("url", ""),
                "kalshi_url": km.meta.get("url", ""),
                "kalshi_ticker": km.marketId,
                "polymarket": {
                    "venue": "polymarket",
                    "id": pm.marketId,
                    "question": pm.title,
                    "team_key": pm.team_key,
                    "yes_token": pm.yesTokenId,
                    "no_token": pm.noTokenId,
                    "expiry_ts": pm.expiryTs,
                    "sport": pm.sport,
                    "volume": pm.meta.get("volume", 0),
                },
                "kalshi": {
                    "venue": "kalshi",
                    "id": km.marketId,
                    "question": km.title,
                    "team_key": km.team_key,
                    "yes_token": km.yesTokenId,
                    "no_token": km.noTokenId,
                    "expiry_ts": km.expiryTs,
                    "sport": km.sport,
                    "volume": km.meta.get("volume", 0),
                },
            })

    log(f"Found {len(pairs)} matched pairs from {len(poly_markets)} Poly x {len(kalshi_markets)} Kalshi markets")
    return pairs


def debug_match_candidates(poly_markets: list[NormalizedMarket],
                            kalshi_markets: list[NormalizedMarket],
                            top_n: int = 50) -> list[dict]:
    """Generate detailed debug output for match candidates.

    Returns top accepted + interesting rejected candidates with full scoring details.
    """
    candidates = []

    for pm in poly_markets[:200]:
        if not pm.title:
            continue
        pm_title_norm = _normalize_vs(pm.title)
        pm_pred = _extract_predicate(pm.title)

        for km in kalshi_markets[:200]:
            if not km.title:
                continue
            km_title_norm = _normalize_vs(km.title)
            km_pred = _extract_predicate(km.title)

            eval_result = _evaluate_candidate(
                pm_title_norm, km_title_norm, pm, km, pm_pred, km_pred
            )

            # Include candidates with any score or any rejection reason
            if eval_result["finalScore"] > 0.05 or eval_result["whyRejected"]:
                candidates.append({
                    "polyTitle": pm.title,
                    "kalshiTitle": km.title,
                    "polyId": pm.marketId,
                    "kalshiId": km.marketId,
                    **eval_result,
                })

    candidates.sort(key=lambda x: x["finalScore"], reverse=True)

    accepted = [c for c in candidates if c["whyRejected"] is None and c["finalScore"] >= 0.35]
    rejected_interesting = [c for c in candidates if c["whyRejected"] is not None][:25]

    result = accepted[:top_n]
    remaining = top_n - len(result)
    if remaining > 0:
        result.extend(rejected_interesting[:remaining])

    return result[:top_n]

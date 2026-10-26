"""Whether Polymarket trading is permitted from this host.

Polymarket publishes https://polymarket.com/api/geoblock ({blocked, ip, country, region}) and refuses
orders from restricted jurisdictions. The check always uses this host's DIRECT egress: a proxy or VPN
must never be used to change the answer, because that would circumvent the restriction. Any answer
other than an explicit blocked=false (blocked, HTTP error, unparsable body, timeout) means trading is
unavailable, so live execution fails closed. Shadow mode only records the status.
"""

import threading
import time

import requests

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
_TTL = 600
_cache: dict = {}
_lock = threading.Lock()


def _direct_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # ignore HTTP(S)_PROXY / ALL_PROXY: the host's own location decides
    return s


def polymarket_trading_status(session=None, now=None) -> dict:
    """{available, blocked, country, region, reason, checked_at}; cached for 10 minutes."""
    now = now or time.time()
    with _lock:
        if _cache and now - _cache["checked_at"] < _TTL and session is None:
            return dict(_cache)
    out = {"available": False, "blocked": None, "country": None, "region": None, "checked_at": now}
    try:
        r = (session or _direct_session()).get(GEOBLOCK_URL, timeout=10)
        body = r.json() if r.status_code == 200 else None
        if not isinstance(body, dict) or "blocked" not in body:
            out["reason"] = f"geoblock check inconclusive (HTTP {r.status_code})"
        else:
            out.update(blocked=bool(body["blocked"]), country=body.get("country"), region=body.get("region"))
            out["available"] = body["blocked"] is False
            out["reason"] = ("not geoblocked" if out["available"]
                             else f"Polymarket geoblocks this host ({out['country']}/{out['region']})")
    except Exception as e:
        out["reason"] = f"geoblock check failed ({type(e).__name__})"
    with _lock:
        _cache.clear()
        _cache.update(out)
    return dict(out)


def clear_cache() -> None:
    with _lock:
        _cache.clear()

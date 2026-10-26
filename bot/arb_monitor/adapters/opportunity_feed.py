"""Which data provider feeds the execution loop.

ARB_OPPORTUNITY_FEED:
  oddpool          (default) Oddpool /arbitrage/current, unchanged.
  prediction_com   Prediction.com /v2/arb (adapters/prediction_com.py).
  compare          Fetch both every cycle, trade only from ARB_FEED_COMPARE_PRIMARY (default oddpool)
                   and append one JSON line per cycle to ARB_FEED_COMPARE_LOG describing the overlap,
                   so the two can be compared on live data before Oddpool is switched off.

Without PREDICTION_COM_API_KEY, Oddpool stays the feed: prediction_com mode falls back to oddpool, and
compare mode fetches Oddpool only and trades from it whatever ARB_FEED_COMPARE_PRIMARY says.

Both providers produce the same ArbOpportunity; the executor re-verifies every pair against the
venues' live order books regardless of the source.
"""

import json
import os
import time

from .oddpool import ArbOpportunity


def log(msg: str):
    print(f"🛰 [Arb/Feed] {msg}")


FEEDS = ("oddpool", "prediction_com", "compare")


def feed_mode() -> str:
    mode = os.environ.get("ARB_OPPORTUNITY_FEED", "oddpool").strip().lower()
    if mode not in FEEDS:
        raise ValueError(f"ARB_OPPORTUNITY_FEED={mode!r}; expected one of {FEEDS}")
    return mode


def _fetch(name: str) -> list[ArbOpportunity]:
    if name == "prediction_com":
        from .prediction_com import fetch_opportunities as f
    else:
        from .oddpool import fetch_opportunities as f
    return f()


def _key(o: ArbOpportunity) -> str:
    """The Polymarket token actually bought: identical across providers for the same trade."""
    if o.is_display_only:
        return ""
    return o.poly_no_token if o.kalshi_side == "YES" else o.poly_yes_token


def compare_summary(primary: str, a: list[ArbOpportunity], b: list[ArbOpportunity]) -> dict:
    ka = {_key(o): o for o in a if _key(o)}
    kb = {_key(o): o for o in b if _key(o)}
    both = sorted(set(ka) & set(kb))
    return {
        "ts": int(time.time()),
        "primary": primary,
        "oddpool_total": len(a), "prediction_com_total": len(b),
        "oddpool_executable": len(ka), "prediction_com_executable": len(kb),
        "both": len(both),
        "only_oddpool": len(set(ka) - set(kb)),
        "only_prediction_com": len(set(kb) - set(ka)),
        "pairs": [
            {
                "token": k[:18], "kalshi": kb[k].kalshi_ticker,
                "oddpool_net": round(ka[k].net_edge_pct, 3), "prediction_com_net": round(kb[k].net_edge_pct, 3),
                "oddpool_size": round(ka[k].fillable_size_usdc, 2), "prediction_com_size": round(kb[k].fillable_size_usdc, 2),
            }
            for k in both[:25]
        ],
        "top_only_prediction_com": [
            {"pair": o.pair_id, "net": round(o.net_edge_pct, 3), "size": round(o.fillable_size_usdc, 2)}
            for k, o in sorted(kb.items(), key=lambda kv: -kv[1].score) if k not in ka
        ][:10],
    }


def _prediction_com_configured() -> bool:
    return bool(os.environ.get("PREDICTION_COM_API_KEY", "").strip())


def fetch_opportunities() -> list[ArbOpportunity]:
    mode = feed_mode()
    if mode == "prediction_com" and not _prediction_com_configured():
        log("ℹ️ PREDICTION_COM_API_KEY not set — using Oddpool")
        mode = "oddpool"
    if mode != "compare":
        return _fetch(mode)
    primary = os.environ.get("ARB_FEED_COMPARE_PRIMARY", "oddpool").strip().lower()
    if primary not in ("oddpool", "prediction_com"):
        raise ValueError(f"ARB_FEED_COMPARE_PRIMARY={primary!r}")
    if not _prediction_com_configured():
        log("ℹ️ PREDICTION_COM_API_KEY not set — compare mode runs on Oddpool only")
        return _fetch("oddpool")
    res: dict[str, list] = {}
    for name in ("oddpool", "prediction_com"):
        try:
            res[name] = _fetch(name)
        except Exception as e:
            log(f"⚠️ {name} failed in compare mode: {e}")
            res[name] = []
    summary = compare_summary(primary, res["oddpool"], res["prediction_com"])
    log(
        f"compare: oddpool={summary['oddpool_executable']} prediction_com={summary['prediction_com_executable']} "
        f"both={summary['both']} (trading from {primary})"
    )
    path = os.environ.get("ARB_FEED_COMPARE_LOG", "")
    if not path:
        log("ℹ️ ARB_FEED_COMPARE_LOG not set — comparison not written")
        return res[primary]
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(summary) + "\n")
    except OSError as e:
        log(f"⚠️ could not write {path}: {e}")
    return res[primary]

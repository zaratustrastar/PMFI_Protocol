"""In-memory storage with TTL cache for arb scan results."""

import time
import threading
from typing import Any


class ArbCache:
    """Thread-safe in-memory cache with TTL for arb scan results."""

    def __init__(self, ttl_seconds: int = 45):
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._data: dict[str, Any] = {}
        self._timestamps: dict[str, float] = {}

    def get(self, key: str) -> Any | None:
        with self._lock:
            ts = self._timestamps.get(key)
            if ts is None:
                return None
            if time.time() - ts > self._ttl:
                del self._data[key]
                del self._timestamps[key]
                return None
            return self._data.get(key)

    def set(self, key: str, value: Any):
        with self._lock:
            self._data[key] = value
            self._timestamps[key] = time.time()

    def clear(self):
        with self._lock:
            self._data.clear()
            self._timestamps.clear()


class ArbResultStore:
    """Stores the latest arb scan results."""

    def __init__(self):
        self._lock = threading.Lock()
        self._results: dict[str, Any] = {
            "asOf": 0,
            "opportunities": [],
            "watchlist": [],
            "pairsTracked": 0,
            "lastScanMs": 0,
            "scanCount": 0,
            "lastError": None,
            "status": "initializing",
        }

    def update(self, opportunities: list[dict], watchlist: list[dict],
               pairs_tracked: int, scan_ms: int):
        with self._lock:
            self._results = {
                "asOf": int(time.time()),
                "opportunities": opportunities,
                "watchlist": watchlist,
                "pairsTracked": pairs_tracked,
                "lastScanMs": scan_ms,
                "scanCount": self._results.get("scanCount", 0) + 1,
                "lastError": None,
                "status": "ok",
            }

    def set_error(self, error: str):
        with self._lock:
            self._results["lastError"] = error
            self._results["status"] = "error"

    def get_results(self) -> dict:
        with self._lock:
            return dict(self._results)

    def get_health(self) -> dict:
        with self._lock:
            return {
                "status": self._results.get("status", "unknown"),
                "asOf": self._results.get("asOf", 0),
                "scanCount": self._results.get("scanCount", 0),
                "lastScanMs": self._results.get("lastScanMs", 0),
                "pairsTracked": self._results.get("pairsTracked", 0),
                "opportunityCount": len(self._results.get("opportunities", [])),
                "watchlistCount": len(self._results.get("watchlist", [])),
                "lastError": self._results.get("lastError"),
            }


arb_cache = ArbCache()
arb_store = ArbResultStore()

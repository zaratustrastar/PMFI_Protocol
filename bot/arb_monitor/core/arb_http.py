"""arb_http.py — bounded-timeout JSON requests with retry/backoff for IDEMPOTENT calls only
(bridge quotes, bridge status). Never use it for venue withdrawal POSTs."""

import time

import requests

from ..config import http_timeout_secs, http_retries, http_backoff_secs


def log(msg: str):
    print(f"🌐 [ArbHttp] {msg}")


def request_json(method: str, url: str, *, params=None, json_body=None,
                 timeout: float | None = None, retries: int | None = None):
    """GET/POST returning parsed JSON. Retries network errors, HTTP 429 and 5xx with exponential
    backoff; any other 4xx raises immediately. Raises RuntimeError after the last attempt."""
    timeout = http_timeout_secs() if timeout is None else timeout
    retries = max(0, http_retries() if retries is None else retries)
    backoff = http_backoff_secs()
    last = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(backoff * (2 ** (attempt - 1)))
        try:
            if method.upper() == "GET":
                resp = requests.get(url, params=params, timeout=timeout)
            else:
                resp = requests.post(url, json=json_body, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last = f"{type(e).__name__}: {e}"
            log(f"⚠️ {method} {url} attempt {attempt + 1}/{retries + 1} failed: {last}")
            continue
        status = getattr(resp, "status_code", 200)
        if status == 429 or status >= 500:
            last = f"HTTP {status}"
            log(f"⚠️ {method} {url} attempt {attempt + 1}/{retries + 1}: {last}")
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"{method} {url} failed after {retries + 1} attempts: {last}")

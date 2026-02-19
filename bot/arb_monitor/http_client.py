"""Shared HTTP client with retries, backoff, and optional proxy support."""

import os
import time
import requests
from typing import Optional


PROXY_ENABLED_POLY = os.environ.get("PROXY_ENABLED_POLY", "0") == "1"
PROXY_ENABLED_OPINION = os.environ.get("PROXY_ENABLED_OPINION", "0") == "1"

HTTP_PROXY = os.environ.get("HTTP_PROXY", "")
HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "")

DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DEFAULT_TIMEOUT = 15
DEFAULT_MAX_RETRIES = 3
BACKOFF_BASE = 1.5


def _build_proxies(venue: str) -> Optional[dict]:
    enabled = PROXY_ENABLED_POLY if venue == "polymarket" else PROXY_ENABLED_OPINION
    if not enabled:
        return None
    proxies = {}
    if HTTP_PROXY:
        proxies["http"] = HTTP_PROXY
    if HTTPS_PROXY:
        proxies["https"] = HTTPS_PROXY
    return proxies if proxies else None


def get(url: str, *,
        venue: str = "generic",
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES) -> Optional[requests.Response]:
    final_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        final_headers.update(headers)

    proxies = _build_proxies(venue)

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                url,
                params=params,
                headers=final_headers,
                proxies=proxies,
                timeout=timeout,
            )

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = BACKOFF_BASE ** (attempt + 1)
                print(f"⏳ [HTTP] {venue} {resp.status_code} on {url}, retry in {wait:.1f}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                last_error = f"HTTP {resp.status_code}"
                continue

            return resp

        except requests.exceptions.Timeout as e:
            wait = BACKOFF_BASE ** (attempt + 1)
            print(f"⏳ [HTTP] {venue} timeout on {url}, retry in {wait:.1f}s (attempt {attempt+1}/{max_retries})")
            time.sleep(wait)
            last_error = str(e)
        except requests.exceptions.RequestException as e:
            wait = BACKOFF_BASE ** (attempt + 1)
            print(f"⏳ [HTTP] {venue} error on {url}: {e}, retry in {wait:.1f}s (attempt {attempt+1}/{max_retries})")
            time.sleep(wait)
            last_error = str(e)

    print(f"❌ [HTTP] {venue} exhausted {max_retries} retries for {url}: {last_error}")
    return None

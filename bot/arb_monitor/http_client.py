"""Shared HTTP client with session pooling, proxy support, retry/backoff, and Cloudflare detection.

Proxy configuration via environment variables:
  export HTTP_PROXY=http://user:pass@proxy-host:port
  export HTTPS_PROXY=http://user:pass@proxy-host:port
  export NO_PROXY=localhost,127.0.0.1

If HTTP_PROXY or HTTPS_PROXY is set, ALL arb monitor requests route through the proxy.
requests library honors NO_PROXY automatically when set in os.environ.

Non-JSON responses (e.g. Cloudflare HTML challenge pages) are detected and logged.
"""

import os
import time
import requests
from typing import Optional


HTTP_PROXY = os.environ.get("HTTP_PROXY", "")
HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "")

DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DEFAULT_TIMEOUT = 10
DEFAULT_MAX_RETRIES = 3
BACKOFF_BASE = 1.5

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        proxies = {}
        if HTTP_PROXY:
            proxies["http"] = HTTP_PROXY
        if HTTPS_PROXY:
            proxies["https"] = HTTPS_PROXY
        if proxies:
            _session.proxies.update(proxies)
            print(f"🌐 [HTTP] Proxy configured: http={'yes' if HTTP_PROXY else 'no'}, https={'yes' if HTTPS_PROXY else 'no'}")
        else:
            print("🌐 [HTTP] No proxy configured (direct connections)")
    return _session


def _is_json_response(resp: requests.Response) -> bool:
    ct = resp.headers.get("Content-Type", "")
    return "application/json" in ct


def _detect_cloudflare_block(resp: requests.Response, venue: str, url: str) -> bool:
    """Check if response is a Cloudflare HTML challenge instead of JSON.

    Returns True if blocked (caller should treat as failure).
    """
    if _is_json_response(resp):
        return False
    ct = resp.headers.get("Content-Type", "")
    body_preview = resp.text[:200] if resp.text else "(empty)"
    print(f"⚠️ [HTTP] {venue} non-JSON response from {url} (Content-Type: {ct}): {body_preview}")
    return True


def get(url: str, *,
        venue: str = "generic",
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES) -> Optional[requests.Response]:

    session = _get_session()
    req_headers = {}
    if headers:
        req_headers.update(headers)

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = session.get(
                url,
                params=params,
                headers=req_headers,
                timeout=timeout,
            )

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = BACKOFF_BASE ** (attempt + 1)
                print(f"⏳ [HTTP] {venue} {resp.status_code} on {url}, retry in {wait:.1f}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                last_error = f"HTTP {resp.status_code}"
                continue

            if resp.status_code == 403:
                if _detect_cloudflare_block(resp, venue, url):
                    print(f"🛡️ [HTTP] {venue} Cloudflare block on {url} (403). Proxy may be required.")
                    return None
                return None

            if resp.status_code != 200:
                print(f"⚠️ [HTTP] {venue} {resp.status_code} on {url}")
                return None

            if _detect_cloudflare_block(resp, venue, url):
                return None

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

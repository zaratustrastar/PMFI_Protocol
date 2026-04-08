"""Shared HTTP client with session pooling, proxy support, retry/backoff, and Cloudflare detection.

Proxy configuration (checked in order of priority):
  1. HTTP_PROXY / HTTPS_PROXY  — standard env vars
  2. PROXY_URL                 — single URL used for both http and https (supports socks5://)

Opinion-specific proxy:
  OPINION_PROXY_URL — separate residential proxy for Opinion Labs (Serbian IP).
  Opinion blocks US/restricted-jurisdiction IPs at the API level.
  All opinion.py calls use opinion_get() which routes through this proxy.
  opinion_clob_sdk also gets its opinion.trade requests proxied via HTTPAdapter patch.

Cloudflare detection: Only blocks responses that are clearly HTML challenge pages.
Valid JSON is accepted regardless of Content-Type header.

Proxy routing:
  gamma-api.polymarket.com (Cloudflare-protected) → Spain proxy (PROXY_URL)
  Opinion REST API (proxy.opinion.trade:8443)      → Serbian proxy (OPINION_PROXY_URL)
  Kalshi + clob.polymarket.com                     → direct (bypass_proxy=True)
"""

import os
import time
import requests
from typing import Optional


HTTP_PROXY = os.environ.get("HTTP_PROXY", "")
HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "")
PROXY_URL = os.environ.get("PROXY_URL", "")
OPINION_PROXY_URL = os.environ.get("OPINION_PROXY_URL", "")

DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DEFAULT_TIMEOUT = 10
DEFAULT_MAX_RETRIES = 3
BACKOFF_BASE = 1.5

_session: Optional[requests.Session] = None
_direct_session: Optional[requests.Session] = None
_opinion_session: Optional[requests.Session] = None


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
        if not proxies and PROXY_URL:
            proxies["http"] = PROXY_URL
            proxies["https"] = PROXY_URL
        if proxies:
            _session.proxies.update(proxies)
            proxy_display = list(proxies.values())[0]
            if "@" in proxy_display:
                proxy_display = proxy_display.split("@")[1]
            print(f"🌐 [HTTP] Proxy configured: {proxy_display} (source: {'PROXY_URL' if PROXY_URL and not HTTP_PROXY and not HTTPS_PROXY else 'HTTP(S)_PROXY'})")
        else:
            print("🌐 [HTTP] No proxy configured (direct connections)")
    return _session


def _get_direct_session() -> requests.Session:
    """Return a session that always connects directly, bypassing any configured proxy.

    trust_env=False ensures HTTP_PROXY/HTTPS_PROXY env vars are also ignored,
    so bypass_proxy=True is guaranteed to be truly proxy-free regardless of
    how the environment is configured.
    """
    global _direct_session
    if _direct_session is None:
        _direct_session = requests.Session()
        _direct_session.trust_env = False
        _direct_session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    return _direct_session


_opinion_session_proxy: str = ""  # tracks which proxy URL the current session was built for


def _under_proxychains() -> bool:
    """Return True when the process is wrapped by proxychains4.

    proxychains injects itself via LD_PRELOAD. When active it is already
    routing ALL outbound TCP through the configured SOCKS/HTTP chain at the
    OS level, so setting session.proxies would send traffic through the proxy
    server TWICE (application-level → proxychains TCP-level → proxy again),
    creating a circular connection that always times out.
    """
    return "proxychains" in os.environ.get("LD_PRELOAD", "").lower()


def _get_opinion_session() -> requests.Session:
    """Return a session for Opinion Labs API calls.

    Proxy strategy (mutually exclusive):
      A) proxychains active  — trust_env=False, no session.proxies.
         proxychains already routes all TCP (including Opinion) through the
         Serbian residential proxy at the OS level. Adding session.proxies on
         top causes a circular double-proxy loop → timeout.
      B) proxychains absent  — trust_env=False, session.proxies = OPINION_PROXY_URL.
         Application-level proxy routes Opinion through Serbian residential IP
         to bypass geo-blocking.

    Reads OPINION_PROXY_URL from the environment on each call so runtime
    changes are picked up on the next call without restart.
    """
    global _opinion_session, _opinion_session_proxy
    current = os.environ.get("OPINION_PROXY_URL", "")
    if _opinion_session is not None and current != _opinion_session_proxy:
        print(f"🌐 [HTTP] Opinion proxy changed — resetting session")
        _opinion_session = None
    if _opinion_session is None:
        _opinion_session_proxy = current
        _opinion_session = requests.Session()
        _opinion_session.trust_env = False
        _opinion_session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        if _under_proxychains():
            print(
                "🌐 [HTTP] Opinion session: proxychains detected — skipping session.proxies "
                "(TCP already routed through Serbian proxy at OS level)"
            )
        elif current:
            _opinion_session.proxies.update({"http": current, "https": current})
            host_display = current.split("@")[-1] if "@" in current else current
            print(f"🌐 [HTTP] Opinion proxy configured: {host_display} (source: OPINION_PROXY_URL)")
        else:
            print("🌐 [HTTP] No Opinion proxy configured — Opinion calls will go direct (may be blocked)")
    return _opinion_session


def _looks_like_json(resp: requests.Response) -> bool:
    """Check if the response is likely JSON — by Content-Type or body shape."""
    ct = resp.headers.get("Content-Type", "")
    if "json" in ct.lower():
        return True
    body = resp.text.strip()[:2] if resp.text else ""
    return body in ("{", "[", '{"', '[{')


def _is_html_block(resp: requests.Response) -> bool:
    """Detect Cloudflare / WAF HTML challenge pages.

    Only returns True when the body is clearly HTML, not just missing Content-Type.
    """
    ct = resp.headers.get("Content-Type", "").lower()
    body_start = resp.text.strip()[:200].lower() if resp.text else ""
    if "text/html" in ct and ("<html" in body_start or "<!doctype" in body_start):
        return True
    if not ct and ("<html" in body_start or "<!doctype" in body_start):
        return True
    return False


def get(url: str, *,
        venue: str = "generic",
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        bypass_proxy: bool = False) -> Optional[requests.Response]:

    session = _get_direct_session() if bypass_proxy else _get_session()
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
                if _is_html_block(resp):
                    body_preview = resp.text[:200] if resp.text else "(empty)"
                    print(f"🛡️ [HTTP] {venue} Cloudflare block on {url} (403): {body_preview}")
                else:
                    print(f"⚠️ [HTTP] {venue} 403 Forbidden on {url}")
                return None

            if resp.status_code != 200:
                print(f"⚠️ [HTTP] {venue} {resp.status_code} on {url}")
                return None

            if _is_html_block(resp):
                body_preview = resp.text[:200] if resp.text else "(empty)"
                print(f"🛡️ [HTTP] {venue} HTML block on 200 from {url}: {body_preview}")
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


def opinion_get(url: str, *,
                venue: str = "opinion",
                params: Optional[dict] = None,
                headers: Optional[dict] = None,
                timeout: int = DEFAULT_TIMEOUT,
                max_retries: int = DEFAULT_MAX_RETRIES) -> Optional[requests.Response]:
    """GET via the Opinion-specific proxy session (Serbian residential proxy).

    Drop-in replacement for get(..., bypass_proxy=True) in opinion.py.
    Routes through OPINION_PROXY_URL when set; falls back to direct if not configured.
    Kalshi is unaffected — it uses bypass_proxy=True via the separate direct session.
    """
    session = _get_opinion_session()
    req_headers = {}
    if headers:
        req_headers.update(headers)

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, headers=req_headers, timeout=timeout)

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = BACKOFF_BASE ** (attempt + 1)
                print(f"⏳ [HTTP] {venue} {resp.status_code} on {url}, retry in {wait:.1f}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                last_error = f"HTTP {resp.status_code}"
                continue

            if resp.status_code == 403:
                if _is_html_block(resp):
                    body_preview = resp.text[:200] if resp.text else "(empty)"
                    print(f"🛡️ [HTTP] {venue} block on {url} (403): {body_preview}")
                else:
                    print(f"⚠️ [HTTP] {venue} 403 Forbidden on {url}")
                return None

            if resp.status_code != 200:
                print(f"⚠️ [HTTP] {venue} {resp.status_code} on {url}")
                return None

            if _is_html_block(resp):
                body_preview = resp.text[:200] if resp.text else "(empty)"
                print(f"🛡️ [HTTP] {venue} HTML block on 200 from {url}: {body_preview}")
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

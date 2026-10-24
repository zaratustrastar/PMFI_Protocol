"""HTTP proxy injection for Polymarket CLOB requests (V1/V2 compatible).

Two transports reach clob*.polymarket.com and both must be patched:
  - `requests` (order books, prices, the V1 SDK): HTTPAdapter.send is wrapped, host-matched.
  - py-clob-client-v2 (orders, cancels, balances, API-key auth): it uses its own module-level
    httpx.Client in py_clob_client_v2.http_helpers.helpers, which the requests patch never sees.
    That client is replaced by one with the same settings plus the proxy. The SDK only talks to the
    CLOB host, so proxying the whole client is equivalent to host matching.
    POLY_CLOB_PROXY_SDK=false keeps the SDK direct (previous behaviour).
"""

import os
import re

POLY_CLOB_HOST_RE = re.compile(r"clob[\w\-]*\.polymarket\.com", re.IGNORECASE)

_PATCHED = False
SDK_ROUTE = "direct"  # "proxy" once the V2 SDK's httpx client goes through the proxy


def _redact(url: str) -> str:
    return re.sub(r"//[^@]+@", "//<redacted>@", url)


def patch_sdk_httpx(proxy_url: str) -> bool:
    """Route py-clob-client-v2's httpx client through proxy_url. False when the SDK or a proxy
    transport is unavailable (logged); the SDK then stays direct."""
    global SDK_ROUTE
    try:
        import httpx
        from py_clob_client_v2.http_helpers import helpers
    except ImportError:
        return False
    try:
        try:
            client = httpx.Client(http2=True, proxy=proxy_url)
        except TypeError:  # httpx < 0.26
            client = httpx.Client(http2=True, proxies=proxy_url)
    except Exception as e:  # e.g. socks5:// without socksio installed
        print(f"[Arb/CLOBProxy] V2 SDK proxy NOT applied ({type(e).__name__}: {e}) -- SDK orders go direct", flush=True)
        return False
    helpers._http_client = client
    SDK_ROUTE = "proxy"
    print(f"[Arb/CLOBProxy] V2 SDK httpx client routed via {_redact(proxy_url)}", flush=True)
    return True


def _under_proxychains():
    return "proxychains" in os.environ.get("LD_PRELOAD", "").lower()


def install_proxy_patch():
    global _PATCHED
    if _PATCHED:
        return

    proxy_url = (
        os.environ.get("POLY_CLOB_PROXY_URL", "")
        or os.environ.get("PROXY_URL", "")
    )

    if not proxy_url:
        print(
            "[Arb/CLOBProxy] No proxy configured -- Polymarket CLOB calls go direct",
            flush=True,
        )
        _PATCHED = True
        return

    if _under_proxychains():
        print(
            "[Arb/CLOBProxy] proxychains detected -- skipping patch "
            "(TCP already proxied at OS level)",
            flush=True,
        )
        _PATCHED = True
        return

    try:
        from requests.adapters import HTTPAdapter
    except ImportError as e:
        print(f"[Arb/CLOBProxy] Cannot import requests.adapters: {e}", flush=True)
        _PATCHED = True
        return

    _orig_send = HTTPAdapter.send
    _proxies = {"http": proxy_url, "https": proxy_url}

    def _poly_proxied_send(self, request, **kwargs):
        try:
            url = getattr(request, "url", "") or ""
            if POLY_CLOB_HOST_RE.search(url):
                kwargs = dict(kwargs)
                kwargs["proxies"] = _proxies
        except Exception:
            pass
        return _orig_send(self, request, **kwargs)

    HTTPAdapter.send = _poly_proxied_send
    _PATCHED = True
    if os.environ.get("POLY_CLOB_PROXY_SDK", "true").strip().lower() == "true":
        patch_sdk_httpx(proxy_url)

    proxy_display = _redact(proxy_url)
    print(
        f"[Arb/CLOBProxy] Poly CLOB proxy ACTIVE "
        f"(HTTPAdapter patched, matching clob*.polymarket.com): {proxy_display}",
        flush=True,
    )


__all__ = ["install_proxy_patch", "patch_sdk_httpx", "POLY_CLOB_HOST_RE"]

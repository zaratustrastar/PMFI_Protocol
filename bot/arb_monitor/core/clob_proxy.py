"""HTTP proxy injection for Polymarket CLOB requests (V1/V2 compatible)."""

import os
import re

POLY_CLOB_HOST_RE = re.compile(r"clob[\w\-]*\.polymarket\.com", re.IGNORECASE)

_PATCHED = False


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

    proxy_display = re.sub(r"//[^@]+@", "//<redacted>@", proxy_url)
    print(
        f"[Arb/CLOBProxy] Poly CLOB proxy ACTIVE "
        f"(HTTPAdapter patched, matching clob*.polymarket.com): {proxy_display}",
        flush=True,
    )


__all__ = ["install_proxy_patch", "POLY_CLOB_HOST_RE"]

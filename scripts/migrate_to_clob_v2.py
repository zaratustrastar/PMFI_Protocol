#!/usr/bin/env python3
"""Apply Polymarket CLOB V2 migration patches. Run from repo root."""

import sys
from pathlib import Path


def log(msg):
    print(f"[migrate] {msg}", flush=True)


REPO_ROOT = Path.cwd()
CORE_DIR = REPO_ROOT / "bot" / "arb_monitor" / "core"

if not CORE_DIR.is_dir():
    REPO_ROOT = Path(__file__).resolve().parent.parent
    CORE_DIR = REPO_ROOT / "bot" / "arb_monitor" / "core"

EDITS = []

# ── arb_executor.py (6 edits) ─────────────────────────────────────────────

EDITS.append((
    "arb_executor.py",
    "executor: replace inline proxy monkey-patch with clob_proxy call",
    """\
# ── Route Polymarket CLOB through residential proxy (bypasses geoblock) ──────
# py_clob_client calls requests.request() directly (no Session, no proxies arg).
# Setting env vars (HTTPS_PROXY) is NOT reliable inside systemd-managed processes.
# Instead we monkey-patch py_clob_client.http_helpers.helpers.request directly so
# every call made by ClobClient.post_order / get / post automatically goes through
# the proxy — no env-var dependency whatsoever.
#
# Kalshi has dedicated direct-request calls in this file that use _KALSHI_SESSION
# (trust_env=False) so they are never routed through the proxy.
import re as _re
import requests as _requests_mod

_poly_clob_proxy = (
    os.environ.get("POLY_CLOB_PROXY_URL")
    or os.environ.get("PROXY_URL", "")
)
_proxy_display = (
    _re.sub(r"//[^@]+@", "//<redacted>@", _poly_clob_proxy)
    if _poly_clob_proxy else ""
)

if _poly_clob_proxy:
    # Monkey-patch py_clob_client's internal request function to inject proxies.
    # post() and get() in helpers.py resolve "request" via the module namespace,
    # so replacing helpers.request here redirects ALL py_clob_client HTTP calls.
    import py_clob_client.http_helpers.helpers as _pch_helpers
    from py_clob_client.exceptions import PolyApiException as _PolyApiException

    _pch_proxies = {"https": _poly_clob_proxy, "http": _poly_clob_proxy}
    _pch_orig_request = _pch_helpers.request  # keep reference for debugging

    def _pch_proxied_request(endpoint, method, headers=None, data=None):
        try:
            headers = _pch_helpers.overloadHeaders(method, headers)
            resp = _requests_mod.request(
                method=method,
                url=endpoint,
                headers=headers,
                json=data if data else None,
                proxies=_pch_proxies,
            )
            if resp.status_code != 200:
                raise _PolyApiException(resp)
            try:
                return resp.json()
            except _requests_mod.exceptions.JSONDecodeError:
                return resp.text
        except _requests_mod.exceptions.RequestException:
            raise _PolyApiException(error_msg="Request exception!")

    _pch_helpers.request = _pch_proxied_request
    print(f"⚡ [Arb/Executor] 🌐 Poly CLOB proxy ACTIVE (monkey-patched): {_proxy_display}", flush=True)
else:
    print("⚡ [Arb/Executor] ⚠️ No Poly CLOB proxy configured (PROXY_URL / POLY_CLOB_PROXY_URL)", flush=True)

# Kalshi HTTP calls in this file use a dedicated session with trust_env=False
# to ensure they always go direct and are never affected by any proxy settings.
_KALSHI_SESSION = _requests_mod.Session()
_KALSHI_SESSION.trust_env = False""",
    """\
# ── Route Polymarket CLOB through residential proxy (V1/V2 compatible) -------
# Uses HTTPAdapter.send patch instead of the old py_clob_client internal hook,
# which may not exist in V2. Host-matched on clob*.polymarket.com only.
import requests as _requests_mod
from .clob_proxy import install_proxy_patch as _install_clob_proxy

_install_clob_proxy()

# Kalshi HTTP calls use a dedicated session with trust_env=False (always direct).
_KALSHI_SESSION = _requests_mod.Session()
_KALSHI_SESSION.trust_env = False""",
))


EDITS.append((
    "arb_executor.py",
    "executor: replace py_clob_client imports in _place_poly_order",
    """\
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import (
            ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions
        )
    except ImportError:
        err = "py_clob_client not installed — cannot place Polymarket orders"
        log(f"❌ [POLY] {err}")
        return False, "", err""",
    """\
    try:
        from .clob_compat import (
            make_client, ApiCreds, OrderArgs, OrderType,
            PartialCreateOrderOptions, SDK_VERSION,
        )
    except ImportError:
        err = "clob_compat / py-clob-client(-v2) not installed"
        log(f"❌ [POLY] {err}")
        return False, "", err""",
))


EDITS.append((
    "arb_executor.py",
    "executor: replace ClobClient construction in _place_poly_order",
    """\
        if poly_api_key and poly_api_secret and poly_api_passphrase:
            creds = ApiCreds(
                api_key=poly_api_key,
                api_secret=poly_api_secret,
                api_passphrase=poly_api_passphrase,
            )
            client = ClobClient(
                clob_url, key=poly_private_key, chain_id=chain_id, creds=creds,
                signature_type=2, funder=poly_proxy_address,
            )
            log(f"🔑 [POLY] Using L2-authenticated ClobClient (sig_type=2/GNOSIS_SAFE, funder={poly_proxy_address})")
        else:
            client = ClobClient(
                clob_url, key=poly_private_key, chain_id=chain_id,
                signature_type=2, funder=poly_proxy_address,
            )
            log(f"🔑 [POLY] Using L1-only ClobClient (sig_type=2/GNOSIS_SAFE, funder={poly_proxy_address})")""",
    """\
        if poly_api_key and poly_api_secret and poly_api_passphrase:
            creds = ApiCreds(
                api_key=poly_api_key,
                api_secret=poly_api_secret,
                api_passphrase=poly_api_passphrase,
            )
            client = make_client(
                host=clob_url, key=poly_private_key, chain_id=chain_id, creds=creds,
                signature_type=2, funder=poly_proxy_address,
            )
            log(f"🔑 [POLY] Using L2-authenticated ClobClient (SDK={SDK_VERSION}, sig_type=2/GNOSIS_SAFE, funder={poly_proxy_address})")
        else:
            client = make_client(
                host=clob_url, key=poly_private_key, chain_id=chain_id,
                signature_type=2, funder=poly_proxy_address,
            )
            log(f"🔑 [POLY] Using L1-only ClobClient (SDK={SDK_VERSION}, sig_type=2/GNOSIS_SAFE, funder={poly_proxy_address})")""",
))


EDITS.append((
    "arb_executor.py",
    "executor: replace _make_poly_client_for_unwind body",
    """\
    def _make_poly_client_for_unwind():
        \"\"\"Build a fully-authenticated ClobClient for unwind operations.\"\"\"
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        clob_url            = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
        chain_id            = int(os.environ.get("POLY_CHAIN_ID", "137"))
        poly_api_key        = os.environ.get("POLY_API_KEY", "")
        poly_api_secret     = os.environ.get("POLY_API_SECRET", "")
        poly_api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
        poly_proxy_address  = os.environ.get("POLY_PROXY_ADDRESS", "") or None
        if poly_api_key and poly_api_secret and poly_api_passphrase:
            creds = ApiCreds(
                api_key=poly_api_key,
                api_secret=poly_api_secret,
                api_passphrase=poly_api_passphrase,
            )
            log(f"🔑 [POLY/UNWIND] Using L2-authenticated ClobClient (funder={poly_proxy_address})")
            return ClobClient(
                clob_url, key=poly_private_key, chain_id=chain_id,
                creds=creds, signature_type=2, funder=poly_proxy_address,
            )
        log("⚠️ [POLY/UNWIND] L2 API credentials missing — cancel/sell may fail auth")
        return ClobClient(
            clob_url, key=poly_private_key, chain_id=chain_id,
            signature_type=2, funder=poly_proxy_address,
        )""",
    """\
    def _make_poly_client_for_unwind():
        \"\"\"Build a fully-authenticated ClobClient for unwind operations.\"\"\"
        from .clob_compat import make_client, ApiCreds, SDK_VERSION
        clob_url            = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
        chain_id            = int(os.environ.get("POLY_CHAIN_ID", "137"))
        poly_api_key        = os.environ.get("POLY_API_KEY", "")
        poly_api_secret     = os.environ.get("POLY_API_SECRET", "")
        poly_api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
        poly_proxy_address  = os.environ.get("POLY_PROXY_ADDRESS", "") or None
        if poly_api_key and poly_api_secret and poly_api_passphrase:
            creds = ApiCreds(
                api_key=poly_api_key,
                api_secret=poly_api_secret,
                api_passphrase=poly_api_passphrase,
            )
            log(f"🔑 [POLY/UNWIND] Using L2 ClobClient (SDK={SDK_VERSION}, funder={poly_proxy_address})")
            return make_client(
                host=clob_url, key=poly_private_key, chain_id=chain_id,
                creds=creds, signature_type=2, funder=poly_proxy_address,
            )
        log(f"⚠️ [POLY/UNWIND] L2 credentials missing (SDK={SDK_VERSION})")
        return make_client(
            host=clob_url, key=poly_private_key, chain_id=chain_id,
            signature_type=2, funder=poly_proxy_address,
        )""",
))


EDITS.append((
    "arb_executor.py",
    "executor: replace OrderArgs/OrderType import in _unwind_poly_leg",
    """\
    log(f"📤 [POLY] Placing offset SELL to unwind filled position: token={token_id[:16]}... size_usdc={filled_size_usdc}")
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType""",
    """\
    log(f"📤 [POLY] Placing offset SELL to unwind filled position: token={token_id[:16]}... size_usdc={filled_size_usdc}")
    try:
        from .clob_compat import OrderArgs, OrderType""",
))


EDITS.append((
    "arb_executor.py",
    "executor: replace PartialCreateOrderOptions import in _unwind_poly_leg",
    "        from py_clob_client.clob_types import PartialCreateOrderOptions",
    "        from .clob_compat import PartialCreateOrderOptions",
))


# ── arb_funder.py (2 edits) ───────────────────────────────────────────────

EDITS.append((
    "arb_funder.py",
    "funder: replace ClobClient construction in _get_platform_balance",
    """\
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
            clob_url           = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
            private_key        = os.environ.get("POLY_PRIVATE_KEY", "")
            poly_proxy_address = os.environ.get("POLY_PROXY_ADDRESS", "") or None
            creds = ApiCreds(
                api_key=poly_api_key.strip(),
                api_secret=poly_api_secret.strip(),
                api_passphrase=poly_api_passphrase.strip(),
            )
            client = ClobClient(
                clob_url,
                key=private_key,
                chain_id=137,
                creds=creds,
                signature_type=2,
                funder=poly_proxy_address,
            )""",
    """\
        try:
            from .clob_compat import (
                make_client, ApiCreds, BalanceAllowanceParams, AssetType, SDK_VERSION,
            )
            clob_url           = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
            private_key        = os.environ.get("POLY_PRIVATE_KEY", "")
            poly_proxy_address = os.environ.get("POLY_PROXY_ADDRESS", "") or None
            creds = ApiCreds(
                api_key=poly_api_key.strip(),
                api_secret=poly_api_secret.strip(),
                api_passphrase=poly_api_passphrase.strip(),
            )
            client = make_client(
                host=clob_url,
                key=private_key,
                chain_id=137,
                creds=creds,
                signature_type=2,
                funder=poly_proxy_address,
            )""",
))


EDITS.append((
    "arb_funder.py",
    "funder: replace EOA ClobClient in _get_platform_balance",
    "                client_eoa = ClobClient(clob_url, key=private_key, chain_id=137, creds=creds, signature_type=0)",
    "                client_eoa = make_client(host=clob_url, key=private_key, chain_id=137, creds=creds, signature_type=0)",
))


# ── arb_nav.py (2 edits) ──────────────────────────────────────────────────

EDITS.append((
    "arb_nav.py",
    "nav: replace ClobClient construction in _get_servicer_balances",
    """\
    if poly_api_key:
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
            clob_url           = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
            private_key        = os.environ.get("POLY_PRIVATE_KEY", "")
            poly_proxy_address = os.environ.get("POLY_PROXY_ADDRESS", "") or None
            creds = ApiCreds(
                api_key=poly_api_key.strip(),
                api_secret=poly_api_secret.strip(),
                api_passphrase=poly_api_passphrase.strip(),
            )
            client = ClobClient(
                clob_url,
                key=private_key,
                chain_id=137,
                creds=creds,
                signature_type=2,
                funder=poly_proxy_address,
            )""",
    """\
    if poly_api_key:
        try:
            from .clob_compat import (
                make_client, ApiCreds, BalanceAllowanceParams, AssetType, SDK_VERSION,
            )
            clob_url           = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
            private_key        = os.environ.get("POLY_PRIVATE_KEY", "")
            poly_proxy_address = os.environ.get("POLY_PROXY_ADDRESS", "") or None
            creds = ApiCreds(
                api_key=poly_api_key.strip(),
                api_secret=poly_api_secret.strip(),
                api_passphrase=poly_api_passphrase.strip(),
            )
            client = make_client(
                host=clob_url,
                key=private_key,
                chain_id=137,
                creds=creds,
                signature_type=2,
                funder=poly_proxy_address,
            )""",
))


EDITS.append((
    "arb_nav.py",
    "nav: replace EOA ClobClient in _get_servicer_balances",
    "                client_eoa = ClobClient(clob_url, key=private_key, chain_id=137, creds=creds, signature_type=0)",
    "                client_eoa = make_client(host=clob_url, key=private_key, chain_id=137, creds=creds, signature_type=0)",
))


# ── Apply ─────────────────────────────────────────────────────────────────

def apply_edits():
    if not CORE_DIR.is_dir():
        log(f"ERROR: Cannot find {CORE_DIR} -- run from repo root")
        return 1

    by_file = {}
    for entry in EDITS:
        by_file.setdefault(entry[0], []).append(entry)

    applied = skipped = missing = 0

    for fname, file_edits in by_file.items():
        path = CORE_DIR / fname
        if not path.is_file():
            log(f"ERROR: Not found: {path}")
            missing += len(file_edits)
            continue

        log(f"")
        log(f"-- {fname} --")
        text = path.read_text()
        modified = False

        for entry in file_edits:
            desc    = entry[1]
            old_str = entry[2]
            new_str = entry[3]

            count_old = text.count(old_str)
            count_new = text.count(new_str)

            if count_old == 0 and count_new >= 1:
                log(f"  SKIP (already applied): {desc}")
                skipped += 1
                continue
            if count_old == 0:
                log(f"  NOT FOUND: {desc}")
                missing += 1
                continue
            if count_old > 1:
                log(f"  AMBIGUOUS ({count_old} matches): {desc}")
                missing += 1
                continue

            text = text.replace(old_str, new_str, 1)
            modified = True
            log(f"  OK: {desc}")
            applied += 1

        if modified:
            path.write_text(text)
            log(f"  Saved: {path}")

    log("")
    log("=" * 60)
    log(f"Applied:  {applied}")
    log(f"Skipped:  {skipped}  (already applied)")
    log(f"Missing:  {missing}  (need manual fix)")
    log("=" * 60)
    return 1 if missing > 0 else 0


if __name__ == "__main__":
    sys.exit(apply_edits())

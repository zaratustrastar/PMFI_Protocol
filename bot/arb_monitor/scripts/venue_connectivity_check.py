"""READ-ONLY connectivity, proxy-routing and account check for the Robinhood relaunch.

    python3 -m bot.arb_monitor.scripts.venue_connectivity_check [--samples 3] [--skip-auth]

Run it on the VPS from the engine's checkout with the engine's environment loaded (the same .env
the service uses), WITHOUT restarting or touching the running service. It only sends GET requests
(plus one eth_chainId / eth_call JSON-RPC read): no order, cancel, withdrawal, approval or
transaction. Credentials and proxy passwords are never printed: proxy URLs are reduced to
scheme://host:port and response bodies are summarised, never echoed.

What it reports (JSON on stdout):
  proxy      every proxy variable that is set (redacted), proxychains, DNS resolution and TCP
             connect time to each proxy endpoint.
  routing    for each engine call path, the route the engine's code actually takes with this
             environment (direct / which proxy), derived from http_client.py, core/clob_proxy.py
             and the call sites. Kalshi always goes direct on the trading path (bypass_proxy /
             trust_env=False); setting PROXY_URL does not change that.
  checks     each read-only request over that route: HTTP status, median latency over --samples,
             ok/failed and a short reason. Polymarket geoblock is checked both direct and over the
             CLOB proxy, which tells whether order placement is allowed from each egress.
  verdict    ready | blocked, with the failing checks.
"""

import argparse
import json
import os
import socket
import statistics
import sys
import time
from urllib.parse import urlparse

import requests


def redact(url: str) -> str:
    if not url:
        return ""
    u = urlparse(url)
    host = u.hostname or ""
    return f"{u.scheme}://{host}{':' + str(u.port) if u.port else ''}"


def scrub(text: str) -> str:
    """Remove any user:password@ from text (exception messages can quote a proxy URL)."""
    import re
    return re.sub(r"//[^/@\s]+@", "//<redacted>@", text)


def proxy_env() -> dict:
    names = ("PROXY_URL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "POLY_CLOB_PROXY_URL", "OPINION_PROXY_URL")
    out = {n: redact(os.environ.get(n, "") or os.environ.get(n.lower(), "")) for n in names}
    out["proxychains"] = "proxychains" in os.environ.get("LD_PRELOAD", "").lower()
    return {k: v for k, v in out.items() if v}


def probe_proxy(url: str) -> dict:
    u = urlparse(url)
    port = u.port or (1080 if (u.scheme or "").startswith("socks") else 8080 if u.scheme == "http" else 443)
    res = {"endpoint": redact(url)}
    t = time.monotonic()
    try:
        addrs = socket.getaddrinfo(u.hostname, port, proto=socket.IPPROTO_TCP)
        res["dns_ms"] = round((time.monotonic() - t) * 1000, 1)
        res["dns_addresses"] = len({a[4][0] for a in addrs})
    except OSError as e:
        res["dns_error"] = type(e).__name__
        return res
    t = time.monotonic()
    try:
        with socket.create_connection((u.hostname, port), timeout=10):
            res["tcp_connect_ms"] = round((time.monotonic() - t) * 1000, 1)
    except OSError as e:
        res["tcp_error"] = type(e).__name__
    return res


def engine_routing(env: dict) -> dict:
    """The route each engine call path takes, from the code (not from a guess)."""
    general = env.get("HTTP_PROXY") or env.get("HTTPS_PROXY") or env.get("PROXY_URL") or ""
    if env.get("proxychains"):
        general_label = "proxychains (all TCP at OS level)"
    else:
        general_label = f"proxy {general}" if general else "direct"
    clob_proxy = env.get("POLY_CLOB_PROXY_URL") or env.get("PROXY_URL") or ""
    sdk_on = os.environ.get("POLY_CLOB_PROXY_SDK", "true").strip().lower() == "true"
    std_env = env.get("HTTPS_PROXY") or env.get("ALL_PROXY") or ""
    return {
        "gamma (http_client session)": general_label,
        "clob books/prices (requests, clob_proxy patch)": f"proxy {clob_proxy}" if clob_proxy else "direct",
        "clob orders/balances (py-clob-client-v2 httpx)":
            (f"proxy {clob_proxy}" if clob_proxy and sdk_on else (f"proxy {std_env} (HTTPS_PROXY env)" if std_env else "direct")),
        "kalshi trading + market data (bypass_proxy / trust_env=False)": "direct",
        "kalshi balance reads (requests default, trust_env)": f"proxy {std_env} (HTTPS_PROXY env)" if std_env else "direct",
        "prediction.com (requests default, trust_env)": f"proxy {std_env} (HTTPS_PROXY env)" if std_env else "direct",
    }


def timed(fn, samples: int) -> dict:
    lat, last, err = [], None, None
    for _ in range(samples):
        t = time.monotonic()
        try:
            last = fn()
            lat.append((time.monotonic() - t) * 1000)
        except Exception as e:  # reported, never raised
            err = scrub(f"{type(e).__name__}: {str(e)}")[:200]
    out = {"samples_ok": len(lat), "samples": samples}
    if lat:
        out["latency_ms_median"] = round(statistics.median(lat), 1)
        out["latency_ms_max"] = round(max(lat), 1)
    if err:
        out["error"] = err
    return out, last


def _session(proxy: str = "") -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def http_check(name: str, route: str, sess, url: str, samples: int, headers=None, params=None, judge=None) -> dict:
    def call():
        r = sess.get(url, headers=headers or {}, params=params, timeout=20)
        return r
    stats, r = timed(call, samples)
    res = {"name": name, "route": route, **stats}
    if r is not None:
        res["status"] = r.status_code
        try:
            body = r.json()
        except ValueError:
            body = None
            res["body"] = "html" if "<html" in (r.text or "")[:300].lower() else "non-json"
        verdict = judge(r, body) if judge else (r.status_code == 200, "")
        res["ok"], why = verdict
        if why:
            res["detail"] = why
    else:
        res["ok"] = False
    return res


def geoblock_judge(r, body):
    if r.status_code != 200 or not isinstance(body, dict):
        return False, "no geoblock answer"
    blocked = bool(body.get("blocked"))
    loc = "/".join(str(body.get(k) or "") for k in ("country", "region"))
    return (not blocked), f"blocked={blocked} location={loc}"


def run(samples: int, skip_auth: bool) -> dict:
    from ..config import KALSHI_BASE_URL, POLY_CLOB_URL, POLY_GAMMA_URL
    env = proxy_env()
    report = {"proxy": {"env": env, "endpoints": []}, "routing": engine_routing(env), "checks": []}
    seen = set()
    for k in ("PROXY_URL", "HTTPS_PROXY", "POLY_CLOB_PROXY_URL"):
        raw = os.environ.get(k, "")
        if raw and redact(raw) not in seen:
            seen.add(redact(raw))
            report["proxy"]["endpoints"].append({"var": k, **probe_proxy(raw)})

    clob_proxy = os.environ.get("POLY_CLOB_PROXY_URL", "") or os.environ.get("PROXY_URL", "")
    direct = _session()
    via_clob = _session(clob_proxy) if clob_proxy else None
    C = report["checks"]
    geo = "https://polymarket.com/api/geoblock"
    C.append(http_check("polymarket geoblock", "direct", direct, geo, 1, judge=geoblock_judge))
    if via_clob:
        C.append(http_check("polymarket geoblock", f"proxy {redact(clob_proxy)}", via_clob, geo, 1, judge=geoblock_judge))

    from .. import http_client
    gamma_route = report["routing"]["gamma (http_client session)"]
    C.append(http_check("gamma markets", gamma_route, http_client._get_session(), f"{POLY_GAMMA_URL}/markets",
                        samples, params={"limit": 1, "active": "true"}))
    clob_sess = via_clob or direct
    C.append(http_check("clob server time", "proxy " + redact(clob_proxy) if clob_proxy else "direct", clob_sess,
                        f"{POLY_CLOB_URL}/time", samples))

    if not skip_auth:
        C.append(guarded("polymarket authenticated balance (V2 SDK)", poly_sdk_balance, samples))
        C.append(guarded("kalshi authenticated balance", kalshi_signed_get, KALSHI_BASE_URL, samples))
    C.append(http_check("kalshi exchange status", "direct", direct, f"{KALSHI_BASE_URL}/exchange/status", samples,
                        judge=lambda r, b: (r.status_code == 200 and isinstance(b, dict) and bool(b.get("trading_active", True)),
                                            f"trading_active={b.get('trading_active') if isinstance(b, dict) else None}")))
    C.append(guarded("prediction.com /v2/arb", prediction_com, samples))
    C.append(guarded("robinhood rpc (chain id, USDG decimals)", robinhood_rpc))

    failed = [f"{c['name']} ({c['route']})" for c in C if not c.get("ok") and not c.get("optional")]
    report["verdict"] = {"result": "ready" if not failed else "blocked", "failed": failed}
    return report


def guarded(name: str, fn, *args) -> dict:
    try:
        return fn(*args)
    except Exception as e:
        return {"name": name, "route": "-", "ok": False, "error": scrub(f"{type(e).__name__}: {e}")[:200]}


def poly_sdk_balance(samples: int) -> dict:
    """L2-authenticated GET /balance-allowance through py-clob-client-v2, with the engine's proxy patch."""
    name = "polymarket authenticated balance (V2 SDK)"
    if not os.environ.get("POLY_API_KEY"):
        return {"name": name, "route": "-", "ok": False, "error": "POLY_API_KEY not set"}
    from ..core import clob_proxy
    clob_proxy.install_proxy_patch()
    from ..core.clob_compat import SDK_VERSION, ApiCreds, AssetType, BalanceAllowanceParams, make_client
    from ..config import POLY_CLOB_URL
    client = make_client(
        POLY_CLOB_URL, key=os.environ.get("POLY_PRIVATE_KEY", ""), chain_id=137,
        creds=ApiCreds(api_key=os.environ["POLY_API_KEY"].strip(),
                       api_secret=os.environ.get("POLY_API_SECRET", "").strip(),
                       api_passphrase=os.environ.get("POLY_API_PASSPHRASE", "").strip()),
        signature_type=2, funder=os.environ.get("POLY_PROXY_ADDRESS") or None,
    )
    stats, body = timed(lambda: client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)), samples)
    route = f"sdk={SDK_VERSION} transport={'proxy' if clob_proxy.SDK_ROUTE == 'proxy' else 'direct'}"
    res = {"name": name, "route": route, **stats, "ok": isinstance(body, dict) and "balance" in body}
    if res["ok"]:
        res["detail"] = f"pUSD balance={int(body['balance']) / 1e6:.2f}"
    return res


def kalshi_signed_get(base: str, samples: int) -> dict:
    name = "kalshi authenticated balance"
    from ..core.kalshi_auth import get_kalshi_headers, kalshi_auth_available
    if not kalshi_auth_available():
        return {"name": name, "route": "direct", "ok": False, "error": "Kalshi credentials not configured"}
    url = f"{base}/portfolio/balance"
    sess = _session()  # the trading path's route: direct

    def call():
        return sess.get(url, headers=get_kalshi_headers("GET", url), timeout=20)
    stats, r = timed(call, samples)
    res = {"name": name, "route": "direct", **stats, "ok": r is not None and r.status_code == 200}
    if r is not None:
        res["status"] = r.status_code
        if r.status_code == 200:
            b = r.json()
            res["detail"] = f"balance=${(b.get('balance') or 0) / 100:.2f}"
    return res


def prediction_com(samples: int) -> dict:
    name = "prediction.com /v2/arb"
    from ..adapters import prediction_com as P
    if not P.api_key():
        return {"name": name, "route": "-", "ok": False, "error": "PREDICTION_COM_API_KEY not set",
                "optional": os.environ.get("ARB_OPPORTUNITY_FEED", "oddpool") == "oddpool"}
    sess = requests.Session()  # trust_env like the adapter
    stats, r = timed(lambda: sess.get(f"{P.base_url()}/arb", params={"limit": 5, "platforms": "polymarket,kalshi"},
                                      headers={"X-API-Key": P.api_key()}, timeout=20), 1)
    res = {"name": name, "route": report_route_env(), **stats, "ok": r is not None and r.status_code == 200}
    if r is not None:
        res["status"] = r.status_code
        res["rate_limits"] = {k: v for k, v in r.headers.items() if k.lower().startswith("x-ratelimit")}
        if r.status_code == 200:
            b = r.json()
            as_of = P._ts(b.get("as_of"))
            res["detail"] = (f"count={b.get('count')} delay_seconds={b.get('delay_seconds')} "
                             f"as_of_age_s={round(time.time() - as_of, 1) if as_of else None}")
    return res


def report_route_env() -> str:
    p = os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or ""
    return f"proxy {redact(p)} (HTTPS_PROXY env)" if p else "direct"


def robinhood_rpc() -> dict:
    from ..config import robinhood_rpc_url, usdg_address
    url = robinhood_rpc_url()
    name = "robinhood rpc (chain id, USDG decimals)"
    if not url:
        return {"name": name, "route": "-", "ok": False, "error": "ROBINHOOD_RPC_URL / ARB_VAULT_RPC_URL not set"}
    s = _session()
    try:
        cid = int(s.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=15).json()["result"], 16)
        dec = int(s.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "eth_call",
                                    "params": [{"to": usdg_address(), "data": "0x313ce567"}, "latest"]}, timeout=15).json()["result"], 16)
    except Exception as e:
        return {"name": name, "route": "direct", "ok": False, "error": type(e).__name__}
    return {"name": name, "route": "direct", "ok": cid == 4663 and dec == 6, "detail": f"chainId={cid} USDG decimals={dec}"}


def load_env_file(path: str) -> None:
    """KEY=VALUE lines (optional `export`, quotes); existing variables win, like python-dotenv."""
    with open(os.path.expanduser(path)) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.removeprefix("export ").split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            os.environ.setdefault(k, v)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--samples", type=int, default=3)
    p.add_argument("--skip-auth", action="store_true", help="skip the two authenticated reads")
    p.add_argument("--env-file", help="load this env file first (values are never printed)")
    a = p.parse_args(argv)
    if a.env_file:
        load_env_file(a.env_file)
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):  # engine modules log to stdout; keep the JSON clean
        report = run(max(1, a.samples), a.skip_auth)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["verdict"]["result"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())

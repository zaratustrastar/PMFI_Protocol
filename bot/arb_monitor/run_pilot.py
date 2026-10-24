"""Standalone runner for the Robinhood vault pilot: the PMFI engine's own execution loop (reporter tick,
tend, opportunity feed, live-book gate, funding, orders, positions) without the Telegram bot, Flask
server or any other PMFI service.

    cd bot && python3 -m arb_monitor.run_pilot --env-file /path/pilot.env --check   # config + startup checks, then exit
    cd bot && python3 -m arb_monitor.run_pilot --env-file /path/pilot.env --once    # one execution cycle, then exit
    cd bot && python3 -m arb_monitor.run_pilot --env-file /path/pilot.env           # the loop (ODDPOOL_POLL_INTERVAL)

Isolation (refuses to start otherwise):
  - --env-file is required and is authoritative: its values override the process environment, and every
    engine variable inherited from the shell or a parent service (ARB_*, POLY_*, KALSHI_*, DATABASE_URL,
    proxies, keys, RPC URLs, ...) that the file does not set is removed before anything is imported.
    The pilot can therefore never pick up the live engine's database, credentials or ledgers silently.
  - ARB_FUNDING_CHAIN=robinhood and ARB_VAULT_CHAIN_ID=4663: this runner never drives the Base vault.
  - ARB_PILOT_DATA_DIR and every ledger / journal path resolve (realpath) outside the live engine's
    directories (/root/PolyNotifyBot, ~/PolyNotifyBot, /opt/polymarket-bot) and differ from every ledger
    path in the production env file (--production-env, default /root/PolyNotifyBot/.env).
  - DATABASE_URL is set and is not the production database (scheme, host incl. localhost aliases, port
    with the 5432 default and database name are compared), nor the DATABASE_URL inherited by the process.
  - Venue credentials, the servicer and the report signer differ from the production env's.
Only variable names and credential-free targets are printed. ARB_EXECUTION_MODE defaults to shadow in
robinhood mode; real orders need ARB_EXECUTION_MODE=live.
"""

import argparse
import os
import sys
from urllib.parse import parse_qs, urlparse

LIVE_DATA_DIR = os.path.realpath(os.path.expanduser("~/PolyNotifyBot/data"))
LIVE_ROOTS = tuple(dict.fromkeys(os.path.realpath(os.path.expanduser(p)) for p in (
    "~/PolyNotifyBot", "/root/PolyNotifyBot", "/opt/polymarket-bot")))
PRODUCTION_ENV = "/root/PolyNotifyBot/.env"

LEDGERS = {
    "ARB_WITHDRAWAL_LEDGER_PATH": "arb_withdrawals_ledger.json",
    "ARB_BRIDGE_LEDGER_PATH": "arb_bridge_ledger.json",
    "ARB_INTENT_LEDGER_PATH": "arb_funding_intents.json",
    "ARB_CASHFLOW_JOURNAL_PATH": "arb_cashflow_journal.json",
    "ARB_VENUE_DEPOSIT_LEDGER_PATH": "arb_venue_deposit_ledger.json",
    "ARB_CTF_JOURNAL": "arb_ctf_redemptions.json",
    "ARB_SHADOW_JOURNAL_PATH": "arb_shadow_trades.jsonl",
    "ARB_FEED_COMPARE_LOG": "arb_feed_compare.jsonl",
    "AI_MATCH_CACHE_PATH": "arb_ai_match_cache.json",
}

# Must not be shared with the live engine (compared against the production env file).
DISTINCT_SECRETS = ("POLY_PRIVATE_KEY", "POLY_API_KEY", "POLY_PROXY_ADDRESS", "KALSHI_API_KEY_ID",
                    "ARB_NAV_SIGNER_PRIVATE_KEY", "ARB_SERVICER_WALLET")

_ENGINE_PREFIXES = ("ARB_", "POLY_", "KALSHI_", "ODDPOOL_", "PREDICTION_COM_", "OPINION_", "ROBINHOOD_",
                    "MILORD_", "CLOB_")
_ENGINE_NAMES = {"DATABASE_URL", "PROXY_URL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "AI_MATCH_CACHE_PATH", "PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"}
_ENGINE_MARKERS = ("PRIVATE_KEY", "API_KEY", "SECRET", "PASSPHRASE", "RPC_URL", "DATABASE")


def is_engine_var(name: str) -> bool:
    u = name.upper()
    return u.startswith(_ENGINE_PREFIXES) or u in _ENGINE_NAMES or any(m in u for m in _ENGINE_MARKERS)


def parse_env_file(path: str) -> dict:
    """KEY=VALUE lines (optional `export`, quotes, comments). Values are never printed."""
    out = {}
    with open(os.path.expanduser(path)) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.removeprefix("export ").split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            elif " #" in v:
                v = v.split(" #", 1)[0].rstrip()
            out[k] = v
    return out


def isolate_env(file_vars: dict, env=os.environ) -> list[str]:
    """Apply file_vars over env (file wins) and drop inherited engine variables the file does not set.
    Returns the dropped names."""
    dropped = sorted(k for k in list(env) if is_engine_var(k) and k not in file_vars)
    for k in dropped:
        del env[k]
    env.update(file_vars)
    return dropped


def under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def same_path(a: str, b: str) -> bool:
    ra, rb = (os.path.realpath(os.path.expanduser(x)) for x in (a, b))
    if ra == rb:
        return True
    try:
        return os.path.samefile(ra, rb)
    except OSError:
        return False


def db_identity(url: str) -> tuple:
    """(scheme, host, port, dbname) with credentials dropped and local aliases folded together."""
    u = urlparse(url.strip())
    q = parse_qs(u.query)
    scheme = {"postgres": "postgresql"}.get(u.scheme.split("+")[0].lower(), u.scheme.split("+")[0].lower())
    try:
        port = u.port
    except ValueError:
        port = None
    host = (q.get("host", [None])[0] or u.hostname or "localhost").lower()
    if host in ("127.0.0.1", "::1", "localhost") or host.startswith("/"):
        host = "localhost"
    dbname = u.path.lstrip("/") or q.get("dbname", [""])[0] or (u.username or "")
    return scheme, host, port or (5432 if scheme == "postgresql" else None), dbname


def prepare_env(env=os.environ, production: dict | None = None, inherited: dict | None = None) -> list[str]:
    """Validate and fill the pilot environment. Returns problems (empty = ok). Must run before any
    arb_monitor import, because some ledger paths are read at import time."""
    production, inherited = production or {}, inherited or {}
    problems = []
    if env.get("ARB_FUNDING_CHAIN", "").strip().lower() != "robinhood":
        problems.append("ARB_FUNDING_CHAIN must be robinhood")
    if env.get("ARB_VAULT_CHAIN_ID", "").strip() != "4663":
        problems.append("ARB_VAULT_CHAIN_ID must be 4663")
    prod_paths = [production[v] for v in LEDGERS if production.get(v)]
    data = env.get("ARB_PILOT_DATA_DIR", "").strip()
    if not data:
        problems.append("ARB_PILOT_DATA_DIR is required (pilot ledgers live there)")
    else:
        real = os.path.realpath(os.path.expanduser(data))
        if any(under(real, r) or under(r, real) for r in LIVE_ROOTS):
            problems.append(f"ARB_PILOT_DATA_DIR must be outside the live engine's directories {list(LIVE_ROOTS)}")
        elif any(under(os.path.realpath(os.path.expanduser(p)), real) for p in prod_paths):
            problems.append("ARB_PILOT_DATA_DIR contains a production ledger path")
        else:
            os.makedirs(real, exist_ok=True)
            for var, name in LEDGERS.items():
                path = env.get(var, "") or os.path.join(real, name)
                rp = os.path.realpath(os.path.expanduser(path))
                if any(under(rp, r) for r in LIVE_ROOTS):
                    problems.append(f"{var} points into the live engine's directories")
                elif any(same_path(rp, p) for p in prod_paths):
                    problems.append(f"{var} is the same file as a production ledger")
                env[var] = path
    db = env.get("DATABASE_URL", "")
    if not db:
        problems.append("DATABASE_URL is required (pilot positions database)")
    else:
        for label, other in (("production env file", production.get("DATABASE_URL")),
                             ("inherited process environment", inherited.get("DATABASE_URL"))):
            if other and db_identity(other) == db_identity(db):
                problems.append(f"DATABASE_URL is the same database as the {label}'s")
    for var in DISTINCT_SECRETS:
        a, b = env.get(var, "").strip().lower(), production.get(var, "").strip().lower()
        if a and a == b:
            problems.append(f"{var} is shared with the production engine (the pilot needs its own)")
    pem, prod_pem = env.get("KALSHI_PRIVATE_KEY_PATH", ""), production.get("KALSHI_PRIVATE_KEY_PATH", "")
    if pem and prod_pem and same_path(pem, prod_pem):
        problems.append("KALSHI_PRIVATE_KEY_PATH is the production engine's key file")
    if env.get("ARB_EXECUTION_MODE", "shadow").strip().lower() not in ("shadow", "live"):
        problems.append("ARB_EXECUTION_MODE must be shadow or live")
    return problems


def db_target(url: str) -> str:
    scheme, host, port, dbname = db_identity(url)
    return f"{scheme}://{host}{':' + str(port) if port else ''}/{dbname}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="PMFI engine pilot runner (Robinhood vault)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="validate config and run startup checks, then exit")
    g.add_argument("--once", action="store_true", help="run one execution cycle, then exit")
    p.add_argument("--env-file", required=True, help="the pilot env file; authoritative (values are never printed)")
    p.add_argument("--production-env", default=None,
                   help=f"the live engine's env file to compare against (default {PRODUCTION_ENV} if readable)")
    a = p.parse_args(argv)

    inherited = dict(os.environ)
    production = {}
    prod_path = a.production_env or PRODUCTION_ENV
    try:
        production = parse_env_file(prod_path)
        print(f"pilot: comparing against production env {prod_path} ({len(production)} variables)")
    except OSError as e:
        if a.production_env:
            print(f"REFUSED: cannot read --production-env {prod_path}: {type(e).__name__}")
            return 2
        print(f"pilot: production env {prod_path} not readable; comparing against inherited env and live paths only")
    dropped = isolate_env(parse_env_file(a.env_file))
    if dropped:
        print(f"pilot: dropped {len(dropped)} inherited engine variables not in the env file: {', '.join(dropped)}")

    problems = prepare_env(production=production, inherited=inherited)
    if problems:
        for x in problems:
            print(f"REFUSED: {x}")
        return 2
    mode = os.environ.get("ARB_EXECUTION_MODE", "shadow").strip().lower()
    print(f"pilot: mode={mode} feed={os.environ.get('ARB_OPPORTUNITY_FEED', 'oddpool')} "
          f"data={os.environ['ARB_PILOT_DATA_DIR']} db={db_target(os.environ['DATABASE_URL'])} "
          f"vault={os.environ.get('ARB_VAULT_V2_ADDRESS', '<unset>')}")

    from .core import arb_execution_loop as EL
    if a.check:
        errs = EL.run_startup_checks()
        if errs and mode == "shadow":
            print(f"startup checks: shadow OK; these would block LIVE trading: {errs}")
            return 0
        print("startup checks: OK" if not errs else f"startup checks FAILED: {errs}")
        return 0 if not errs else 1
    if a.once:
        if mode != "shadow":
            EL.run_startup_checks()
        EL._execution_cycle()
        return 0
    EL._loop_running = True
    EL._execution_loop_worker()
    return 0


if __name__ == "__main__":
    sys.exit(main())

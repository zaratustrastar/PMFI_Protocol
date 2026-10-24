"""Standalone runner for the Robinhood vault pilot: the PMFI engine's own execution loop (reporter tick,
tend, opportunity feed, live-book gate, funding, orders, positions) without the Telegram bot, Flask
server or any other PMFI service.

    cd bot && python3 -m arb_monitor.run_pilot --check     # config + startup checks, then exit
    cd bot && python3 -m arb_monitor.run_pilot --once      # one execution cycle, then exit
    cd bot && python3 -m arb_monitor.run_pilot             # the loop (ODDPOOL_POLL_INTERVAL)

Guards (refuses to start otherwise):
  - ARB_FUNDING_CHAIN=robinhood and ARB_VAULT_CHAIN_ID=4663: this runner never drives the Base vault;
  - ARB_PILOT_DATA_DIR set and not the live engine's ~/PolyNotifyBot/data: every ledger and journal
    (withdrawals, bridges, intents, cash flow, venue deposits, shadow trades, feed comparison) is
    placed there unless explicitly set, so the pilot can never write the live engine's ledgers;
  - DATABASE_URL set (positions); its target is printed without credentials so the operator can see
    it is the pilot database, not the live one.
ARB_EXECUTION_MODE defaults to shadow in robinhood mode; real orders need ARB_EXECUTION_MODE=live.
"""

import argparse
import os
import sys
from urllib.parse import urlparse

LIVE_DATA_DIR = os.path.realpath(os.path.expanduser("~/PolyNotifyBot/data"))

LEDGERS = {
    "ARB_WITHDRAWAL_LEDGER_PATH": "arb_withdrawals_ledger.json",
    "ARB_BRIDGE_LEDGER_PATH": "arb_bridge_ledger.json",
    "ARB_INTENT_LEDGER_PATH": "arb_funding_intents.json",
    "ARB_CASHFLOW_JOURNAL_PATH": "arb_cashflow_journal.json",
    "ARB_VENUE_DEPOSIT_LEDGER_PATH": "arb_venue_deposit_ledger.json",
    "ARB_SHADOW_JOURNAL_PATH": "arb_shadow_trades.jsonl",
    "ARB_FEED_COMPARE_LOG": "arb_feed_compare.jsonl",
}


def prepare_env(env=os.environ) -> list[str]:
    """Validate and fill the pilot environment. Returns problems (empty = ok). Must run before any
    arb_monitor import, because some ledger paths are read at import time."""
    problems = []
    if env.get("ARB_FUNDING_CHAIN", "").strip().lower() != "robinhood":
        problems.append("ARB_FUNDING_CHAIN must be robinhood")
    if env.get("ARB_VAULT_CHAIN_ID", "").strip() != "4663":
        problems.append("ARB_VAULT_CHAIN_ID must be 4663")
    data = env.get("ARB_PILOT_DATA_DIR", "").strip()
    if not data:
        problems.append("ARB_PILOT_DATA_DIR is required (pilot ledgers live there)")
    else:
        real = os.path.realpath(os.path.expanduser(data))
        if real == LIVE_DATA_DIR or real.startswith(LIVE_DATA_DIR + os.sep):
            problems.append(f"ARB_PILOT_DATA_DIR must not be the live engine's {LIVE_DATA_DIR}")
        else:
            os.makedirs(real, exist_ok=True)
            for var, name in LEDGERS.items():
                path = env.get(var, "") or os.path.join(real, name)
                rp = os.path.realpath(os.path.expanduser(path))
                if rp.startswith(LIVE_DATA_DIR + os.sep):
                    problems.append(f"{var} points into the live engine's data directory")
                env[var] = path
    if not env.get("DATABASE_URL"):
        problems.append("DATABASE_URL is required (pilot positions database)")
    if env.get("ARB_EXECUTION_MODE", "shadow").strip().lower() not in ("shadow", "live"):
        problems.append("ARB_EXECUTION_MODE must be shadow or live")
    return problems


def db_target(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.hostname or ''}{':' + str(u.port) if u.port else ''}{u.path}"


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
    p = argparse.ArgumentParser(description="PMFI engine pilot runner (Robinhood vault)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="validate config and run startup checks, then exit")
    g.add_argument("--once", action="store_true", help="run one execution cycle, then exit")
    p.add_argument("--env-file", help="load this env file first (values are never printed)")
    a = p.parse_args(argv)
    if a.env_file:
        load_env_file(a.env_file)

    problems = prepare_env()
    if problems:
        for x in problems:
            print(f"REFUSED: {x}")
        return 2
    mode = os.environ.get("ARB_EXECUTION_MODE", "shadow")
    print(f"pilot: mode={mode} feed={os.environ.get('ARB_OPPORTUNITY_FEED', 'oddpool')} "
          f"data={os.environ['ARB_PILOT_DATA_DIR']} db={db_target(os.environ['DATABASE_URL'])} "
          f"vault={os.environ.get('ARB_VAULT_V2_ADDRESS', '<unset>')}")

    from .core import arb_execution_loop as EL
    if a.check:
        errs = EL.run_startup_checks()
        print("startup checks: OK" if not errs else f"startup checks FAILED: {errs}")
        return 0 if not errs else 1
    if a.once:
        EL.run_startup_checks()
        EL._execution_cycle()
        return 0
    EL._loop_running = True
    EL._execution_loop_worker()
    return 0


if __name__ == "__main__":
    sys.exit(main())

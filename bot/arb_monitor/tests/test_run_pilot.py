"""Pilot runner guards: never the Base vault, never the live engine's ledgers. Offline."""
import os
import tempfile
import unittest

import _fakes  # noqa: F401
from arb_monitor import run_pilot as R


class Guards(unittest.TestCase):
    def env(self, **kw):
        base = {"ARB_FUNDING_CHAIN": "robinhood", "ARB_VAULT_CHAIN_ID": "4663", "DATABASE_URL": "postgresql://u:p@db/pilot"}
        base.update(kw)
        return base

    def test_ledgers_go_to_the_pilot_dir(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.env(ARB_PILOT_DATA_DIR=d)
            self.assertEqual(R.prepare_env(env), [])
            for var, name in R.LEDGERS.items():
                self.assertEqual(env[var], os.path.join(os.path.realpath(d), name))

    def test_refusals(self):
        self.assertIn("ARB_FUNDING_CHAIN must be robinhood", R.prepare_env(self.env(ARB_FUNDING_CHAIN="base", ARB_PILOT_DATA_DIR="/tmp/x")))
        self.assertIn("ARB_VAULT_CHAIN_ID must be 4663", R.prepare_env(self.env(ARB_VAULT_CHAIN_ID="8453", ARB_PILOT_DATA_DIR="/tmp/x")))
        self.assertTrue(any("ARB_PILOT_DATA_DIR is required" in p for p in R.prepare_env(self.env())))
        self.assertTrue(any("live engine" in p for p in R.prepare_env(self.env(ARB_PILOT_DATA_DIR="~/PolyNotifyBot/data"))))
        with tempfile.TemporaryDirectory() as d:
            bad = self.env(ARB_PILOT_DATA_DIR=d, ARB_INTENT_LEDGER_PATH="~/PolyNotifyBot/data/arb_funding_intents.json")
            self.assertTrue(any("ARB_INTENT_LEDGER_PATH" in p for p in R.prepare_env(bad)))

    def test_db_target_has_no_credentials(self):
        self.assertEqual(R.db_target("postgresql://user:secret@db.host:5432/pilot"), "postgresql://db.host:5432/pilot")


if __name__ == "__main__":
    unittest.main()

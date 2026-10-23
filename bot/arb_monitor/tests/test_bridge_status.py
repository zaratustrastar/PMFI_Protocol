import unittest

from arb_monitor.core.arb_bridge_status import in_transit_rows


class BridgeStatusTest(unittest.TestCase):
    def test_reports_only_money_in_transit(self):
        snap = {
            "withdrawals": [{"id": "w1", "platform": "polymarket", "amount_usdc": 500.0, "status": "pending", "created_at": 100},
                            {"id": "w0", "amount_usdc": 5.0, "status": "completed", "created_at": 50}],
            "bridges": [{"tx_hash": "0xab", "direction": "rh_to_base", "amount_usdc": 20.0, "status": "stale", "created_at": 90}],
            "venue_deposits": [{"id": "k1", "amount_usdc": 7.0, "status": "pending", "created_at": 95, "receipt": "unconfirmed"}],
            "intents": [{"idem_key": "i1", "amount_usdc": 3.0, "status": "broadcast", "ledgered": False, "created_at": 99},
                        {"idem_key": "i2", "amount_usdc": 3.0, "status": "broadcast", "ledgered": True, "created_at": 99}],
        }
        rows = in_transit_rows(snap)
        self.assertEqual(sorted(r["id"] for r in rows), ["0xab", "i1", "k1", "w1"])
        w1 = next(r for r in rows if r["id"] == "w1")
        self.assertEqual((w1["kind"], w1["amount"], w1["status"], w1["startedAt"]), ("withdrawals:polymarket", 500.0, "pending", 100))


if __name__ == "__main__":
    unittest.main()

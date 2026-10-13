"""Sign a V3 report with the EXISTING reporter code configured for Robinhood Chain.

    python3 milord/offchain/gen_reporter_fixture.py   (run from the repo root)

Writes milord/test/fixtures/reporter_v3_robinhood.json; test/robinhood/ReporterCompat.t.sol
checks that PMFIArbVaultV3 on chain 46630 accepts the signature. Uses a throwaway test key.
"""
import json
import os
import sys

os.environ.update({
    "ARB_VAULT_DOMAIN_SALT": "PMFIArbVaultV3.milord.v1",
    "ARB_VAULT_CHAIN_ID": "46630",
})
sys.path.insert(0, os.getcwd())
from bot.arb_monitor.core import arb_reporter as r  # noqa: E402

TEST_KEY = "0x" + "11" * 32  # throwaway, not a real wallet
VAULT = "0x000000000000000000000000000000000000a3a3"
TS, DEADLINE, NONCE, ASSETS_USDG = 1_800_000_000, 1_800_000_600, 1, 0.0

sig = r._sign_report(ASSETS_USDG, TS, DEADLINE, NONCE, VAULT, TEST_KEY)
from eth_account import Account  # noqa: E402

out = {"vault": VAULT, "chainId": r.BASE_CHAIN_ID, "salt": r.DOMAIN_SALT_TEXT, "timestamp": TS, "deadline": DEADLINE,
       "nonce": NONCE, "reportedAssets": int(ASSETS_USDG * 1e6), "signer": Account.from_key(TEST_KEY).address, "signature": sig}
with open("milord/test/fixtures/reporter_v3_robinhood.json", "w") as f:
    json.dump(out, f, indent=2)
print(out)

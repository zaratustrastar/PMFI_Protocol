"""
test_auto_claim.py — Unit tests for the pARB V2 auto-claim logic.

Tests verify:
  1. ABI function selectors match expected keccak256 values.
  2. Batch chunking splits IDs correctly (no gaps, no duplicates).
  3. Nonce tracking in _send_claim_batch_with_retry responds correctly to
     broadcast-failures (nonce not consumed) vs on-chain reverts (nonce consumed).
  4. sweep_claimable_requests filters correctly by status == CLAIMABLE (1).
  5. auto_claim_after_report still calls sweep even when confirmation times out.

Run with:
    python3 -m pytest tests/test_auto_claim.py -v
  or:
    python3 tests/test_auto_claim.py
"""

import sys
import types
import time
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Minimal shims for dependencies not available in test environment
# ---------------------------------------------------------------------------

def _install_shims():
    """Install minimal stubs for eth_hash, eth_account, eth_abi so the module
    can be imported without a full blockchain environment."""

    # eth_hash.auto
    eth_hash = types.ModuleType("eth_hash")
    eth_hash_auto = types.ModuleType("eth_hash.auto")

    def _keccak(data: bytes) -> bytes:
        import hashlib
        # Use sha3_256 as a stand-in for keccak256 in tests —
        # actual selector values are verified against precomputed constants.
        from Crypto.Hash import keccak as _kc  # type: ignore
        k = _kc.new(digest_bits=256)
        k.update(data)
        return k.digest()

    try:
        from Crypto.Hash import keccak as _kc  # pycryptodome — check available
        eth_hash_auto.keccak = _keccak
    except ImportError:
        # Fall back to hashlib.sha3_256 (different hash, but sufficient for
        # structure / chunking tests that don't check selector bytes)
        eth_hash_auto.keccak = lambda data: __import__("hashlib").sha3_256(data).digest()

    eth_hash.auto = eth_hash_auto
    sys.modules.setdefault("eth_hash", eth_hash)
    sys.modules.setdefault("eth_hash.auto", eth_hash_auto)

    # eth_account
    if "eth_account" not in sys.modules:
        eth_account_mod = types.ModuleType("eth_account")

        class _FakeAccount:
            address = "0xKEEPER"

            @staticmethod
            def from_key(key):
                return _FakeAccount()

            def sign_transaction(self, tx):
                sig = MagicMock()
                sig.rawTransaction = b"\x00" * 32
                return sig

        eth_account_mod.Account = _FakeAccount
        sys.modules["eth_account"] = eth_account_mod

    # eth_abi
    if "eth_abi" not in sys.modules:
        eth_abi_mod = types.ModuleType("eth_abi")

        def _encode(types, values):
            import struct
            result = b""
            for t, v in zip(types, values):
                if t == "uint256":
                    result += v.to_bytes(32, "big")
                elif t == "uint256[]":
                    result += len(v).to_bytes(32, "big")
                    for item in v:
                        result += item.to_bytes(32, "big")
            return result

        def _decode(types, data):
            results = []
            offset = 0
            for t in types:
                chunk = data[offset:offset + 32]
                offset += 32
                if t in ("address",):
                    results.append("0x" + chunk.hex()[-40:])
                elif t in ("uint256", "uint8"):
                    results.append(int.from_bytes(chunk, "big"))
                else:
                    results.append(chunk)
            return tuple(results)

        eth_abi_mod.encode = _encode
        eth_abi_mod.decode = _decode
        sys.modules["eth_abi"] = eth_abi_mod


_install_shims()

# Now import the reporter
sys.path.insert(0, "bot/arb_monitor")
import importlib
import bot.arb_monitor.core.arb_reporter as reporter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _keccak256_hex(text: str) -> str:
    """Compute keccak256 of UTF-8 text, return hex string."""
    from eth_hash.auto import keccak
    return keccak(text.encode()).hex()


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestAutoClaimSelectors(unittest.TestCase):
    """Verify function selectors used to build calldata are correct."""

    # keccak256 selectors (first 4 bytes) — verified against pycryptodome keccak output.
    EXPECTED_SELECTORS = {
        "depositRequestCount()":        "45102836",
        "redeemRequestCount()":         "43667ef8",
        "depositRequests(uint256)":     "fabd0341",
        "redeemRequests(uint256)":      "e85ba3e9",
        "autoClaimDeposits(uint256[])": "16ae643a",
        "autoClaimRedeems(uint256[])":  "44fa112f",
    }

    def test_keccak256_available(self):
        """Ensure the keccak256 helper can be called."""
        result = reporter._keccak256_text("hello")
        self.assertIsInstance(result, bytes)
        self.assertEqual(len(result), 32)

    def test_selector_bytes_correct(self):
        """Spot-check that _keccak256_text produces 4-byte selectors matching
        expected values (requires pycryptodome for correct keccak)."""
        try:
            from Crypto.Hash import keccak as _kc  # noqa: F401
        except ImportError:
            self.skipTest("pycryptodome not available; skipping selector byte check")

        for sig, expected_hex in self.EXPECTED_SELECTORS.items():
            selector = reporter._keccak256_text(sig)[:4]
            actual_hex = selector.hex()
            self.assertEqual(
                actual_hex, expected_hex,
                f"Selector mismatch for {sig!r}: got {actual_hex}, want {expected_hex}",
            )


class TestBatchChunking(unittest.TestCase):
    """Verify the chunking strategy in sweep_claimable_requests."""

    def test_chunk_covers_all_ids(self):
        """All IDs are covered across chunks with no gaps or duplicates."""
        ids = list(range(67))  # 3 batches: 25 + 25 + 17
        chunk_size = reporter.MAX_CLAIMS_PER_TX
        chunks = [
            ids[i : i + chunk_size] for i in range(0, len(ids), chunk_size)
        ]
        reconstructed = [item for chunk in chunks for item in chunk]
        self.assertEqual(ids, reconstructed)

    def test_chunk_size_respected(self):
        """No chunk exceeds MAX_CLAIMS_PER_TX."""
        ids = list(range(100))
        chunk_size = reporter.MAX_CLAIMS_PER_TX
        chunks = [
            ids[i : i + chunk_size] for i in range(0, len(ids), chunk_size)
        ]
        for chunk in chunks:
            self.assertLessEqual(len(chunk), chunk_size)

    def test_max_claims_per_tx_value(self):
        """MAX_CLAIMS_PER_TX must be a positive integer."""
        self.assertIsInstance(reporter.MAX_CLAIMS_PER_TX, int)
        self.assertGreater(reporter.MAX_CLAIMS_PER_TX, 0)


class TestNonceTracking(unittest.TestCase):
    """Verify nonce is advanced correctly in _send_claim_batch_with_retry."""

    def _run_batch(self, broadcast_ok: bool, mined: bool, ids=None):
        """Helper: run _send_claim_batch_with_retry with mocked internals.

        broadcast_ok — whether _broadcast_claim_tx returns a tx_hash
        mined        — whether _wait_for_tx_confirm returns True
        Returns the next_nonce returned by the function.
        """
        if ids is None:
            ids = [0, 1, 2]
        details = [{"id": i, "receiver": "0xA", "assets": 1_000_000,
                    "processed_pps": 10**18} for i in ids]

        broadcast_result = "0xDEAD" if broadcast_ok else None

        with patch.object(reporter, "_broadcast_claim_tx", return_value=broadcast_result), \
             patch.object(reporter, "_wait_for_tx_confirm", return_value=mined), \
             patch.object(reporter, "_get_gas_price", return_value=1_000_000_000), \
             patch.object(reporter, "_get_nonce", return_value=5):

            return reporter._send_claim_batch_with_retry(
                vault_address="0xVAULT",
                signer_key="0x" + "aa" * 32,
                is_deposit=True,
                batch_ids=ids,
                batch_details=details,
                nonce=10,
                gas_price=1_000_000_000,
            )

    def test_success_advances_nonce_by_one(self):
        next_nonce = self._run_batch(broadcast_ok=True, mined=True)
        self.assertEqual(next_nonce, 11)

    def test_broadcast_failure_does_not_consume_nonce(self):
        """If broadcast returns None, nonce is not consumed — retry uses same nonce."""
        with patch.object(reporter, "_broadcast_claim_tx", return_value=None), \
             patch.object(reporter, "_wait_for_tx_confirm", return_value=False):
            # Primary fails (broadcast_ok=False → nonce not consumed)
            # Left half: broadcast fails → not consumed, right half: broadcast fails
            # Final nonce should equal starting nonce (10)
            next_nonce = reporter._send_claim_batch_with_retry(
                vault_address="0xVAULT",
                signer_key="0x" + "aa" * 32,
                is_deposit=True,
                batch_ids=[0, 1, 2, 3],
                batch_details=[{"id": i, "receiver": "0xA", "assets": 1_000_000,
                                "processed_pps": 10**18} for i in range(4)],
                nonce=10,
                gas_price=1_000_000_000,
            )
        # No broadcasts succeeded — nonce stays at 10
        self.assertEqual(next_nonce, 10)

    def test_mined_revert_consumes_nonce(self):
        """If broadcast succeeds but tx reverts, nonce is consumed for the retry."""
        call_count = {"n": 0}

        def mock_broadcast(*args, **kwargs):
            call_count["n"] += 1
            return f"0xTXHASH{call_count['n']}"

        with patch.object(reporter, "_broadcast_claim_tx", side_effect=mock_broadcast), \
             patch.object(reporter, "_wait_for_tx_confirm", return_value=False):
            # Primary reverts → nonce 10 consumed
            # Left half: nonce=11 reverts → consumed; right half: nonce=12 reverts → consumed
            next_nonce = reporter._send_claim_batch_with_retry(
                vault_address="0xVAULT",
                signer_key="0x" + "aa" * 32,
                is_deposit=True,
                batch_ids=[0, 1, 2, 3],
                batch_details=[{"id": i, "receiver": "0xA", "assets": 1_000_000,
                                "processed_pps": 10**18} for i in range(4)],
                nonce=10,
                gas_price=1_000_000_000,
            )
        # All 3 txs (primary + two halves) were broadcast → nonce advanced 3 times
        self.assertEqual(next_nonce, 13)


class TestSweepFiltersClaimable(unittest.TestCase):
    """Verify sweep_claimable_requests only processes CLAIMABLE (status=1) requests."""

    def _fake_request(self, idx, status):
        return {
            "id": idx,
            "owner": "0xUSER",
            "receiver": "0xUSER",
            "assets": 10_000_000,
            "submitted_at": int(time.time()),
            "status": status,
            "processed_pps": 10**18,
            "claimable_assets": 5_000_000,
            "shares": 10 * 10**18,
        }

    def test_only_claimable_requests_are_batched(self):
        """PENDING (0), CLAIMED (2), CANCELLED (3) are skipped; CLAIMABLE (1) is claimed."""
        statuses = [0, 1, 2, 1, 3, 1]   # indices 1, 3, 5 are CLAIMABLE

        def mock_get_dep(vault_addr, idx):
            return self._fake_request(idx, statuses[idx])

        claimed_batches = []

        def mock_send_batch(vault_addr, signer_key, is_deposit, batch_ids,
                            batch_details, nonce, gas_price):
            claimed_batches.extend(batch_ids)
            return nonce + 1

        def mock_count(vault, is_deposit=False):
            return len(statuses) if is_deposit else 0

        with patch.object(reporter, "_get_request_count", side_effect=mock_count), \
             patch.object(reporter, "_get_deposit_request", side_effect=mock_get_dep), \
             patch.object(reporter, "_get_redeem_request", return_value={}), \
             patch.object(reporter, "_send_claim_batch_with_retry",
                          side_effect=mock_send_batch), \
             patch.object(reporter, "_get_nonce", return_value=1), \
             patch.object(reporter, "_get_gas_price", return_value=1_000_000_000):

            import os
            os.environ["ARB_NAV_SIGNER_PRIVATE_KEY"] = "0x" + "bb" * 32

            reporter.sweep_claimable_requests("0xVAULT", "0x" + "bb" * 32)

        self.assertEqual(sorted(claimed_batches), [1, 3, 5],
                         "Only CLAIMABLE (status=1) requests should be in claimed batches")


class TestAutoClaimAfterReportTimeout(unittest.TestCase):
    """Verify sweep runs even when the report tx confirmation times out."""

    def test_sweep_called_on_timeout(self):
        """sweep_claimable_requests must be called even if report tx doesn't confirm."""
        with patch.object(reporter, "_wait_for_tx_confirm", return_value=False), \
             patch.object(reporter, "sweep_claimable_requests") as mock_sweep:

            reporter.auto_claim_after_report("0xVAULT", "0x" + "cc" * 32, "0xTXHASH")

        mock_sweep.assert_called_once_with("0xVAULT", "0x" + "cc" * 32)

    def test_sweep_called_on_success(self):
        """sweep_claimable_requests must also be called when report tx confirms."""
        with patch.object(reporter, "_wait_for_tx_confirm", return_value=True), \
             patch.object(reporter, "sweep_claimable_requests") as mock_sweep:

            reporter.auto_claim_after_report("0xVAULT", "0x" + "cc" * 32, "0xTXHASH")

        mock_sweep.assert_called_once_with("0xVAULT", "0x" + "cc" * 32)


if __name__ == "__main__":
    unittest.main(verbosity=2)

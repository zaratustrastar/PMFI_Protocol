"""Kalshi RSA Authentication helper.

Kalshi API v2 uses RSA-PSS + SHA-256 signing.
Each request requires three headers:
  KALSHI-ACCESS-KEY        — your API key ID
  KALSHI-ACCESS-TIMESTAMP  — current time in milliseconds (string)
  KALSHI-ACCESS-SIGNATURE  — base64( RSA_PSS_sign( "{ts}{METHOD}{path}" ) )

The path must be the URL path only (no query string, no host).
Example: /trade-api/v2/portfolio/orders

Environment variables:
  KALSHI_API_KEY_ID        — API key ID from Kalshi dashboard (e.g. dc5fed12-...)
  KALSHI_PRIVATE_KEY_PATH  — path to your RSA private key PEM file on disk
  KALSHI_PRIVATE_KEY_PEM   — alternatively, the PEM content directly as an env var
                             (use if you prefer not to deal with a file path)
"""

import os
import time
import base64
from urllib.parse import urlparse
from typing import Optional


def _load_private_key():
    """Load RSA private key from file path or PEM env var."""
    from cryptography.hazmat.primitives import serialization

    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    pem_content = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "")

    if pem_path:
        with open(pem_path, "rb") as f:
            pem_bytes = f.read()
    elif pem_content:
        pem_bytes = pem_content.replace("\\n", "\n").encode("utf-8")
    else:
        raise ValueError(
            "Kalshi private key not configured — set KALSHI_PRIVATE_KEY_PATH "
            "(path to PEM file) or KALSHI_PRIVATE_KEY_PEM (PEM content)"
        )

    return serialization.load_pem_private_key(
        pem_bytes,
        password=None,
    )


def get_kalshi_headers(method: str, url: str) -> Optional[dict]:
    """Build Kalshi RSA-PSS authentication headers for a given request.

    Args:
        method: HTTP method (GET, POST, DELETE — will be uppercased)
        url:    Full URL or just the path (e.g. https://...kalshi.com/trade-api/v2/portfolio/orders)

    Returns:
        Dict of headers to merge into the request, or None if credentials missing.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    if not key_id:
        print("❌ [KalshiAuth] Missing KALSHI_API_KEY_ID")
        return None

    ts = str(int(time.time() * 1000))
    path = urlparse(url).path or "/"
    method = method.upper()

    msg = f"{ts}{method}{path}".encode("utf-8")

    try:
        private_key = _load_private_key()
        signature = private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode("utf-8")
    except Exception as e:
        print(f"❌ [KalshiAuth] RSA signing failed: {e!r}")
        return None

    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "Accept": "application/json",
    }


def kalshi_auth_available() -> bool:
    """Return True if Kalshi RSA credentials are configured."""
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    has_key = bool(
        os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        or os.environ.get("KALSHI_PRIVATE_KEY_PEM", "")
    )
    return bool(key_id and has_key)

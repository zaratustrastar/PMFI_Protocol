"""Polymarket CLOB SDK compatibility layer (V1 -> V2)."""

SDK_VERSION: str


def _log(msg: str) -> None:
    print(f"[Arb/CLOBCompat] {msg}", flush=True)


try:
    from py_clob_client_v2.client import ClobClient as _ClobClient
    from py_clob_client_v2.clob_types import (
        ApiCreds,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        BalanceAllowanceParams,
        AssetType,
    )
    try:
        from py_clob_client_v2.exceptions import PolyApiException
    except Exception:
        PolyApiException = Exception
    SDK_VERSION = "v2"
    _log("Loaded py-clob-client-v2 (Python SDK V2) -- module=" + _ClobClient.__module__)
except ImportError:
    from py_clob_client.client import ClobClient as _ClobClient
    from py_clob_client.clob_types import (
        ApiCreds,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        BalanceAllowanceParams,
        AssetType,
    )
    try:
        from py_clob_client.exceptions import PolyApiException
    except Exception:
        PolyApiException = Exception
    SDK_VERSION = "v1"
    _log(
        "WARNING: Loaded LEGACY py-clob-client (V1) -- "
        "this STOPS WORKING after the Polymarket V2 cutover. "
        "Run: pip install py-clob-client-v2"
    )


def make_client(
    host,
    key,
    chain_id=137,
    creds=None,
    signature_type=2,
    funder=None,
):
    """Construct a ClobClient that works against either V1 or V2."""
    common = {"key": key, "signature_type": signature_type}
    if creds is not None:
        common["creds"] = creds
    if funder is not None:
        common["funder"] = funder

    if SDK_VERSION == "v2":
        try:
            return _ClobClient(host, chain=chain_id, **common)
        except TypeError as e:
            if "chain" in str(e):
                return _ClobClient(host, chain_id=chain_id, **common)
            raise
    return _ClobClient(host, chain_id=chain_id, **common)


__all__ = [
    "make_client",
    "SDK_VERSION",
    "ApiCreds",
    "OrderArgs",
    "OrderType",
    "PartialCreateOrderOptions",
    "BalanceAllowanceParams",
    "AssetType",
    "PolyApiException",
]

import os
import sys
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

POLYGON_RPC = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
SAFE_ADDRESS = Web3.to_checksum_address(os.getenv("POLY_PROXY_ADDRESS", ""))
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

raw_pk = os.getenv("POLY_PRIVATE_KEY", "").strip()
if not raw_pk:
    raise SystemExit("POLY_PRIVATE_KEY missing")
if not raw_pk.startswith("0x"):
    raw_pk = "0x" + raw_pk

acct = Account.from_key(raw_pk)
OWNER = Web3.to_checksum_address(acct.address)

if not SAFE_ADDRESS:
    raise SystemExit("POLY_PROXY_ADDRESS missing")

if len(sys.argv) < 2:
    raise SystemExit("Usage: python3 direct_safe_withdraw.py <amount_usdc> [recipient] [--dry-run]")

amount_usdc = float(sys.argv[1])
recipient = Web3.to_checksum_address(sys.argv[2]) if len(sys.argv) >= 3 and not sys.argv[2].startswith("--") else OWNER
dry_run = "--dry-run" in sys.argv

amount_raw = int(round(amount_usdc * 1_000_000))
if amount_raw <= 0:
    raise SystemExit("amount must be > 0")

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))

safe_abi = [
    {"inputs":[],"name":"nonce","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[
        {"internalType":"address","name":"to","type":"address"},
        {"internalType":"uint256","name":"value","type":"uint256"},
        {"internalType":"bytes","name":"data","type":"bytes"},
        {"internalType":"uint8","name":"operation","type":"uint8"},
        {"internalType":"uint256","name":"safeTxGas","type":"uint256"},
        {"internalType":"uint256","name":"baseGas","type":"uint256"},
        {"internalType":"uint256","name":"gasPrice","type":"uint256"},
        {"internalType":"address","name":"gasToken","type":"address"},
        {"internalType":"address","name":"refundReceiver","type":"address"},
        {"internalType":"bytes","name":"signatures","type":"bytes"}
    ],"name":"execTransaction","outputs":[{"internalType":"bool","name":"success","type":"bool"}],"stateMutability":"payable","type":"function"}
]

erc20_abi = [
    {"inputs":[
        {"internalType":"address","name":"to","type":"address"},
        {"internalType":"uint256","name":"amount","type":"uint256"}
    ],"name":"transfer","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}
]

safe = w3.eth.contract(address=SAFE_ADDRESS, abi=safe_abi)
usdc = w3.eth.contract(address=USDC_E, abi=erc20_abi)

safe_nonce = safe.functions.nonce().call()
safe_balance = usdc.functions.balanceOf(SAFE_ADDRESS).call()

if amount_raw > safe_balance:
    raise SystemExit(f"amount exceeds safe balance: want={amount_raw} have={safe_balance}")

inner_data = usdc.encode_abi("transfer", args=[recipient, amount_raw])

typed = {
    "types": {
        "EIP712Domain": [
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "SafeTx": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "nonce", "type": "uint256"},
        ],
    },
    "primaryType": "SafeTx",
    "domain": {"chainId": 137, "verifyingContract": SAFE_ADDRESS},
    "message": {
        "to": USDC_E,
        "value": 0,
        "data": inner_data,
        "operation": 0,
        "safeTxGas": 0,
        "baseGas": 0,
        "gasPrice": 0,
        "gasToken": "0x0000000000000000000000000000000000000000",
        "refundReceiver": "0x0000000000000000000000000000000000000000",
        "nonce": safe_nonce,
    },
}

signable = encode_typed_data(full_message=typed)
signed = Account.sign_message(signable, private_key=raw_pk)

# Safe expects packed signature: r + s + v
sig = signed.signature
r = sig[:32]
s = sig[32:64]
v = bytes([sig[64]])
packed_sig = r + s + v

tx = safe.functions.execTransaction(
    USDC_E,
    0,
    inner_data,
    0,
    0,
    0,
    0,
    "0x0000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000000000",
    packed_sig,
)

estimated = tx.estimate_gas({"from": OWNER})

print(f"Owner: {OWNER}")
print(f"Safe: {SAFE_ADDRESS}")
print(f"Recipient: {recipient}")
print(f"Safe nonce: {safe_nonce}")
print(f"Safe balance raw: {safe_balance}")
print(f"Amount raw: {amount_raw}")
print(f"Estimated gas: {estimated}")

if dry_run:
    print("DRY_RUN=1")
    sys.exit(0)

nonce = w3.eth.get_transaction_count(OWNER)
gas_price = w3.eth.gas_price

built = tx.build_transaction({
    "from": OWNER,
    "nonce": nonce,
    "gas": int(estimated * 1.2),
    "gasPrice": gas_price,
    "chainId": 137,
})

signed_tx = w3.eth.account.sign_transaction(built, raw_pk)
tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)

print(f"TX_HASH: {tx_hash.hex()}")
print(f"RECEIPT_STATUS: {receipt.status}")

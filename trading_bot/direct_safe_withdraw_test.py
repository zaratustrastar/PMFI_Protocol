import os
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
RECIPIENT = OWNER

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
    ],"name":"transfer","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}
]

safe = w3.eth.contract(address=SAFE_ADDRESS, abi=safe_abi)
usdc = w3.eth.contract(address=USDC_E, abi=erc20_abi)

amount_raw = 10_000  # 0.01 USDC.e
safe_nonce = safe.functions.nonce().call()

inner_data = usdc.encode_abi("transfer", args=[RECIPIENT, amount_raw])

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
    "domain": {
        "chainId": 137,
        "verifyingContract": SAFE_ADDRESS,
    },
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
).build_transaction({
    "from": OWNER,
    "nonce": w3.eth.get_transaction_count(OWNER),
    "gasPrice": w3.eth.gas_price,
    "chainId": 137,
})

try:
    gas_est = w3.eth.estimate_gas(tx)
    tx["gas"] = int(gas_est * 1.2)
    print("Estimated gas:", gas_est)
except Exception as e:
    print("estimate_gas failed:", e)
    tx["gas"] = 500000

print("Owner:", OWNER)
print("Safe:", SAFE_ADDRESS)
print("Recipient:", RECIPIENT)
print("Safe nonce:", safe_nonce)
print("Amount raw:", amount_raw)
print("Inner calldata:", inner_data)
print("Gas:", tx["gas"])

signed_tx = acct.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
print("TX_HASH:", tx_hash.hex())

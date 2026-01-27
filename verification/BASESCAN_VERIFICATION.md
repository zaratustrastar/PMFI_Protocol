# Basescan Manual Verification

Contract: `0x08C58b344401abf0C9696E98Fc7a465835d3EdB6`

## Steps

1. Go to https://basescan.org/address/0x08C58b344401abf0C9696E98Fc7a465835d3EdB6#code

2. Click **"Verify and Publish"**

3. Select:
   - Compiler Type: **Solidity (Standard-Json-Input)**
   - Compiler Version: **v0.8.20+commit.a1b79de6**
   - License: **MIT**

4. Upload the file: `v7-standard-input.json` (in this folder)

5. Add Constructor Arguments (ABI-encoded):
```
000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda0291300000000000000000000000059d0461ec7c4688dd3daab7ea903d93d109db9e000000000000000000000000059d0461ec7c4688dd3daab7ea903d93d109db9e00000000000000000000000002b20920a00d705043260ebfe6561bc96fbd84dbe00000000000000000000000000000000000000000000000000038d7ea4c68000000000000000000000000000000000000000000000000000000000174876e8000000000000000000000000000000000000000000000000000000000000000bb8
```

6. Click **"Verify and Publish"**

## Constructor Arguments Decoded

| Parameter | Value |
|-----------|-------|
| _usdc | 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 |
| _navSigner | 0x59D0461ec7C4688dd3DAab7Ea903d93d109dB9E0 |
| _taxCollector | 0x59D0461ec7C4688dd3DAab7Ea903d93d109dB9E0 |
| _polymarketWallet | 0x2b20920A00D705043260eBFE6561bC96FBd84dBE |
| _maxDepositPerWallet | 1000000000000000 (no limit) |
| _maxTotalDeposits | 100000000000 (100k USDC) |
| _maxLossBps | 3000 (30%) |

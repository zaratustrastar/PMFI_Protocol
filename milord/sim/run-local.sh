#!/usr/bin/env bash
# Fresh anvil -> Permit2 at its canonical address -> deploy (protocol + local Hookr-style v4 market)
# -> end-to-end simulation. Local only; anvil public test keys only.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f env.sh ] && . ./env.sh
pkill -x anvil 2>/dev/null || true
anvil --silent --port 8545 --block-base-fee-per-gas 0 --code-size-limit 50000 >/dev/null 2>&1 &
sleep 2
cast rpc --rpc-url http://127.0.0.1:8545 anvil_setCode 0x000000000022D473030F116dDEE9F6B43aC78BA3 "$(cat sim/permit2.runtime.hex)" >/dev/null
forge script script/DeployLocal.s.sol --rpc-url http://127.0.0.1:8545 --broadcast --slow >/dev/null
node sim/simulate.mjs

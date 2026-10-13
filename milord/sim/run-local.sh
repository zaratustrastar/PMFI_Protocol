#!/usr/bin/env bash
# Fresh anvil -> deploy -> 3-week simulation. Local only.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f env.sh ] && . ./env.sh
pkill -x anvil 2>/dev/null || true
anvil --silent --port 8545 --block-base-fee-per-gas 0 >/dev/null 2>&1 &
sleep 2
forge script script/DeployLocal.s.sol --rpc-url http://127.0.0.1:8545 --broadcast >/dev/null
node sim/simulate.mjs

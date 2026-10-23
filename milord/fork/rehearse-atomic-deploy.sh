#!/usr/bin/env bash
# Rehearse the scripted launch path on a PRIVATE local Anvil fork of Robinhood Chain (4663):
#   1. script/DeployAtomicLaunch.s.sol deploys MilordAtomicLaunch (flat launch) with the PUBLIC Anvil key #0;
#   2. the "Team Safe" (a stand-in contract address on the fork, impersonated) calls launch{value: Hookr fee};
#   3. offchain/record-launch.mjs reads the result back (read-only) into a temporary deployments file.
# Everything happens inside the local fork: nothing reaches Robinhood Chain. The fork URL must be 127.0.0.1, answer as
# Anvil and report a fork, exactly as in run-fork-tests.sh. Production artifacts from CI (no via-IR compile) unless
# FORK_BUILD=lowmem.
#
# Usage (from milord/): ./fork/rehearse-atomic-deploy.sh      Env: FORK_PORT (8549), ROBINHOOD_RPC, FORK_BLOCK, FORK_BUILD
set -euo pipefail
cd "$(dirname "$0")/.."
unset PRIVATE_KEY ETH_PRIVATE_KEY DEPLOYER_PRIVATE_KEY ETH_KEYSTORE ETH_PASSWORD ETH_FROM MNEMONIC 2>/dev/null || true

UPSTREAM="${ROBINHOOD_RPC:-https://rpc.mainnet.chain.robinhood.com}"
FORK_PORT="${FORK_PORT:-8549}"
FORK_URL="http://127.0.0.1:${FORK_PORT}"
ANVIL0_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 # public Anvil test key #0
SAFE=0x0000000000000000000000000000000000005AfE01  # stand-in Team Safe (code etched on the fork only)
TAXR=0x0000000000000000000000000000000000005AfE02  # stand-in TradingTaxSplitter (must be a contract)
LAUNCHER=0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4
mkdir -p fork/logs
LOG="fork/logs/rehearse-$(date -u +%Y%m%dT%H%M%SZ).log"
TMP=$(mktemp -d)
ANVIL_PID=""
say() { echo "$*" | tee -a "$LOG"; }
die() { say "REFUSED: $*"; exit 1; }
cleanup() { [ -n "$ANVIL_PID" ] && kill "$ANVIL_PID" 2>/dev/null && wait "$ANVIL_PID" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

if [ "${FORK_BUILD:-ci}" = "lowmem" ]; then
  export FOUNDRY_PROFILE=lowmem
else
  [ -d out ] && [ -f cache/solidity-files-cache.json ] || die "no production artifacts in out/: run ./fork/run-fork-tests.sh first (it downloads them) or FORK_BUILD=lowmem"
  export FOUNDRY_SOLC="$PWD/sim/no-compile-solc.sh" FOUNDRY_OFFLINE=true
  unset FOUNDRY_PROFILE
fi

(exec 3<>"/dev/tcp/127.0.0.1/${FORK_PORT}") 2>/dev/null && die "port ${FORK_PORT} is in use"
ARGS=(--fork-url "$UPSTREAM" --host 127.0.0.1 --port "$FORK_PORT" --retries 10 --timeout 60000 --silent)
[ -n "${FORK_BLOCK:-}" ] && ARGS+=(--fork-block-number "$FORK_BLOCK")
nice -n 10 anvil "${ARGS[@]}" >>"$LOG" 2>&1 &
ANVIL_PID=$!
for _ in $(seq 1 300); do cast chain-id --rpc-url "$FORK_URL" >/dev/null 2>&1 && break; sleep 0.2; done
case "$(cast client --rpc-url "$FORK_URL")" in anvil*) ;; *) die "not Anvil" ;; esac
cast rpc --rpc-url "$FORK_URL" anvil_nodeInfo | grep -q '"forkBlockNumber"' || die "Anvil is not a fork"
[ "$(cast chain-id --rpc-url "$FORK_URL")" = 4663 ] || die "fork is not chain 4663"
[ "$(cast codesize --rpc-url "$FORK_URL" $LAUNCHER)" -gt 0 ] || die "HookrLauncher has no code on the fork"
say "local fork $FORK_URL verified (chain 4663, block $(cast block-number --rpc-url "$FORK_URL"))"

# stand-ins exist only on this fork
for A in $SAFE $TAXR; do cast rpc --rpc-url "$FORK_URL" anvil_setCode $A 0x00 >/dev/null; done
cast rpc --rpc-url "$FORK_URL" anvil_setBalance $SAFE 0xDE0B6B3A7640000 >/dev/null # 1 ETH, fork only

say "== 1. DeployAtomicLaunch.s.sol (Anvil key #0, local fork)"
MILORD_LOCAL_FORK=yes MILORD_TEAM_SAFE=$SAFE forge script script/DeployAtomicLaunch.s.sol \
  --rpc-url "$FORK_URL" --broadcast --private-key $ANVIL0_KEY --slow 2>&1 | tee -a "$LOG" | grep -E "MilordAtomicLaunch|MILORD_PREDICTED_TOKEN|launch fee|Error" || true
HELPER=$(node -e 'const r=require("./broadcast/DeployAtomicLaunch.s.sol/4663/run-latest.json");console.log(r.transactions.find(t=>t.contractName==="MilordAtomicLaunch").contractAddress)')
PRED=$(cast call --rpc-url "$FORK_URL" "$HELPER" "predictedToken()(address)")
[ "$(cast codesize --rpc-url "$FORK_URL" "$PRED")" = 0 ] || die "predicted token already has code"
say "helper $HELPER, predicted MILORD $PRED (no code yet)"

say "== 2. Team Safe stand-in: launch{value: fee}"
FEE=$(cast call --rpc-url "$FORK_URL" "$HELPER" "launchFee()(uint256)" | awk '{print $1}')
DEADLINE=$(( $(cast block --rpc-url "$FORK_URL" latest -f timestamp) + 3600 ))
cast rpc --rpc-url "$FORK_URL" anvil_impersonateAccount $SAFE >/dev/null
cast send --rpc-url "$FORK_URL" --unlocked --from $SAFE "$HELPER" "launch(address,uint256)" $TAXR $DEADLINE --value "$FEE" >>"$LOG" 2>&1 || die "launch reverted (see $LOG)"
cast rpc --rpc-url "$FORK_URL" anvil_stopImpersonatingAccount $SAFE >/dev/null
SUPPLY=$(cast call --rpc-url "$FORK_URL" "$PRED" "totalSupply()(uint256)" | awk '{print $1}')
say "launched: MILORD code $(cast codesize --rpc-url "$FORK_URL" "$PRED") bytes, totalSupply $SUPPLY, fee paid $FEE wei"

say "== 3. record-launch.mjs (read-only)"
echo '{"chainId":4663}' > "$TMP/deployments.json"
RPC_URL="$FORK_URL" node offchain/record-launch.mjs "$TMP/deployments.json" "$HELPER" ${STATE_VIEW:-} 2>&1 | tee -a "$LOG"
say "REHEARSAL PASSED (local fork only; log $LOG)"

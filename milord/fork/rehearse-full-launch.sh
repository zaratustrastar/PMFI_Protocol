#!/usr/bin/env bash
# Integrated launch rehearsal on a PRIVATE local Anvil fork of Robinhood Chain (4663). Everything runs inside the
# fork; nothing reaches Robinhood Chain. Public Anvil keys only; Safes are impersonated stand-in addresses.
#
#   1. script/DeployAtomicLaunch.s.sol   MilordAtomicLaunch (Anvil #0)                     -> predicted MILORD
#   2. script/DeployRobinhood.s.sol      the whole protocol against the predicted token  -> deployments.json
#   3. fork/rehearse-flows.mjs launch    Safe batches from offchain/safe-launch-batch.mjs: ownership (Team and
#                                        Arbitrage Safe), safe-1-launch; record-launch.mjs with the official StateView
#                                        (must be complete)
#   4. script/HookrPostLaunch.s.sol      V4SwapAdapter + HookrLockFeeCollector over the recorded pool key
#   5. fork/rehearse-flows.mjs operate   safe-2-accept, safe-3-lock (FORK ONLY), trading through the Hookr pool,
#                                        keeper passes (tax queues, splitters, LP fees, buyback, ETH->USDG router),
#                                        an NFT conversion with a signed TWAP quote, a signed vault report, deposit
#                                        settlement, a weekly root, checker, activation and a pARB claim.
#
# Fails closed: any refused check, failed forge run or recorder result other than "complete" stops it (exit 1).
# Usage (from milord/): ./fork/rehearse-full-launch.sh
# Env: FORK_PORT (8550), ROBINHOOD_RPC, FORK_BLOCK, FORK_KEEP=yes (leave the fork running for a service/frontend soak),
#      FORK_BUILD=ci|local (ci: CI artifacts for HEAD, as
#      fork/rehearse-atomic-deploy.sh; local: fresh optimized build, workstation only, ~6 GB RAM)
set -euo pipefail
cd "$(dirname "$0")/.."
unset PRIVATE_KEY ETH_PRIVATE_KEY DEPLOYER_PRIVATE_KEY ETH_KEYSTORE ETH_PASSWORD ETH_FROM MNEMONIC 2>/dev/null || true

UPSTREAM="${ROBINHOOD_RPC:-https://rpc.mainnet.chain.robinhood.com}"
FORK_PORT="${FORK_PORT:-8550}"
[[ "$FORK_PORT" =~ ^[0-9]+$ ]] && [ "$FORK_PORT" -ge 1024 ] && [ "$FORK_PORT" -le 65535 ] || { echo 'invalid FORK_PORT'; exit 1; }
[ -z "${FORK_URL:-}" ] || { echo 'FORK_URL override refused; runner owns its local fork'; exit 1; }
FORK_URL="http://127.0.0.1:${FORK_PORT}"
LAUNCHER=0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4
ANVIL0_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 # public Anvil test key #0
# Public Anvil accounts #1-#6 hold the service roles; the keys are well known and never hold real funds.
SERVICER=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
QUOTE_SIGNER=0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC
REPORT_SIGNER=0x90F79bf6EB2c4f870365E785982E1f101E93b906
KEEPER=0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65
PUBLISHER=0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc
PAUSER=0x976EA74026E726554dB657fA54763abd0C3a0aa9
TEAM_SAFE=0x00000000000000000000000000000000005afe01  # stand-in Team Safe (owner of every admin role)
ARB_SAFE=0x00000000000000000000000000000000005afe03   # stand-in Arbitrage Funding Safe (router owner)
WETH=0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73
USDG=0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168
FUNDING_POOL=0x69bfaf19c9f377bb306a89aed9f6b07e2c1a8d9a
STATE_VIEW=0xf3334192d15450cdd385c8b70e03f9a6bd9e673b # Uniswap v4 StateView, Robinhood Chain (developers.uniswap.org)
mkdir -p fork/logs
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG="fork/logs/full-rehearsal-$STAMP.log"
TMP=$(mktemp -d)
# DeployRobinhood writes its manifest with vm.writeJson, which foundry.toml allows only under ./deployments (and
# ./sim). A unique, git-ignored temporary file there; mktemp never reuses an existing name, so no release manifest
# (deployments/robinhood-<chain>.json) can be overwritten. Moved into $TMP right after the script; removed on exit.
MANIFEST=""
ANVIL_PID=""
say() { echo "$*" | tee -a "$LOG"; }
die() { say "REFUSED: $*"; exit 1; }
cleanup() {
  if [ -n "$ANVIL_PID" ]; then kill "$ANVIL_PID" 2>/dev/null || true; wait "$ANVIL_PID" 2>/dev/null || true; fi
  rm -rf "$TMP"
  if [ -n "$MANIFEST" ]; then rm -f -- "$MANIFEST"; fi
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM
for A in "$TEAM_SAFE" "$ARB_SAFE" "$STATE_VIEW" "$FUNDING_POOL"; do [[ "$A" =~ ^0x[0-9a-fA-F]{40}$ ]] || die "malformed address $A"; done

export FOUNDRY_BROADCAST="$TMP/broadcast"
if [ "${FORK_BUILD:-ci}" = "local" ]; then
  unset FOUNDRY_PROFILE FOUNDRY_SOLC FOUNDRY_OFFLINE
  forge build
else
  [ "${FORK_BUILD:-ci}" = ci ] || die 'FORK_BUILD must be ci or local'
  HEAD_SHA=$(git rev-parse --verify HEAD) || die "no Git HEAD"
  [ "$(cat out/.ci-commit 2>/dev/null)" = "$HEAD_SHA" ] || die 'CI artifact commit mismatch; download artifacts for HEAD (./fork/run-fork-tests.sh does)'
  [ -z "$(git status --porcelain --untracked-files=normal -- .)" ] || die 'dirty milord tree: CI artifacts may be stale'
  [ -f cache/solidity-files-cache.json ] || die "no production artifacts in out/"
  export FOUNDRY_SOLC="$PWD/sim/no-compile-solc.sh" FOUNDRY_OFFLINE=true
  unset FOUNDRY_PROFILE
fi
for F in out/RehearsalTrader.sol/RehearsalTrader.json out/MilordForkBase.sol/ArbSysForkStub.json; do
  [ -f "$F" ] || die "artifacts lack $F (old artifacts?)"
done

(exec 3<>"/dev/tcp/127.0.0.1/${FORK_PORT}") 2>/dev/null && die "port ${FORK_PORT} is in use"
# --auto-impersonate: the stand-in Safes send their batches unsigned. Only inside this private fork.
ARGS=(--fork-url "$UPSTREAM" --host 127.0.0.1 --port "$FORK_PORT" --retries 10 --timeout 60000 --silent --auto-impersonate)
[ -n "${FORK_BLOCK:-}" ] && ARGS+=(--fork-block-number "$FORK_BLOCK")
nice -n 10 anvil "${ARGS[@]}" >>"$LOG" 2>&1 &
ANVIL_PID=$!
for _ in $(seq 1 300); do kill -0 "$ANVIL_PID" 2>/dev/null || die "Anvil startup failed"; cast chain-id --rpc-url "$FORK_URL" >/dev/null 2>&1 && break; sleep 0.2; done
case "$(cast client --rpc-url "$FORK_URL")" in anvil*) ;; *) die "not Anvil" ;; esac
cast rpc --rpc-url "$FORK_URL" anvil_nodeInfo | grep -q '"forkBlockNumber"' || die "Anvil is not a fork"
[ "$(cast chain-id --rpc-url "$FORK_URL")" = 4663 ] || die "fork is not chain 4663"
[ "$(cast codesize --rpc-url "$FORK_URL" $LAUNCHER)" -gt 0 ] || die "HookrLauncher has no code on the fork"
[ "$(cast codesize --rpc-url "$FORK_URL" $STATE_VIEW)" -gt 0 ] || die "StateView has no code on the fork"
say "local fork $FORK_URL verified (chain 4663, block $(cast block-number --rpc-url "$FORK_URL"))"

# Anvil does not emulate ArbSys (0x64); Hookr's pending-owner bookkeeping reads it (same stub as the forge fork suite)
cast rpc --rpc-url "$FORK_URL" anvil_setCode 0x0000000000000000000000000000000000000064 \
  "$(node -e 'console.log(require("./out/MilordForkBase.sol/ArbSysForkStub.json").deployedBytecode.object)')" >/dev/null
# stand-in Safes exist only on this fork; 1 ETH each for the Hookr launch fee and gas
for A in $TEAM_SAFE $ARB_SAFE; do
  cast rpc --rpc-url "$FORK_URL" anvil_setCode $A 0x00 >/dev/null
  cast rpc --rpc-url "$FORK_URL" anvil_setBalance $A 0xDE0B6B3A7640000 >/dev/null
done

broadcast_addr() { # <script> <contract name> -> the single CREATE address, after checking every receipt succeeded
  node -e '
    const r = require(process.argv[1]);
    const ts = r.transactions.filter((t) => t.contractName === process.argv[2] && t.transactionType === "CREATE");
    if (ts.length !== 1 || !r.receipts?.length || r.receipts.some((x) => BigInt(x.status) !== 1n)) throw Error("missing or failed receipts");
    console.log(ts[0].contractAddress);' "$FOUNDRY_BROADCAST/$1/4663/run-latest.json" "$2"
}

say "== 1. DeployAtomicLaunch.s.sol"
MILORD_LOCAL_FORK=yes MILORD_TEAM_SAFE=$TEAM_SAFE forge script script/DeployAtomicLaunch.s.sol \
  --rpc-url "$FORK_URL" --broadcast --private-key $ANVIL0_KEY --slow >>"$LOG" 2>&1 || die "DeployAtomicLaunch failed (see $LOG)"
HELPER=$(broadcast_addr DeployAtomicLaunch.s.sol MilordAtomicLaunch) || die "no MilordAtomicLaunch deployment"
PRED=$(cast call --rpc-url "$FORK_URL" "$HELPER" "predictedToken()(address)")
[ "$(cast codesize --rpc-url "$FORK_URL" "$PRED")" = 0 ] || die "predicted token already has code"
say "helper $HELPER, predicted MILORD $PRED"

say "== 2. DeployRobinhood.s.sol (whole protocol against the predicted token)"
MANIFEST=$(mktemp --suffix=.json deployments/.rehearsal-full-XXXXXXXX) || die "cannot create a manifest file in ./deployments"
case "$MANIFEST" in deployments/.rehearsal-full-*.json) ;; *) die "unexpected manifest path $MANIFEST" ;; esac
MILORD_LOCAL_FORK=yes MILORD_OWNER_SAFE=$TEAM_SAFE MILORD_TEAM_SAFE=$TEAM_SAFE MILORD_ARB_SAFE=$ARB_SAFE \
MILORD_SERVICER=$SERVICER MILORD_QUOTE_SIGNER=$QUOTE_SIGNER MILORD_REPORT_SIGNER=$REPORT_SIGNER \
MILORD_OPERATOR=$KEEPER MILORD_PUBLISHER=$PUBLISHER MILORD_ROUTER_PAUSER=$PAUSER \
MILORD_PREDICTED_TOKEN=$PRED MILORD_VAULT_ASSET=$USDG MILORD_FUNDING_POOL=$FUNDING_POOL MILORD_WETH=$WETH \
MILORD_BASE_URI="ipfs://rehearsal/" MILORD_CHARACTERISTICS="a,b,c,d,e,f,g,h" MILORD_VAULT_CAP=1000000000000 \
MILORD_DEPLOYMENTS_OUT="./$MANIFEST" \
  forge script script/DeployRobinhood.s.sol --rpc-url "$FORK_URL" --broadcast --private-key $ANVIL0_KEY --slow >>"$LOG" 2>&1 \
  || die "DeployRobinhood failed (see $LOG)"
[ -s "$MANIFEST" ] || die "DeployRobinhood wrote no deployments file"
mv -- "$MANIFEST" "$TMP/deployments.json"
MANIFEST=""
grep -A1 "configuration hash" "$LOG" | tail -1 | tee -a "$LOG" >/dev/null

flows() { REHEARSAL_RPC="$FORK_URL" REHEARSAL_TMP="$TMP" STATE_VIEW=$STATE_VIEW node fork/rehearse-flows.mjs "$@" 2>&1 | tee -a "$LOG"; }
keep_logs() {
  cp "$TMP/deployments.json" "${LOG%.log}.deployments.json" 2>/dev/null || true
  [ -f "$TMP/summary.json" ] && cp "$TMP/summary.json" "${LOG%.log}.summary.json"
  return 0
}

say "== 3. Safe ownership, atomic launch, record-launch.mjs"
flows launch "$TMP/deployments.json" "$HELPER" || { keep_logs; die "launch phase failed (see $LOG)"; }

say "== 4. HookrPostLaunch.s.sol (buyback route and LP fee collector over the recorded pool)"
dep() { node -e 'const v=require(process.argv[1])[process.argv[2]]; if(v===undefined) throw Error("missing "+process.argv[2]); console.log(v)' "$TMP/deployments.json" "$1"; }
MILORD_LOCAL_FORK=yes MILORD_TOKEN=$(dep MILORD) MILORD_POOL_HOOK=$(dep HookrRoot) MILORD_POOL_FEE=$(dep lpFee) \
MILORD_POOL_TICK_SPACING=$(dep tickSpacing) V4_POOL_MANAGER=$(dep PoolManager) MILORD_FAMILY_ID=$(dep MilordFamilyId) \
MILORD_POOL_ID=$(dep MilordPoolId) MILORD_BUYBACK_BURNER=$(dep BuybackBurner) HOOKR_FAMILY_LOCK=$(dep HookrFamilyLock) \
  forge script script/HookrPostLaunch.s.sol --rpc-url "$FORK_URL" --broadcast --private-key $ANVIL0_KEY --slow >>"$LOG" 2>&1 \
  || { keep_logs; die "HookrPostLaunch failed (see $LOG)"; }
ADAPTER=$(broadcast_addr HookrPostLaunch.s.sol V4SwapAdapter) || die "no V4SwapAdapter deployment"
COLLECTOR=$(broadcast_addr HookrPostLaunch.s.sol HookrLockFeeCollector) || die "no HookrLockFeeCollector deployment"
say "V4SwapAdapter $ADAPTER, HookrLockFeeCollector $COLLECTOR"

say "== 5. accept, lock (fork only), trading, keeper, conversion, report, weekly root, claim"
STATUS=0
flows operate "$TMP/deployments.json" "$HELPER" "$ADAPTER" "$COLLECTOR" || STATUS=$?
keep_logs
[ "$STATUS" = 0 ] || die "integrated rehearsal failed (exit $STATUS, see $LOG)"
say "INTEGRATED REHEARSAL PASSED (private fork only; summary ${LOG%.log}.summary.json)"
if [ "${FORK_KEEP:-no}" = yes ]; then
  # Soak / frontend test against the rehearsed fork (docs/launch/TESTNET_SERVICES.md). Ctrl-C stops Anvil.
  say "fork kept at $FORK_URL; deployments ${LOG%.log}.deployments.json. Services: ALLOW_LOCAL_FORK=yes RPC_URL=$FORK_URL"
  wait "$ANVIL_PID" || true
fi

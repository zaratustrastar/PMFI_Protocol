#!/usr/bin/env bash
# Robinhood Chain (4663) mainnet-fork integration suite: MILORD launch, Anti-Snipe, lock and buyback against the
# DEPLOYED Hookr-1 contracts, on a private local Anvil fork. Safe to run on the VPS:
#   - no private key is read or used, no transaction is signed; `forge test` executes everything in its own in-memory
#     EVM and only READS state from the local Anvil (which reads mainnet state over the public RPC);
#   - the fork URL must be http://127.0.0.1 or http://localhost, answer as Anvil, and report an active fork
#     (chain id 4663 alone is not accepted, because a fork keeps the mainnet chain id);
#   - production artifacts come from CI (no via-IR build here); solc is replaced by a stub that refuses to compile;
#   - its own Anvil binds 127.0.0.1 on a port that must be free, runs at low CPU/IO priority, and only that process
#     is stopped on exit. Nothing else on the machine is touched.
#
# Usage (from milord/):  ./fork/run-fork-tests.sh
# Env (all optional):
#   FORK_URL=http://127.0.0.1:8547   use an Anvil fork that is already running instead of starting one
#   FORK_PORT=8548                   port for the runner's own Anvil (default 8548; must be free)
#   ROBINHOOD_RPC=<url>              upstream for the runner's own Anvil (default: public Robinhood RPC)
#   FORK_BLOCK=<n>                   pin the fork block (default: latest)
#   FORK_BUILD=ci|lowmem             ci (default): CI production artifacts for this commit, downloaded with gh if
#                                    missing; lowmem: compile the non-IR lowmem profile instead (~0.7 GB)
#   MATCH=<regex>                    forge --match-test filter
#   SNIPE_PIPS, SNIPE_SECONDS, GUARD_BLOCKS, LOCK_MARGIN_BLOCKS   Anti-Snipe exploration knobs (tests' defaults:
#                                    400000, 3600, 300, 7200)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f env.sh ] && source ./env.sh

# Never let a key reach forge or cast through the environment.
unset PRIVATE_KEY ETH_PRIVATE_KEY DEPLOYER_PRIVATE_KEY ETH_KEYSTORE ETH_PASSWORD ETH_FROM MNEMONIC 2>/dev/null || true

REPO=zaratustrastar/PMFI_Protocol
UPSTREAM="${ROBINHOOD_RPC:-https://rpc.mainnet.chain.robinhood.com}"
FORK_PORT="${FORK_PORT:-8548}"
FORK_BUILD="${FORK_BUILD:-ci}"
LAUNCHER=0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4
ROOT=0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc
LOCK=0xfAFEcE64099C1E791C4B46189c277f00c0c7e00b
RULES_A=0x4a71c270b1520F94064268102AebD744b0DE7942
RULES_B=0x10f684361b35C8411a2c07F68A6fB1F4B7e555F7
TAX_A=0xE73fAD0d773b9571a7a9d2A1edc01132fF5f1e1F
TAX_B=0x99012422e1acBdf53C42BB378eb2f4e468005d3c
TREASURY=0x2242F7FC75606500503075f29ea8cDA684EfB4a9
mkdir -p fork/logs
LOG="fork/logs/fork-$(date -u +%Y%m%dT%H%M%SZ).log"
ANVIL_PID=""

die() { echo "REFUSED: $*" | tee -a "$LOG" >&2; exit 1; }
say() { echo "$*" | tee -a "$LOG"; }
# Low CPU and IO priority, by exec (no wrapper shell), so $! is the real process id.
LOWPRIO=(nice -n 10)
command -v ionice >/dev/null && LOWPRIO=(ionice -c3 nice -n 10)

stop_anvil() {
  if [ -n "$ANVIL_PID" ] && kill -0 "$ANVIL_PID" 2>/dev/null; then
    kill "$ANVIL_PID" 2>/dev/null || true
    wait "$ANVIL_PID" 2>/dev/null || true
  fi
  ANVIL_PID=""
}

# ---------------------------------------------------------------------------------------------- 1. build artifacts
use_prebuilt() {
  [ -d out ] && [ -f cache/solidity-files-cache.json ] || return 1
  export FOUNDRY_SOLC="$PWD/sim/no-compile-solc.sh" FOUNDRY_OFFLINE=true
  unset FOUNDRY_PROFILE
}

if [ "$FORK_BUILD" = "lowmem" ]; then
  export FOUNDRY_PROFILE=lowmem
  say "build: lowmem profile (non-IR, ~0.7 GB peak), out-lowmem/"
else
  HEAD_SHA=$(git rev-parse HEAD)
  if [ "$(cat out/.ci-commit 2>/dev/null)" != "$HEAD_SHA" ]; then
    command -v gh >/dev/null || die "no CI artifacts for $HEAD_SHA in out/ and gh is not installed. Download milord-production-out for this commit (docs/VPS_DEVELOPMENT.md) or use FORK_BUILD=lowmem"
    git diff --quiet HEAD -- . || die "uncommitted changes under milord/: CI artifacts would not match (use FORK_BUILD=lowmem)"
    RUN=$(gh run list -R "$REPO" --workflow milord-ci --commit "$HEAD_SHA" --status success --json databaseId --jq '.[0].databaseId')
    [ -n "$RUN" ] || die "no successful milord-ci run for $HEAD_SHA yet (wait for CI, or FORK_BUILD=lowmem)"
    say "downloading CI production artifacts (run $RUN) for $HEAD_SHA"
    rm -rf out cache
    gh run download "$RUN" -R "$REPO" -n milord-production-out -D .
    echo "$HEAD_SHA" > out/.ci-commit
  fi
  use_prebuilt || die "out/ or cache/ missing after download"
  say "build: CI production artifacts for $HEAD_SHA (no compilation; solc replaced by a refusing stub)"
fi

# ---------------------------------------------------------------------------------------------- 2. local Anvil fork
if [ -z "${FORK_URL:-}" ]; then
  if (exec 3<>"/dev/tcp/127.0.0.1/${FORK_PORT}") 2>/dev/null; then
    die "port ${FORK_PORT} on 127.0.0.1 is in use (set FORK_PORT to a free port, or FORK_URL to reuse a local fork)"
  fi
  FORK_URL="http://127.0.0.1:${FORK_PORT}"
  trap stop_anvil EXIT
  trap 'stop_anvil; exit 130' INT TERM
  ARGS=(--fork-url "$UPSTREAM" --host 127.0.0.1 --port "$FORK_PORT" --retries 10 --timeout 60000 --fork-retry-backoff 1000 --silent)
  [ -n "${FORK_BLOCK:-}" ] && ARGS+=(--fork-block-number "$FORK_BLOCK")
  say "starting a private Anvil fork of Robinhood Chain on $FORK_URL"
  "${LOWPRIO[@]}" anvil "${ARGS[@]}" >>"$LOG" 2>&1 &
  ANVIL_PID=$!
  for _ in $(seq 1 300); do
    kill -0 "$ANVIL_PID" 2>/dev/null || die "anvil exited during startup (see $LOG)"
    cast chain-id --rpc-url "$FORK_URL" >/dev/null 2>&1 && break
    sleep 0.2
  done
fi

# ---------------------------------------------------------------------------------------------- 3. prove it is local
case "$FORK_URL" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) die "FORK_URL must be http://127.0.0.1:<port> or http://localhost:<port>, got $FORK_URL" ;;
esac
CLIENT=$(cast client --rpc-url "$FORK_URL" 2>/dev/null || true)
case "$CLIENT" in anvil*) ;; *) die "$FORK_URL is not Anvil (web3_clientVersion: '$CLIENT')" ;; esac
INFO=$(cast rpc --rpc-url "$FORK_URL" anvil_nodeInfo 2>/dev/null || true)
FORK_FROM=$(printf '%s' "$INFO" | grep -o '"forkUrl": *"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/' || true)
FORK_AT=$(printf '%s' "$INFO" | grep -o '"forkBlockNumber": *[0-9]*' | head -1 | grep -o '[0-9]*$' || true)
[ -n "$FORK_AT" ] || die "$FORK_URL is Anvil but not a fork (anvil_nodeInfo has no forkBlockNumber)"
CHAIN=$(cast chain-id --rpc-url "$FORK_URL")
[ "$CHAIN" = "4663" ] || die "fork chain id is $CHAIN, expected 4663 (Robinhood Chain)"
SIZE=$(cast codesize --rpc-url "$FORK_URL" "$LAUNCHER")
[ "$SIZE" -gt 0 ] || die "HookrLauncher has no code on this fork"
say "verified: $FORK_URL is a local Anvil ($CLIENT), forked at block $FORK_AT from ${FORK_FROM:-<hidden>}, chain $CHAIN"

# ---------------------------------------------------------------------------------------------- 4. read-only preflight
c() { cast call --rpc-url "$FORK_URL" "$@" 2>&1 | tr '\n' ' '; }
say "== Hookr-1 on the fork (read-only)"
for A in "HookrLauncher $LAUNCHER" "HookrRoot $ROOT" "HookrFamilyLock $LOCK" "HookrRules-A $RULES_A" "HookrRules-B $RULES_B" \
         "Tax+Conversion-A $TAX_A" "Tax+Conversion-B $TAX_B" "Treasury $TREASURY"; do
  set -- $A; say "  $1 $2 code $(cast codesize --rpc-url "$FORK_URL" "$2") bytes"
done
REG=$(cast call --rpc-url "$FORK_URL" $LAUNCHER "registry()(address)" 2>/dev/null || echo "<registry() failed>")
say "  registry $REG  root open: $(c $REG 'rootOpen(address)(bool)' $ROOT) ETH is a quote: $(c $REG 'isQuote(address)(bool)' 0x0000000000000000000000000000000000000000)"
for M in $RULES_A $RULES_B $TAX_A $TAX_B; do
  say "  admission(root, $M): $(c $REG 'admission(address,address)((uint8,address,bytes32,bytes32,(uint24,uint24,uint16),uint32,uint8,bool,bool))' $ROOT $M)"
done
for R in $RULES_A $RULES_B; do say "  $R minProtocolShareBps $(c $R 'minProtocolShareBps()(uint16)') protocolRecipient $(c $R 'protocolRecipient()(address)')"; done
for T in $TAX_A $TAX_B; do say "  $T tax minProtocolShareBps $(c $T 'minProtocolShareBps()(uint16)')"; done
say "  treasury feeTerms (governed floor, integrator rate, minimum): $(c $TREASURY 'feeTerms(address,bool,bytes32)(uint16,uint16,uint16)' 0x0000000000000000000000000000000000000000 false 0x0000000000000000000000000000000000000000000000000000000000000000)"
say "  launchFee (fee, treasury, pending, pendingTreasury, readyAt): $(c $LAUNCHER 'launchFee()(uint256,address,uint256,address,uint256)')"

# ---------------------------------------------------------------------------------------------- 5. the suite
FORGE=(forge test --match-path 'test/fork/*' --fork-url "$FORK_URL" --threads "${FORGE_THREADS:-2}" -vv)
[ -n "${MATCH:-}" ] && FORGE+=(--match-test "$MATCH")
say "== forge ${FORGE[*]:1}"
hwm_mb() { echo $(( $(grep -o 'VmHWM:[[:space:]]*[0-9]*' "/proc/$1/status" 2>/dev/null | grep -o '[0-9]*$' || echo 0) / 1024 )); }
SECONDS=0
set +e
"${LOWPRIO[@]}" "${FORGE[@]}" > >(tee -a "$LOG") 2>&1 &
FORGE_PID=$!
FORGE_PEAK=0; ANVIL_PEAK=0
while kill -0 "$FORGE_PID" 2>/dev/null; do
  m=$(hwm_mb "$FORGE_PID"); [ "$m" -gt "$FORGE_PEAK" ] && FORGE_PEAK=$m
  if [ -n "$ANVIL_PID" ]; then m=$(hwm_mb "$ANVIL_PID"); [ "$m" -gt "$ANVIL_PEAK" ] && ANVIL_PEAK=$m; fi
  sleep 0.5
done
wait "$FORGE_PID"
STATUS=$?
set -e
sleep 0.5
say "resources: forge peak ${FORGE_PEAK} MB, runner's anvil peak ${ANVIL_PEAK} MB (0 = reused FORK_URL), wall ${SECONDS} s"
say "log: $LOG"
exit $STATUS

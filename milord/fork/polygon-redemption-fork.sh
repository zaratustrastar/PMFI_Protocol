#!/usr/bin/env bash
# B21: Polymarket pUSD redemption (standard CTF, neg-risk CTF, Protocol V2) against the DEPLOYED Polygon contracts,
# on a private local Anvil fork of Polygon. Safe to run on the VPS:
#   - no private key is read; the test holder is a fresh impersonated address that exists only on the fork;
#   - every transaction goes to the fork's own RPC, which must be http://127.0.0.1 or http://localhost, answer as
#     Anvil and report an active fork (chain id 137 alone is not accepted, because a fork keeps the mainnet id);
#   - its own Anvil binds 127.0.0.1 on a port that must be free, runs at low CPU/IO priority, and only that process
#     is stopped on exit. The upstream RPC is only read (by Anvil). No Polymarket API, relayer or Safe is used.
#   - the transport under test is trading_bot/ctf_redemption.mjs from this commit, copied into a temporary
#     directory under fork/ so it uses milord/node_modules; the copy is removed on exit.
#
# Usage (from milord/):  ./fork/polygon-redemption-fork.sh
# Env (all optional):
#   POLYGON_FORK_RPC=<url>   upstream Polygon RPC for Anvil (default https://polygon-bor-rpc.publicnode.com)
#   FORK_PORT=8551           port for the runner's own Anvil (must be free)
#   FORK_BLOCK=<n>           pin the fork block (default: latest)
set -euo pipefail
cd "$(dirname "$0")/.."
unset PRIVATE_KEY ETH_PRIVATE_KEY DEPLOYER_PRIVATE_KEY MILORD_POLY_PRIVATE_KEY POLY_PRIVATE_KEY ETH_KEYSTORE ETH_PASSWORD ETH_FROM MNEMONIC 2>/dev/null || true

UPSTREAM="${POLYGON_FORK_RPC:-https://polygon-bor-rpc.publicnode.com}"
FORK_PORT="${FORK_PORT:-8551}"
FORK_URL="http://127.0.0.1:${FORK_PORT}"
CTF=0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
ROUTER=0x12121212006e4CD160D18e3f00711DA5c3372600
mkdir -p fork/logs
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG="fork/logs/polygon-redemption-$STAMP.log"
ANVIL_PID=""
TMP=""

die() { echo "REFUSED: $*" | tee -a "$LOG" >&2; exit 1; }
say() { echo "$*" | tee -a "$LOG"; }
LOWPRIO=(nice -n 10)
command -v ionice >/dev/null && LOWPRIO=(ionice -c3 nice -n 10)
cleanup() {
  if [ -n "$ANVIL_PID" ] && kill -0 "$ANVIL_PID" 2>/dev/null; then kill "$ANVIL_PID" 2>/dev/null || true; wait "$ANVIL_PID" 2>/dev/null || true; fi
  [ -n "$TMP" ] && rm -rf -- "$TMP"
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

command -v anvil >/dev/null && command -v cast >/dev/null || die "anvil and cast (Foundry) are required"
[ -d node_modules/ethers ] || die "milord/node_modules is missing; run npm ci in milord/ first"
if (exec 3<>"/dev/tcp/127.0.0.1/${FORK_PORT}") 2>/dev/null; then die "port ${FORK_PORT} on 127.0.0.1 is in use (set FORK_PORT)"; fi

ARGS=(--fork-url "$UPSTREAM" --host 127.0.0.1 --port "$FORK_PORT" --retries 10 --timeout 60000 --fork-retry-backoff 1000 --silent)
[ -n "${FORK_BLOCK:-}" ] && ARGS+=(--fork-block-number "$FORK_BLOCK")
say "starting a private Anvil fork of Polygon on $FORK_URL"
"${LOWPRIO[@]}" anvil "${ARGS[@]}" >>"$LOG" 2>&1 &
ANVIL_PID=$!
for _ in $(seq 1 300); do
  kill -0 "$ANVIL_PID" 2>/dev/null || die "anvil exited during startup (see $LOG)"
  cast chain-id --rpc-url "$FORK_URL" >/dev/null 2>&1 && break
  sleep 0.2
done

CLIENT=$(cast client --rpc-url "$FORK_URL" 2>/dev/null || true)
case "$CLIENT" in anvil*) ;; *) die "$FORK_URL is not Anvil (web3_clientVersion: '$CLIENT')" ;; esac
INFO=$(cast rpc --rpc-url "$FORK_URL" anvil_nodeInfo 2>/dev/null || true)
FORK_AT=$(printf '%s' "$INFO" | grep -o '"forkBlockNumber": *[0-9]*' | head -1 | grep -o '[0-9]*$' || true)
[ -n "$FORK_AT" ] || die "$FORK_URL is Anvil but not a fork"
CHAIN=$(cast chain-id --rpc-url "$FORK_URL")
[ "$CHAIN" = "137" ] || die "fork chain id is $CHAIN, expected 137 (Polygon)"
[ "$(cast codesize --rpc-url "$FORK_URL" "$CTF")" -gt 0 ] && [ "$(cast codesize --rpc-url "$FORK_URL" "$ROUTER")" -gt 0 ] || die "Polymarket contracts have no code on this fork"
say "verified: $FORK_URL is a local Anvil ($CLIENT), Polygon fork at block $FORK_AT"

TMP=$(mktemp -d fork/.polygon-redemption-XXXXXXXX)
cp ../trading_bot/ctf_redemption.mjs "$TMP/ctf_redemption.mjs"
say "transport: trading_bot/ctf_redemption.mjs at $(git rev-parse --short HEAD)$(git diff --quiet HEAD -- ../trading_bot/ctf_redemption.mjs || echo ' (uncommitted changes)')"
set +e
FORK_URL="$FORK_URL" CTF_TRANSPORT="$PWD/$TMP/ctf_redemption.mjs" "${LOWPRIO[@]}" node fork/polygon-redemption.mjs 2>&1 | tee -a "$LOG"
STATUS=${PIPESTATUS[0]}
set -e
tail -1 "$LOG" > "fork/logs/polygon-redemption-$STAMP.summary.json"
[ "$STATUS" = 0 ] && say "POLYGON REDEMPTION FORK TEST PASSED (log $LOG)" || say "POLYGON REDEMPTION FORK TEST FAILED (log $LOG)"
exit "$STATUS"

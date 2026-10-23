# Sourced by the sim runners. Starts a private anvil on 127.0.0.1 and stops only that process.
# ANVIL_PORT (default 8545) must be free: the runner refuses to start otherwise, and never kills
# any process it did not start.
# MILORD_PREBUILT=1: use the production artifacts already in out/ and cache/ (e.g. downloaded from CI)
# and refuse to compile anything (see sim/no-compile-solc.sh).
ANVIL_PORT="${ANVIL_PORT:-8545}"
RPC="http://127.0.0.1:${ANVIL_PORT}"
export RPC RPC_URL="$RPC"
ANVIL_PID=""

if [ "${MILORD_PREBUILT:-0}" = "1" ]; then
  [ -d out ] && [ -f cache/solidity-files-cache.json ] || { echo "MILORD_PREBUILT=1 but out/ or cache/ is missing" >&2; exit 1; }
  export FOUNDRY_SOLC="$PWD/sim/no-compile-solc.sh" FOUNDRY_OFFLINE=true
  unset FOUNDRY_PROFILE
fi

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/${ANVIL_PORT}") 2>/dev/null; }

stop_anvil() {
  if [ -n "$ANVIL_PID" ] && kill -0 "$ANVIL_PID" 2>/dev/null; then
    kill "$ANVIL_PID" 2>/dev/null || true
    wait "$ANVIL_PID" 2>/dev/null || true
  fi
  ANVIL_PID=""
}

start_anvil() {
  if port_busy; then
    echo "port ${ANVIL_PORT} on 127.0.0.1 is already in use; refusing to start (set ANVIL_PORT to a free port)" >&2
    exit 1
  fi
  trap stop_anvil EXIT
  trap 'stop_anvil; exit 130' INT TERM
  anvil --host 127.0.0.1 --port "$ANVIL_PORT" --silent --block-base-fee-per-gas 0 --code-size-limit 50000 >/dev/null 2>&1 &
  ANVIL_PID=$!
  for _ in $(seq 1 100); do
    kill -0 "$ANVIL_PID" 2>/dev/null || { echo "anvil exited during startup (port ${ANVIL_PORT})" >&2; exit 1; }
    cast chain-id --rpc-url "$RPC" >/dev/null 2>&1 && return 0
    sleep 0.2
  done
  echo "anvil did not answer on ${RPC}" >&2
  exit 1
}

deploy_local() {
  cast rpc --rpc-url "$RPC" anvil_setCode 0x000000000022D473030F116dDEE9F6B43aC78BA3 "$(cat sim/permit2.runtime.hex)" >/dev/null
  forge script script/DeployLocal.s.sol --rpc-url "$RPC" --broadcast --slow >/dev/null
}

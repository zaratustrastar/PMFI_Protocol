#!/usr/bin/env bash
# Hookr-1 bytecode verification for the contracts MILORD launches through on Robinhood Chain (4663).
#
# Proves that the code MILORD will call is the published Hookr source, compiled with the expected settings:
#   offline (always):  the deployment record's compiler settings for every selected contract equal the expected ones
#                      (solc 0.8.37, via-IR, optimizer 200 runs, EVM prague); the Hookr checkout is the pinned commit
#                      with clean, pinned submodules; every source file hashes to the record's sourceKeccak256; the
#                      repo's SOURCE_MANIFEST.json check passes; each contract's standard-json input compiles with the
#                      official solc 0.8.37 to bytecode whose embedded metadata IPFS hash equals the record's, and, for
#                      contracts without immutables, whose keccak256 equals the record's runtimeKeccak256 (the keccak of
#                      the code on chain, see docs/launch/HOOKR_BYTECODE_VERIFICATION.md);
#   on chain (RPC_URL): chain id is 4663, and for every address the code on chain equals the compiled code byte for
#                      byte, with only the immutables taken from the chain (as hookr-1/check-runtime.mjs does), and
#                      hashes to the record's runtimeKeccak256.
# Safe to run on the VPS:
#   - READ-ONLY: the only RPC calls are eth_chainId and eth_getCode (via `cast` or curl); no key is read, nothing is
#     signed or sent;
#   - nothing from the Hookr checkout is executed: its .mjs logic is ported into the helper this script writes to its
#     own work dir, and git runs with hooks and fsmonitor disabled;
#   - solc runs one contract at a time at the lowest CPU/IO priority (nice 19, ionice idle), so live services keep
#     the machine; compiler outputs are cached by input hash, so a re-run only compiles what changed.
#
# Usage (from milord/):  ./hookr-verify/verify-hookr.sh                  # offline phase only
#                        RPC_URL=https://... ./hookr-verify/verify-hookr.sh   # offline + on-chain phase
# Env (all optional except RPC_URL for the on-chain phase):
#   RPC_URL=<url>        Robinhood Chain RPC. Without it the on-chain phase is reported as "not run". Never defaulted.
#   HOOKR_SRC=<path>     hookr-contracts checkout to use (must be at the pinned commit; missing submodules are
#                        initialized). Default: a fresh clone of the pinned commit in a temp dir, removed on exit.
#   SOLC=<path>          solc 0.8.37 binary. Default: the official solc-static-linux v0.8.37 from GitHub releases,
#                        cached under ${XDG_CACHE_HOME:-~/.cache}/milord-hookr-verify, sha256-checked against the
#                        solc-bin list.json.
#   SOLC_SHA256=<hex>    expected sha256 of the downloaded solc when list.json cannot be fetched (if both are
#                        available they must agree).
#   ONLY=<addr,addr>     verify only these selected addresses (debugging).
#   NO_CACHE=1           ignore cached compiler outputs.
# Output: hookr-verify/out/report.md and report.json (+ work/ and cache/). Exit status is non-zero on any mismatch.
set -euo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$SELF")/.."

# Never let a key reach cast through the environment (this script never needs one).
unset PRIVATE_KEY ETH_PRIVATE_KEY DEPLOYER_PRIVATE_KEY ETH_KEYSTORE ETH_PASSWORD ETH_FROM MNEMONIC 2>/dev/null || true

# ---------------------------------------------------------------------------------------------- 0. what we expect
HOOKR_REPO=https://github.com/cholakovvv/hookr-contracts.git
HOOKR_PIN=7e771b3b244c812c699abe3e74d166289709ff0f
RECORD=hookr-1/deployments/robinhood-4663.json
CHAIN_ID=4663
SOLC_VERSION=0.8.37
SOLC_LONG=0.8.37+commit.f401782d
SOLC_URL="https://github.com/ethereum/solidity/releases/download/v${SOLC_VERSION}/solc-static-linux"
# The canonical checksum list is binaries.soliditylang.org; the second URL is the same list in its source repository
# (ethereum/solc-bin), used only when the first host is unreachable.
SOLC_LISTS=(https://binaries.soliditylang.org/linux-amd64/list.json
            https://raw.githubusercontent.com/ethereum/solc-bin/gh-pages/linux-amd64/list.json)
EXPECT_VIA_IR=true
EXPECT_OPTIMIZER_RUNS=200
EXPECT_EVM=prague

# The contracts MILORD depends on, as "address name why". The helper refuses if the record names a different contract
# at an address, or if a selected contract links a library that is not selected.
SELECTION=(
  "0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4 HookrLauncher         launches the MILORD family; launchFee(); transferFamily"
  "0x8567369a70100828611BB6F44ec051B48F10fb59 HookrLaunchChecks     library linked by HookrLauncher: launch checks and the launch fee"
  "0x1B23D6d7beBcB61E39317E9F0Bc02AEB91aFCcAe HookrTokenDeployer    library linked by HookrLauncher"
  "0xfAFEcE64099C1E791C4B46189c277f00c0c7e00b HookrFamilyLock       permanent lock of the MILORD family; pays LP fees to HookrLockFeeCollector"
  "0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc HookrRoot             the v4 hook of the MILORD pool"
  "0x9A8a1006469BA196f8FEfc017a53505C83A060BB HookrLane             module HookrRoot DELEGATECALLs (runs in the root's storage)"
  "0x4a71c270b1520F94064268102AebD744b0DE7942 HookrRules            Rules candidate A (release)"
  "0xF83569E564d70daebc35950f66a73f2e2B915583 HookrRecapture        module Rules A DELEGATECALLs"
  "0x10f684361b35C8411a2c07F68A6fB1F4B7e555F7 HookrRules            Rules candidate B (King of the Pool whole pot)"
  "0x60Efc4a41498e9433be2155b6cc1677AEC31D649 HookrRecapture        module Rules B DELEGATECALLs"
  "0x463c82beA1a52F4Cc05ca0741a24F0AD24bA4602 HookrRegistry         registry: admissions MILORD's launch checks (launcher.registry())"
  "0x675ac2102E8c69E2467F64656E3c235AF3901Ac1 HookrRegistryAdmin    library linked by HookrRegistry"
  "0xB9F5c54E5Cc7ac6c7a96335E5A334Cf4eA496E98 HookrRegistryChecks   library linked by HookrRegistry and HookrRegistryAdmin"
  "0x2242F7FC75606500503075f29ea8cDA684EfB4a9 HookrTreasury         protocol fee terms and protocol-share destination"
  "0xE73fAD0d773b9571a7a9d2A1edc01132fF5f1e1F HookrDirectionalTax   Tax + Conversion candidate A: the 1.5% buy/sell tax"
  "0x2C2Fd39520165e5Ee5a9DD69297fe0BE02F29619 HookrTaxQueue         queue implementation every ERC-1167 tax-queue clone of tax A delegates to"
  "0x0Eb5076E91803194281Dcfd94aDF453d5D1575B0 HookrFeeSwapExecutor  executor wired into tax A and its queues (constructor argument)"
  "0xC9B5Ded0c6f1eeFFf2a41607d6EE4e71be88763A HookrFeeRouteRegistry route registry of tax A (executor.routeRegistry())"
  "0x78Bc6b7302Dda8632e661c8d74966bdBea1c66b1 HookrSessionAdvisory  session calendar fixed in both tax candidates' constructors"
  "0x99012422e1acBdf53C42BB378eb2f4e468005d3c HookrDirectionalTax   tax candidate B (builder-attribution partner tax, no queues)"
  "0x48FE32A6a1b1507D6981A51f907d53595C16e862 HookrPartnerRegistry  partner registry fixed in tax B's constructor"
  "0xC2Bc9a3fe6c4Ce8C2F7EB4655b0869b01D8c93C9 HookrRouter           Hookr's authenticated swap router (HOOKR_FINAL_CONFIG)"
  "0x445D7891381Aa7F91F5048EdBC6a611ccBDe7865 HookrQuoter           Hookr's quoter (HOOKR_FINAL_CONFIG)"
)

# On-chain wiring: the immutables the byte comparison takes from the chain, read back through their getters (eth_call)
# and compared with the constructor arguments the record lists, as "address signature expected label". Expected is an
# address or a decimal number.
REG=0x463c82beA1a52F4Cc05ca0741a24F0AD24bA4602
POOL_MANAGER=0x8366a39CC670B4001A1121B8F6A443A643e40951
WIRING=(
  "0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4 registry()             $REG HookrLauncher"
  "0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4 poolManager()          $POOL_MANAGER HookrLauncher"
  "0xfAFEcE64099C1E791C4B46189c277f00c0c7e00b launcher()             0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4 HookrFamilyLock"
  "0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc registry()             $REG HookrRoot"
  "0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc poolManager()          $POOL_MANAGER HookrRoot"
  "0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc router()               0xC2Bc9a3fe6c4Ce8C2F7EB4655b0869b01D8c93C9 HookrRoot"
  "0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc quoter()               0x445D7891381Aa7F91F5048EdBC6a611ccBDe7865 HookrRoot"
  "0x4a71c270b1520F94064268102AebD744b0DE7942 registry()             $REG HookrRules-A"
  "0x4a71c270b1520F94064268102AebD744b0DE7942 protocolRecipient()    0x2242F7FC75606500503075f29ea8cDA684EfB4a9 HookrRules-A"
  "0x4a71c270b1520F94064268102AebD744b0DE7942 minProtocolShareBps()  2000 HookrRules-A"
  "0x10f684361b35C8411a2c07F68A6fB1F4B7e555F7 registry()             $REG HookrRules-B"
  "0x10f684361b35C8411a2c07F68A6fB1F4B7e555F7 protocolRecipient()    0x2242F7FC75606500503075f29ea8cDA684EfB4a9 HookrRules-B"
  "0x10f684361b35C8411a2c07F68A6fB1F4B7e555F7 minProtocolShareBps()  2000 HookrRules-B"
  "0xE73fAD0d773b9571a7a9d2A1edc01132fF5f1e1F registry()             $REG HookrDirectionalTax-A"
  "0xE73fAD0d773b9571a7a9d2A1edc01132fF5f1e1F routeRegistry()        0xC9B5Ded0c6f1eeFFf2a41607d6EE4e71be88763A HookrDirectionalTax-A"
  "0xE73fAD0d773b9571a7a9d2A1edc01132fF5f1e1F queueImplementation()  0x2C2Fd39520165e5Ee5a9DD69297fe0BE02F29619 HookrDirectionalTax-A"
  "0xE73fAD0d773b9571a7a9d2A1edc01132fF5f1e1F minProtocolShareBps()  2000 HookrDirectionalTax-A"
  "0xE73fAD0d773b9571a7a9d2A1edc01132fF5f1e1F calendar()             0x78Bc6b7302Dda8632e661c8d74966bdBea1c66b1 HookrDirectionalTax-A"
  "0x2C2Fd39520165e5Ee5a9DD69297fe0BE02F29619 executor()             0x0Eb5076E91803194281Dcfd94aDF453d5D1575B0 HookrTaxQueue"
  "0x2C2Fd39520165e5Ee5a9DD69297fe0BE02F29619 routeRegistry()        0xC9B5Ded0c6f1eeFFf2a41607d6EE4e71be88763A HookrTaxQueue"
  "0x99012422e1acBdf53C42BB378eb2f4e468005d3c root()                 0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc HookrDirectionalTax-B"
  "0x99012422e1acBdf53C42BB378eb2f4e468005d3c partnerRegistry()      0x48FE32A6a1b1507D6981A51f907d53595C16e862 HookrDirectionalTax-B"
  "0x99012422e1acBdf53C42BB378eb2f4e468005d3c calendar()             0x78Bc6b7302Dda8632e661c8d74966bdBea1c66b1 HookrDirectionalTax-B"
)
# Owner-controlled state worth seeing next to the result (reported, not judged): "address signature label".
INFO_READS=(
  "$REG owner() HookrRegistry"
  "$REG guardian() HookrRegistry"
  "0x2242F7FC75606500503075f29ea8cDA684EfB4a9 owner() HookrTreasury"
  "0x2242F7FC75606500503075f29ea8cDA684EfB4a9 target() HookrTreasury"
  "0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4 launchFee() HookrLauncher(first-word=fee-wei)"
  "0xC9B5Ded0c6f1eeFFf2a41607d6EE4e71be88763A owner() HookrFeeRouteRegistry"
  "0x48FE32A6a1b1507D6981A51f907d53595C16e862 owner() HookrPartnerRegistry"
)

OUT="$PWD/hookr-verify/out"
WORK="$OUT/work"
CACHE="$OUT/cache"
mkdir -p "$OUT"
rm -rf "$WORK"            # our own scratch dir only (exact path, inside hookr-verify/out)
mkdir -p "$WORK/inputs" "$WORK/chain" "$CACHE"
LOG="$OUT/verify-$(date -u +%Y%m%dT%H%M%SZ).log"
TMP_SRC=""

die() { echo "REFUSED: $*" | tee -a "$LOG" >&2; exit 1; }
say() { echo "$*" | tee -a "$LOG"; }
cleanup() { if [ -n "$TMP_SRC" ] && [ -d "$TMP_SRC" ]; then rm -rf "$TMP_SRC"; fi; }
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

# Lowest CPU and IO priority, by exec (no wrapper shell), so $! is the real solc process id.
LOWPRIO=(nice -n 19)
command -v ionice >/dev/null && LOWPRIO=(nice -n 19 ionice -c3)
# git on a checkout we do not trust: no hooks, no fsmonitor, no file:// submodule transport.
GIT=(git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.file.allow=never)

command -v node >/dev/null || die "node is required (any version >= 18)"
command -v git >/dev/null || die "git is required"
command -v curl >/dev/null || die "curl is required"
command -v sha256sum >/dev/null || die "sha256sum is required"

# Our helper (ported logic of hookr-1/standard-input.mjs, check-runtime.mjs and check-source-manifest.mjs, plus
# keccak256 and the reports) is written from this script into our work dir; it only imports node: builtins.
HELPER="$WORK/hookr-verify-helper.mjs"
sed -n '/^#==HELPER-BEGIN==$/,/^#==HELPER-END==$/p' "$SELF" | sed '1d;$d' > "$HELPER"
h() { node "$HELPER" "$@"; }

# ---------------------------------------------------------------------------------------------- 1. Hookr source
need_submodule() {   # <repo> <path>: initialize a submodule if it is not checked out yet
  local repo=$1 sub=$2
  if [ -z "$(ls -A "$repo/$sub" 2>/dev/null)" ]; then
    say "  initializing submodule $sub"
    "${GIT[@]}" -C "$repo" submodule update --init --depth 1 -- "$sub" >>"$LOG" 2>&1 \
      || "${GIT[@]}" -C "$repo" submodule update --init -- "$sub" >>"$LOG" 2>&1 \
      || die "cannot initialize submodule $sub in $repo"
  fi
}
check_pinned() {     # <parent repo> <submodule path>: checked-out commit equals the gitlink, and no tracked edits
  local repo=$1 sub=$2 want got
  want=$("${GIT[@]}" -C "$repo" ls-tree HEAD -- "$sub" | awk '{print $3}')
  got=$("${GIT[@]}" -C "$repo/$sub" rev-parse HEAD)
  [ -n "$want" ] && [ "$want" = "$got" ] || die "$sub is at $got, the pinned tree records ${want:-nothing}"
  [ -z "$("${GIT[@]}" -C "$repo/$sub" status --porcelain --untracked-files=no)" ] || die "$sub has local modifications"
  say "  $sub at $got (pinned)"
}

say "== Hookr source"
if [ -n "${HOOKR_SRC:-}" ]; then
  SRC=$(cd "$HOOKR_SRC" && pwd)
  say "  using HOOKR_SRC=$SRC"
else
  TMP_SRC=$(mktemp -d "${TMPDIR:-/tmp}/hookr-src.XXXXXX")
  SRC="$TMP_SRC/hookr-contracts"
  say "  cloning $HOOKR_REPO at $HOOKR_PIN into $SRC"
  mkdir -p "$SRC"
  "${GIT[@]}" -C "$SRC" init -q
  "${GIT[@]}" -C "$SRC" remote add origin "$HOOKR_REPO"
  "${GIT[@]}" -C "$SRC" fetch -q --depth 1 origin "$HOOKR_PIN" >>"$LOG" 2>&1 \
    || "${GIT[@]}" -C "$SRC" fetch -q origin >>"$LOG" 2>&1 || die "cannot fetch $HOOKR_REPO"
  "${GIT[@]}" -C "$SRC" -c advice.detachedHead=false checkout -q "$HOOKR_PIN" || die "commit $HOOKR_PIN not found"
fi
HEAD_SHA=$("${GIT[@]}" -C "$SRC" rev-parse HEAD)
[ "$HEAD_SHA" = "$HOOKR_PIN" ] || die "Hookr checkout is at $HEAD_SHA, expected the pin $HOOKR_PIN"
[ -z "$("${GIT[@]}" -C "$SRC" status --porcelain --untracked-files=no --ignore-submodules=all)" ] \
  || die "Hookr checkout has local modifications to tracked files"
say "  hookr-contracts at $HEAD_SHA (pinned, no local modifications)"
# The record's sources only come from hookr-1/ and lib/v4-core (with its OpenZeppelin and solmate).
need_submodule "$SRC" lib/v4-core
check_pinned "$SRC" lib/v4-core
for s in lib/openzeppelin-contracts lib/solmate; do
  need_submodule "$SRC/lib/v4-core" "$s"
  check_pinned "$SRC/lib/v4-core" "$s"
done

# ---------------------------------------------------------------------------------------------- 2. solc 0.8.37
say "== solc"
OFFICIAL_SHA=""
for u in "${SOLC_LISTS[@]}"; do
  if curl -fsSL --max-time 60 -o "$WORK/solc-list.json" "$u" 2>>"$LOG"; then
    OFFICIAL_SHA=$(h solc-sha "$WORK/solc-list.json" "$SOLC_LONG") || OFFICIAL_SHA=""
    [ -n "$OFFICIAL_SHA" ] && { say "  official sha256 of $SOLC_LONG from $u: $OFFICIAL_SHA"; break; }
  fi
done
if [ -n "${SOLC_SHA256:-}" ]; then
  SOLC_SHA256=$(echo "$SOLC_SHA256" | tr 'A-F' 'a-f' | sed 's/^0x//')
  [ -z "$OFFICIAL_SHA" ] || [ "$OFFICIAL_SHA" = "$SOLC_SHA256" ] \
    || die "SOLC_SHA256 ($SOLC_SHA256) differs from the published list.json ($OFFICIAL_SHA)"
  OFFICIAL_SHA=$SOLC_SHA256
fi
if [ -n "${SOLC:-}" ]; then
  [ -x "$SOLC" ] || die "SOLC=$SOLC is not executable"
  GOT_SHA=$(sha256sum "$SOLC" | cut -d' ' -f1)
  if [ -n "$OFFICIAL_SHA" ] && [ "$GOT_SHA" != "$OFFICIAL_SHA" ]; then
    say "  WARNING: SOLC sha256 $GOT_SHA is not the official linux-amd64 build; relying on its version string and on the bytecode matching the record"
  fi
else
  [ -n "$OFFICIAL_SHA" ] || die "cannot fetch the solc checksum list (${SOLC_LISTS[*]}); set SOLC_SHA256=<sha256 of solc-static-linux v$SOLC_VERSION> or SOLC=<path>"
  SOLC_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/milord-hookr-verify"
  SOLC="$SOLC_DIR/solc-$SOLC_LONG"
  mkdir -p "$SOLC_DIR"
  if [ ! -f "$SOLC" ] || [ "$(sha256sum "$SOLC" | cut -d' ' -f1)" != "$OFFICIAL_SHA" ]; then
    say "  downloading $SOLC_URL"
    curl -fsSL --max-time 300 -o "$SOLC.part" "$SOLC_URL" || die "cannot download $SOLC_URL"
    GOT_SHA=$(sha256sum "$SOLC.part" | cut -d' ' -f1)
    [ "$GOT_SHA" = "$OFFICIAL_SHA" ] || { rm -f "$SOLC.part"; die "downloaded solc sha256 $GOT_SHA != official $OFFICIAL_SHA"; }
    chmod 755 "$SOLC.part"; mv "$SOLC.part" "$SOLC"
  fi
  GOT_SHA=$OFFICIAL_SHA
fi
SOLC_VER=$("$SOLC" --version | sed -n 's/^Version: //p')
case "$SOLC_VER" in "$SOLC_LONG".*|"$SOLC_LONG") ;; *) die "solc reports '$SOLC_VER', expected $SOLC_LONG" ;; esac
say "  $SOLC ($SOLC_VER, sha256 $GOT_SHA)"

# ---------------------------------------------------------------------------------------------- 3. offline phase
say "== offline: settings, sources, standard inputs"
printf '%s\n' "${SELECTION[@]}" > "$WORK/selection.txt"
cat > "$WORK/expect.json" <<EOF
{"chainId": $CHAIN_ID, "compiler": "$SOLC_LONG", "viaIR": $EXPECT_VIA_IR, "optimizerRuns": $EXPECT_OPTIMIZER_RUNS,
 "evmVersion": "$EXPECT_EVM", "only": "${ONLY:-}"}
EOF
# plan: checks settings, names, library closure, source hashes; writes one standard input per contract.
h plan "$SRC" "$WORK" | tee -a "$LOG"
say "== offline: SOURCE_MANIFEST.json (port of check-source-manifest.mjs)"
h manifest "$SRC" "$WORK" | tee -a "$LOG"

say "== offline: compile (one at a time, ${LOWPRIO[*]})"
hwm_kb() { grep -o 'VmHWM:[[:space:]]*[0-9]*' "/proc/$1/status" 2>/dev/null | grep -o '[0-9]*$' || echo 0; }
SOLC_TAG=${GOT_SHA:0:16}
: > "$WORK/metrics.tsv"
while IFS=$'\t' read -r ADDR NAME INSHA; do
  IN="$WORK/inputs/$INSHA.json"
  RES="$CACHE/$SOLC_TAG-$INSHA.json"
  if [ -s "$RES" ] && [ -z "${NO_CACHE:-}" ]; then
    say "  $NAME $ADDR: cached output ($INSHA)"
    printf '%s\t%s\t%s\t%s\n' "$ADDR" "-" "-" "cached" >> "$WORK/metrics.tsv"
    continue
  fi
  T0=$(date +%s%N)
  set +e
  "${LOWPRIO[@]}" "$SOLC" --standard-json "$IN" > "$RES.part" 2> "$WORK/solc-$INSHA.err" &
  PID=$!
  PEAK=0
  while kill -0 "$PID" 2>/dev/null; do
    m=$(hwm_kb "$PID"); [ "$m" -gt "$PEAK" ] && PEAK=$m
    sleep 0.1
  done
  wait "$PID"; RC=$?
  set -e
  SECS=$(( ($(date +%s%N) - T0) / 1000000 ))
  SECS=$(printf '%d.%03d' $((SECS / 1000)) $((SECS % 1000)))
  [ "$RC" -eq 0 ] || { cat "$WORK/solc-$INSHA.err" >> "$LOG"; die "solc exited $RC on $NAME $ADDR"; }
  mv "$RES.part" "$RES"
  say "  $NAME $ADDR: ${SECS}s, peak $((PEAK / 1024)) MB"
  printf '%s\t%s\t%s\t%s\n' "$ADDR" "$SECS" "$((PEAK / 1024))" "compiled" >> "$WORK/metrics.tsv"
done < "$WORK/plan.tsv"

say "== offline: compare with the record"
h offline "$WORK" "$CACHE" "$SOLC_TAG" | tee -a "$LOG"

# ---------------------------------------------------------------------------------------------- 4. on-chain phase
ONCHAIN=0
if [ -n "${RPC_URL:-}" ]; then
  ONCHAIN=1
  say "== on chain (read-only: eth_chainId, eth_getCode)"
  rpc() {   # <method> <params json>: one JSON-RPC read, result printed
    curl -fsS --max-time 60 -H 'content-type: application/json' \
      -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\",\"params\":$2}" "$RPC_URL" \
      | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const j=JSON.parse(s);if(j.error||typeof j.result!=="string"){console.error(JSON.stringify(j.error||j));process.exit(1)}console.log(j.result)})'
  }
  if command -v cast >/dev/null; then
    CHAIN=$(cast chain-id --rpc-url "$RPC_URL")
  else
    CHAIN=$(( $(rpc eth_chainId '[]') ))
  fi
  [ "$CHAIN" = "$CHAIN_ID" ] || die "RPC chain id is $CHAIN, expected $CHAIN_ID (Robinhood Chain)"
  BLOCK_AT=$(rpc eth_blockNumber '[]' 2>/dev/null || echo unknown)
  say "  chain id $CHAIN, block $BLOCK_AT"
  while IFS=$'\t' read -r ADDR NAME INSHA; do
    if command -v cast >/dev/null; then
      cast code --rpc-url "$RPC_URL" "$ADDR" > "$WORK/chain/$ADDR.hex"
    else
      rpc eth_getCode "[\"$ADDR\",\"latest\"]" > "$WORK/chain/$ADDR.hex"
    fi
  done < "$WORK/plan.tsv"
  h onchain "$WORK" "$CACHE" "$SOLC_TAG" | tee -a "$LOG"
  say "  wiring (immutables MILORD relies on, read back with eth_call):"
  call_word() {   # <address> <signature>: first 32-byte word returned, lowercase hex without 0x ("error" on failure)
    local data out
    data=$(h selector "$2")
    out=$(rpc eth_call "[{\"to\":\"$1\",\"data\":\"$data\"},\"latest\"]" 2>>"$LOG") || { echo error; return; }
    out=$(echo "${out#0x}" | tr 'A-F' 'a-f'); echo "${out:0:64}"
  }
  : > "$WORK/wiring.tsv"
  for W in "${WIRING[@]}"; do
    read -r WADDR WSIG WEXP WLABEL <<< "$W"
    GOT=$(call_word "$WADDR" "$WSIG")
    case "$WEXP" in
      0x*) WANT=$(printf '%064s' "$(echo "${WEXP#0x}" | tr 'A-F' 'a-f')" | tr ' ' 0) ;;
      *)   WANT=$(printf '%064x' "$WEXP") ;;
    esac
    if [ "$GOT" = "$WANT" ]; then R=ok; else R=MISMATCH; fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$R" "$WLABEL $WADDR" "$WSIG" "$WEXP" "0x$GOT" >> "$WORK/wiring.tsv"
    say "    $R $WLABEL.$WSIG expected $WEXP, got 0x$GOT"
  done
  for W in "${INFO_READS[@]}"; do
    read -r WADDR WSIG WLABEL <<< "$W"
    GOT=$(call_word "$WADDR" "$WSIG")
    printf '%s\t%s\t%s\t%s\t%s\n' "info" "$WLABEL $WADDR" "$WSIG" "-" "0x$GOT" >> "$WORK/wiring.tsv"
    say "    info $WLABEL.$WSIG = 0x$GOT"
  done
  printf '{"chainId": %s, "block": "%s"}\n' "$CHAIN" "$BLOCK_AT" > "$WORK/chain.json"
else
  say "== on chain: not run (set RPC_URL to a Robinhood Chain RPC to run it)"
fi

# ---------------------------------------------------------------------------------------------- 5. report
cat > "$WORK/run.json" <<EOF
{"hookrRepo": "$HOOKR_REPO", "hookrCommit": "$HEAD_SHA", "solc": "$SOLC_VER", "solcSha256": "$GOT_SHA",
 "solcOfficialSha256": "$OFFICIAL_SHA", "onchain": $ONCHAIN, "host": "$(uname -sm)", "nproc": "$(nproc 2>/dev/null || echo ?)",
 "date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF
set +e
h report "$WORK" "$OUT" | tee -a "$LOG"
STATUS=${PIPESTATUS[0]}
set -e
say "report: $OUT/report.md, $OUT/report.json; log: $LOG"
exit "$STATUS"

# ================================================================================================ helper (node)
# Everything between the markers is written verbatim to out/work/hookr-verify-helper.mjs and run with node. The
# quoted no-op heredoc keeps bash from parsing it (and it is after `exit` anyway).
: <<'#==HELPER-END=='
#==HELPER-BEGIN==
// MILORD hook-verify helper, generated by hookr-verify/verify-hookr.sh. Imports only node: builtins.
// Ports hookr-1/standard-input.mjs (plan), hookr-1/check-runtime.mjs (onchain) and check-source-manifest.mjs
// (manifest) from hookr-contracts@7e771b3, and adds keccak256, the metadata-hash check and the reports.
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";

const [cmd, ...args] = process.argv.slice(2);
const readJson = (p) => JSON.parse(readFileSync(p, "utf8"));
const fail = (msg) => { console.error(`REFUSED: ${msg}`); process.exit(1); };

// ---- keccak256 (Keccak-f[1600], 64-bit lanes as two 32-bit halves; original Keccak padding 0x01..0x80)
const RC = [
  "0000000000000001", "0000000000008082", "800000000000808a", "8000000080008000", "000000000000808b",
  "0000000080000001", "8000000080008081", "8000000000008009", "000000000000008a", "0000000000000088",
  "0000000080008009", "000000008000000a", "000000008000808b", "800000000000008b", "8000000000008089",
  "8000000000008003", "8000000000008002", "8000000000000080", "000000000000800a", "800000008000000a",
  "8000000080008081", "8000000000008080", "0000000080000001", "8000000080008008",
].map((x) => [parseInt(x.slice(8), 16) >>> 0, parseInt(x.slice(0, 8), 16) >>> 0]); // [lo, hi]
const ROT = [0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14];
function keccakF(s) {
  const C = new Uint32Array(10), B = new Uint32Array(50);
  for (let round = 0; round < 24; round++) {
    for (let x = 0; x < 5; x++) {
      C[2 * x] = s[2 * x] ^ s[2 * x + 10] ^ s[2 * x + 20] ^ s[2 * x + 30] ^ s[2 * x + 40];
      C[2 * x + 1] = s[2 * x + 1] ^ s[2 * x + 11] ^ s[2 * x + 21] ^ s[2 * x + 31] ^ s[2 * x + 41];
    }
    for (let x = 0; x < 5; x++) {
      const p = (x + 4) % 5, n = (x + 1) % 5;
      const dl = C[2 * p] ^ ((C[2 * n] << 1) | (C[2 * n + 1] >>> 31));
      const dh = C[2 * p + 1] ^ ((C[2 * n + 1] << 1) | (C[2 * n] >>> 31));
      for (let y = 0; y < 25; y += 5) { s[2 * (x + y)] ^= dl; s[2 * (x + y) + 1] ^= dh; }
    }
    for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) {
      const i = x + 5 * y, j = y + 5 * ((2 * x + 3 * y) % 5);
      let lo = s[2 * i], hi = s[2 * i + 1], r = ROT[i];
      if (r >= 32) { const t = lo; lo = hi; hi = t; r -= 32; }
      if (r > 0) { const l2 = (lo << r) | (hi >>> (32 - r)); hi = (hi << r) | (lo >>> (32 - r)); lo = l2; }
      B[2 * j] = lo; B[2 * j + 1] = hi;
    }
    for (let y = 0; y < 25; y += 5) for (let x = 0; x < 5; x++) {
      const i = x + y, a = ((x + 1) % 5) + y, b = ((x + 2) % 5) + y;
      s[2 * i] = B[2 * i] ^ (~B[2 * a] & B[2 * b]);
      s[2 * i + 1] = B[2 * i + 1] ^ (~B[2 * a + 1] & B[2 * b + 1]);
    }
    s[0] ^= RC[round][0]; s[1] ^= RC[round][1];
  }
}
function keccak256(buf) {
  const rate = 136, s = new Uint32Array(50);
  const padded = Buffer.alloc(Math.floor(buf.length / rate) * rate + rate);
  buf.copy(padded); padded[buf.length] ^= 0x01; padded[padded.length - 1] ^= 0x80;
  for (let off = 0; off < padded.length; off += rate) {
    for (let i = 0; i < rate / 4; i++) s[i] ^= padded.readUInt32LE(off + 4 * i);
    keccakF(s);
  }
  const out = Buffer.alloc(32);
  for (let i = 0; i < 8; i++) out.writeUInt32LE(s[i] >>> 0, 4 * i);
  return "0x" + out.toString("hex");
}
if (keccak256(Buffer.alloc(0)) !== "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    || keccak256(Buffer.from("abc")) !== "0x4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45") {
  fail("keccak256 self-test failed");
}
const sha256 = (b) => createHash("sha256").update(b).digest("hex");

// ---- base58btc and the metadata hash solc appends (CBOR map with "ipfs": 34-byte multihash)
const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function base58(bytes) {
  let n = BigInt("0x" + Buffer.from(bytes).toString("hex")), out = "";
  while (n > 0n) { out = B58[Number(n % 58n)] + out; n /= 58n; }
  for (const b of bytes) { if (b !== 0) break; out = "1" + out; }
  return out;
}
function metadataIpfs(code) {
  const len = code.readUInt16BE(code.length - 2);
  const cbor = code.subarray(code.length - 2 - len, code.length - 2);
  const key = Buffer.from([0x64, 0x69, 0x70, 0x66, 0x73, 0x58, 0x22]); // "ipfs", bytes(34)
  const at = cbor.indexOf(key);
  return at < 0 ? null : base58(cbor.subarray(at + key.length, at + key.length + 34));
}

// ---- selection and record
function loadSelection(work) {
  const expect = readJson(join(work, "expect.json"));
  const only = expect.only ? expect.only.split(",").map((a) => a.trim().toLowerCase()) : null;
  const rows = readFileSync(join(work, "selection.txt"), "utf8").split("\n").filter(Boolean).map((line) => {
    const [address, name, ...why] = line.trim().split(/\s+/);
    return { address, name, why: why.join(" ") };
  });
  return { expect, rows: only ? rows.filter((r) => only.includes(r.address.toLowerCase())) : rows, all: rows };
}

function checkSettings(record, c, expect) {
  const problems = [];
  const s = record.settings[c.settings];
  if (!s) return [`no settings profile '${c.settings}' in the record`];
  if (record.compiler !== expect.compiler) problems.push(`record compiler ${record.compiler} != ${expect.compiler}`);
  const allowed = new Set(["evmVersion", "libraries", "metadata", "optimizer", "remappings", "viaIR"]);
  for (const k of Object.keys(s)) if (!allowed.has(k)) problems.push(`unexpected setting '${k}'`);
  if (s.viaIR !== expect.viaIR) problems.push(`viaIR ${s.viaIR} != ${expect.viaIR}`);
  if (s.evmVersion !== expect.evmVersion) problems.push(`evmVersion ${s.evmVersion} != ${expect.evmVersion}`);
  const o = s.optimizer ?? {};
  if (o.enabled !== true) problems.push(`optimizer.enabled ${o.enabled} != true`);
  if (o.runs !== expect.optimizerRuns) problems.push(`optimizer.runs ${o.runs} != ${expect.optimizerRuns}`);
  for (const k of Object.keys(o)) if (k !== "enabled" && k !== "runs") problems.push(`unexpected optimizer.${k}`);
  if (s.libraries && Object.keys(s.libraries).length) problems.push("settings.libraries is not empty");
  const m = s.metadata ?? {};
  if ((m.bytecodeHash ?? "ipfs") !== "ipfs") problems.push(`metadata.bytecodeHash ${m.bytecodeHash} != ipfs`);
  if (m.appendCBOR === false) problems.push("metadata.appendCBOR is false");
  return problems;
}

// plan <src> <work>: per selected contract, check the record and write its standard-json input (standard-input.mjs)
function plan(src, work) {
  const { expect, rows } = loadSelection(work);
  const record = readJson(join(src, "hookr-1/deployments/robinhood-4663.json"));
  if (record.chainId !== expect.chainId) fail(`record chainId ${record.chainId} != ${expect.chainId}`);
  const selected = new Set(loadSelection(work).all.map((r) => r.address.toLowerCase()));
  const results = [], tsv = [];
  const fileOk = new Map();
  for (const row of rows) {
    const c = record.contracts.find((x) => x.address.toLowerCase() === row.address.toLowerCase());
    if (!c) fail(`${row.name} ${row.address} is not in the record`);
    if (c.address !== row.address) fail(`${row.address}: record spells the address ${c.address}`);
    if (c.name !== row.name) fail(`${row.address}: record names ${c.name}, the selection expects ${row.name}`);
    const r = { name: c.name, address: c.address, why: row.why, contract: c.contract, settingsProfile: c.settings,
      group: c.block ? `${c.group}/${c.block}` : c.group, deployment: c.deployment, runtimeKeccak256: c.runtimeKeccak256,
      metadataIpfs: c.metadataIpfs, libraries: c.libraries, constructorArguments: c.constructorArguments, problems: [] };
    r.settingsProblems = checkSettings(record, c, expect);
    r.settingsOk = r.settingsProblems.length === 0;
    for (const [lib, at] of Object.entries(c.libraries)) {
      if (!selected.has(at.toLowerCase())) r.problems.push(`links ${lib} at ${at}, which is not in the selection`);
    }
    // Sources: inside the checkout, and byte-identical to the record's sourceKeccak256.
    const sources = {};
    r.sourceProblems = [];
    for (const [unit, path] of Object.entries(c.sources)) {
      const abs = resolve(src, path);
      if (!abs.startsWith(resolve(src) + sep)) { r.sourceProblems.push(`${path} escapes the checkout`); continue; }
      if (!existsSync(abs)) { r.sourceProblems.push(`${path} is missing`); continue; }
      const bytes = readFileSync(abs);
      if (!fileOk.has(path)) {
        const want = record.sourceKeccak256?.[path];
        fileOk.set(path, !want ? "not in sourceKeccak256" : keccak256(bytes) !== want ? "keccak256 differs" : "");
      }
      if (fileOk.get(path)) r.sourceProblems.push(`${path}: ${fileOk.get(path)}`);
      sources[unit] = { content: bytes.toString("utf8") };
    }
    r.sourceFiles = Object.keys(c.sources).length;
    r.sourceOk = r.sourceProblems.length === 0;
    const input = {
      language: "Solidity",
      sources,
      settings: {
        ...record.settings[c.settings],
        outputSelection: { "*": { "*": ["evm.bytecode.object", "evm.deployedBytecode.object",
          "evm.deployedBytecode.immutableReferences", "evm.deployedBytecode.linkReferences", "metadata"] } },
      },
    };
    const text = JSON.stringify(input) + "\n";
    r.inputSha256 = sha256(text);
    writeFileSync(join(work, "inputs", `${r.inputSha256}.json`), text);
    tsv.push(`${c.address}\t${c.name}\t${r.inputSha256}`);
    results.push(r);
    const flag = r.settingsOk && r.sourceOk && !r.problems.length ? "ok" : "FAIL";
    console.log(`  ${flag.padEnd(4)} ${c.name.padEnd(22)} ${c.address}  settings ${c.settings}: ${r.settingsOk ? "ok" : r.settingsProblems.join("; ")}` +
      `, ${r.sourceFiles} sources: ${r.sourceOk ? "ok" : r.sourceProblems.join("; ")}${r.problems.length ? ", " + r.problems.join("; ") : ""}`);
  }
  writeFileSync(join(work, "plan.json"), JSON.stringify(results, null, 1));
  writeFileSync(join(work, "plan.tsv"), tsv.join("\n") + "\n");
}

// manifest <src> <work>: check-source-manifest.mjs without --write and without --source-git (the upstream
// Hookr-fun/hookr repository it would read is not public); result goes to manifest.json.
function manifest(src, work) {
  const sourceRepository = "https://github.com/Hookr-fun/hookr.git";
  const sourceCommit = "3e5c022764d8eda3a4c417f777abc9c8256ff011";
  const deployed = new Set(["src/HookrKernelQuoterV1.sol", "src/HookrKernelRouterV3.sol", "src/HookrMarketCoordinatorV5.sol",
    "src/HookrModularHookV6.sol", "src/HookrModularHookV6WthV5.sol", "src/HookrModuleCatalogV1.sol",
    "src/HookrNativeMechanicsBlockV2.sol", "src/HookrReleaseCreate2FactoryV1.sol", "src/HookrStackRegistryV2.sol",
    "src/HookrSwapAccountingKernelV3.sol", "src/HookrTreasuryForwarderV1.sol", "src/HookrWthExecutorAdapterV1.sol",
    "src/libraries/HookrMarketCoordinatorInitialBuyLibV4.sol", "src/libraries/HookrModularCorrectionLibV2.sol",
    "src/libraries/HookrModularCorrectionLibV3.sol", "src/libraries/HookrNativeMechanicsCoordinatorLibV2.sol",
    "src/libraries/HookrStatefulSettlementLibV1.sol"]);
  const compiledInto = new Set(["src/HookrMarketCoordinatorV3.sol", "src/HookrStackRegistryV1.sol",
    "src/HookrSwapKernelV3.sol", "src/HookrSwapKernelV5Wth.sol", "src/HookrTokenV61.sol"]);
  const roleFor = (p) => deployed.has(p) ? "deployed" : compiledInto.has(p) ? "compiled-into-a-deployed-contract"
    : p.startsWith("src/interfaces/") ? "interface" : "library";
  const files = [];
  const visit = (abs) => {
    for (const name of readdirSync(abs).sort()) {
      const cand = join(abs, name);
      if (statSync(cand).isDirectory()) visit(cand);
      else { const p = relative(src, cand).split(sep).join("/"); if (p.endsWith(".sol")) files.push(p); }
    }
  };
  visit(join(src, "src"));
  files.sort();
  const problems = [];
  const m = readJson(join(src, "SOURCE_MANIFEST.json"));
  if (m.source_repository !== sourceRepository || m.source_commit !== sourceCommit) problems.push("manifest source identity differs");
  if (JSON.stringify(m.files.map((e) => e.path)) !== JSON.stringify(files)) problems.push("manifest file inventory is stale");
  for (const e of m.files) {
    const expected = { path: e.path, role: roleFor(e.path), source_repository: sourceRepository,
      source_path: `contracts/${e.path}`, source_commit: sourceCommit, sha256: sha256(readFileSync(join(src, e.path))) };
    if (JSON.stringify(e) !== JSON.stringify(expected)) problems.push(`manifest entry is stale or malformed: ${e.path}`);
  }
  const result = { ok: problems.length === 0, files: m.files.length, problems, scope: "repository src/ (pre-hookr-1 export)" };
  writeFileSync(join(work, "manifest.json"), JSON.stringify(result, null, 1));
  console.log(result.ok ? `  SOURCE_MANIFEST.json verified (${m.files.length} files)` : `  SOURCE_MANIFEST.json FAILED: ${problems.join("; ")}`);
}

// Build a contract's runtime from the solc output with the record's libraries linked (as check-runtime.mjs does).
// A library's runtime starts with PUSH20 <its own address>, which its creation code writes at deployment; we put
// the library's own address there (a fixed, known value, not read from the chain).
function built(r, cache, tag) {
  const out = readJson(join(cache, `${tag}-${r.inputSha256}.json`));
  const errors = (out.errors ?? []).filter((e) => e.severity === "error");
  if (errors.length) return { error: errors.map((e) => e.formattedMessage).join("\n") };
  const unit = r.contract.slice(0, r.contract.lastIndexOf(":")), name = r.contract.split(":").pop();
  const deployed = out.contracts?.[unit]?.[name]?.evm?.deployedBytecode;
  if (!deployed) return { error: `${r.contract} is not in the solc output` };
  const meta = JSON.parse(out.contracts[unit][name].metadata);
  const libraries = Object.fromEntries(Object.entries(r.libraries).map(([k, v]) => [k.toLowerCase(), v]));
  const hex = deployed.object.split("");
  for (const [file, libs] of Object.entries(deployed.linkReferences ?? {})) {
    for (const [lib, refs] of Object.entries(libs)) {
      const linked = libraries[`${file}:${lib}`.toLowerCase()];
      if (!linked) return { error: `the record names no address for library ${file}:${lib}` };
      for (const { start } of refs) hex.splice(start * 2, 40, ...linked.slice(2).toLowerCase());
    }
  }
  if (hex.join("").includes("__$")) return { error: "unlinked library placeholder left in the bytecode" };
  const code = Buffer.from(hex.join(""), "hex");
  const isLibrary = /^73[0-9a-f]{40}30(14|18)/i.test(deployed.object) && code.subarray(1, 21).equals(Buffer.alloc(20));
  if (isLibrary) Buffer.from(r.address.slice(2), "hex").copy(code, 1);
  // Via-IR libraries keep their call guard address as the immutable "library_deploy_address": that one is the
  // library's own address (known), so it is filled in here and checked, not taken from the chain.
  const immutables = [];
  for (const [id, refs] of Object.entries(deployed.immutableReferences ?? {})) {
    for (const x of refs) {
      if (id === "library_deploy_address") Buffer.from(r.address.slice(2).padStart(64, "0"), "hex").copy(code, x.start);
      else immutables.push(x);
    }
  }
  return { code, immutables, isLibrary, compilerVersion: meta.compiler?.version, metaSettings: meta.settings };
}

// offline <work> <cache> <tag>: compile results against the record (no chain needed).
function offline(work, cache, tag) {
  const plan = readJson(join(work, "plan.json"));
  const res = {};
  for (const r of plan) {
    const o = { problems: [] };
    const b = built(r, cache, tag);
    if (b.error) { o.problems.push(b.error); o.ok = false; res[r.address] = o; console.log(`  FAIL ${r.name} ${r.address}: ${b.error}`); continue; }
    const zeroed = Buffer.from(b.code);
    for (const { start, length } of b.immutables) zeroed.fill(0, start, start + length);
    o.bytes = b.code.length;
    o.immutableSlots = b.immutables.length;
    o.isLibrary = b.isLibrary;
    o.keccakImmutablesZeroed = keccak256(zeroed);
    o.metadataIpfs = metadataIpfs(b.code);
    o.metadataOk = o.metadataIpfs === r.metadataIpfs;
    if (!o.metadataOk) o.problems.push(`metadata IPFS ${o.metadataIpfs} != record ${r.metadataIpfs}`);
    if (b.compilerVersion !== readJson(join(work, "expect.json")).compiler) o.problems.push(`metadata compiler ${b.compilerVersion}`);
    if (b.immutables.length === 0) {
      o.runtimeKeccak = keccak256(b.code);
      o.runtimeKeccakOk = o.runtimeKeccak === r.runtimeKeccak256;
      if (!o.runtimeKeccakOk) o.problems.push(`runtime keccak ${o.runtimeKeccak} != record ${r.runtimeKeccak256}`);
    } else {
      o.runtimeKeccakOk = null; // record hashes the chain code, whose immutables only the chain (or a constructor run) has
    }
    o.ok = o.problems.length === 0;
    res[r.address] = o;
    console.log(`  ${o.ok ? "ok  " : "FAIL"} ${r.name.padEnd(22)} ${r.address}  ${o.bytes} bytes, metadata ${o.metadataOk ? "= record" : "DIFFERS"}, ` +
      (o.runtimeKeccakOk === null ? `${o.immutableSlots} immutable slots (runtime keccak needs the chain)` :
        `runtime keccak ${o.runtimeKeccakOk ? "= record" : "DIFFERS"}${b.isLibrary ? " (library, own address at PUSH20)" : ""}`));
  }
  writeFileSync(join(work, "offline.json"), JSON.stringify(res, null, 1));
}

// onchain <work> <cache> <tag>: byte-for-byte against the chain code (check-runtime.mjs) + record keccak.
function onchain(work, cache, tag) {
  const plan = readJson(join(work, "plan.json"));
  const res = {};
  for (const r of plan) {
    const o = { problems: [] };
    const chain = Buffer.from(readFileSync(join(work, "chain", `${r.address}.hex`), "utf8").trim().replace(/^0x/, ""), "hex");
    o.chainBytes = chain.length;
    o.chainKeccak = keccak256(chain);
    o.recordKeccakOk = o.chainKeccak === r.runtimeKeccak256;
    if (!chain.length) o.problems.push("no code at the address");
    if (!o.recordKeccakOk) o.problems.push(`chain code keccak ${o.chainKeccak} != record ${r.runtimeKeccak256}`);
    const b = built(r, cache, tag);
    if (b.error) o.problems.push(b.error);
    else if (b.code.length !== chain.length) o.problems.push(`${b.code.length} bytes built, ${chain.length} on chain`);
    else {
      const code = Buffer.from(b.code);
      for (const { start, length } of b.immutables) chain.copy(code, start, start, start + length);
      if (!code.equals(chain)) {
        let first = 0; while (code[first] === chain[first]) first += 1;
        o.problems.push(`first differing byte at ${first}`);
      }
      o.immutableSlots = b.immutables.length;
    }
    o.ok = o.problems.length === 0;
    res[r.address] = o;
    console.log(`  ${o.ok ? "MATCH   " : "MISMATCH"} ${r.name.padEnd(22)} ${r.address}  ${o.chainBytes} bytes, ` +
      `${o.immutableSlots ?? "?"} immutable slots, keccak ${o.recordKeccakOk ? "= record" : "DIFFERS"}${o.ok ? "" : ": " + o.problems.join("; ")}`);
  }
  writeFileSync(join(work, "onchain.json"), JSON.stringify(res, null, 1));
}

// report <work> <out>: report.json + report.md, exit 1 on any failure.
function report(work, out) {
  const run = readJson(join(work, "run.json"));
  const plan = readJson(join(work, "plan.json"));
  const manifestRes = readJson(join(work, "manifest.json"));
  const off = existsSync(join(work, "offline.json")) ? readJson(join(work, "offline.json")) : {};
  const on = existsSync(join(work, "onchain.json")) ? readJson(join(work, "onchain.json")) : null;
  const chainInfo = existsSync(join(work, "chain.json")) ? readJson(join(work, "chain.json")) : null;
  const metrics = Object.fromEntries(readFileSync(join(work, "metrics.tsv"), "utf8").split("\n").filter(Boolean)
    .map((l) => { const [a, s, m, how] = l.split("\t"); return [a, { seconds: s === "-" ? null : Number(s), peakMB: m === "-" ? null : Number(m), how }]; }));
  const rows = plan.map((r) => {
    const o = off[r.address] ?? { ok: false, problems: ["not compiled"] };
    const c = on ? on[r.address] : null;
    return {
      name: r.name, address: r.address, why: r.why, contract: r.contract, settingsProfile: r.settingsProfile,
      deployment: r.deployment.kind, settingsOk: r.settingsOk, settingsProblems: r.settingsProblems,
      sourceOk: r.sourceOk && r.problems.length === 0, sourceFiles: r.sourceFiles, sourceProblems: [...r.sourceProblems, ...r.problems],
      offlineCompileOk: !!o.ok, offline: o, onchain: c ? (c.ok ? "match" : "MISMATCH") : "not run", onchainDetail: c,
      runtimeKeccak256: r.runtimeKeccak256, metadataIpfs: r.metadataIpfs, compile: metrics[r.address] ?? null,
    };
  });
  const wiring = existsSync(join(work, "wiring.tsv")) ? readFileSync(join(work, "wiring.tsv"), "utf8").split("\n").filter(Boolean)
    .map((l) => { const [result, contract, call, expected, got] = l.split("\t"); return { result, contract, call, expected, got }; }) : [];
  const ok = manifestRes.ok && rows.every((x) => x.settingsOk && x.sourceOk && x.offlineCompileOk && x.onchain !== "MISMATCH")
    && wiring.every((w) => w.result !== "MISMATCH");
  const verdict = !ok ? "FAIL" : run.onchain ? "PASS (offline + on chain)" : "PASS offline; on-chain phase not run";
  writeFileSync(join(out, "report.json"), JSON.stringify({ verdict, ok, run, chain: chainInfo, sourceManifest: manifestRes, contracts: rows, wiring }, null, 1) + "\n");
  const yes = (b) => (b ? "OK" : "**FAIL**");
  const md = [
    `# Hookr-1 bytecode verification`, ``,
    `**${verdict}**`, ``,
    `- hookr-contracts ${run.hookrCommit} (${run.hookrRepo})`,
    `- solc ${run.solc}, sha256 ${run.solcSha256}${run.solcOfficialSha256 ? (run.solcSha256 === run.solcOfficialSha256 ? " (official)" : " (NOT the official build)") : ""}`,
    `- SOURCE_MANIFEST.json (${manifestRes.scope}): ${manifestRes.ok ? `OK, ${manifestRes.files} files` : "**FAIL** " + manifestRes.problems.join("; ")}`,
    `- on chain: ${chainInfo ? `chain id ${chainInfo.chainId}, block ${chainInfo.block}` : "not run (no RPC_URL)"}`,
    `- ${run.host}, ${run.nproc} CPUs, ${run.date}`, ``,
    `| Contract | Address | Settings | Sources | Offline compile | Runtime keccak (offline) | On chain | Compile s | Peak MB |`,
    `|---|---|---|---|---|---|---|---|---|`,
    ...rows.map((x) => `| ${x.name} | \`${x.address}\` | ${yes(x.settingsOk)} ${x.settingsProfile} | ${yes(x.sourceOk)} (${x.sourceFiles}) | ` +
      `${yes(x.offlineCompileOk)}${x.offline.metadataOk ? ", metadata = record" : ""} | ` +
      `${x.offline.runtimeKeccakOk === true ? "= record" : x.offline.runtimeKeccakOk === false ? "**DIFFERS**" : `needs chain (${x.offline.immutableSlots} immutables)`} | ` +
      `${x.onchain === "match" ? "match" : x.onchain === "MISMATCH" ? "**MISMATCH**" : "not run"} | ` +
      `${x.compile?.seconds ?? x.compile?.how ?? ""} | ${x.compile?.peakMB ?? ""} |`),
    ``, `Why each contract is selected:`, ``,
    ...rows.map((x) => `- ${x.name} \`${x.address}\` (${x.deployment}): ${x.why}`),
  ];
  const problems = rows.flatMap((x) => [...x.settingsProblems, ...x.sourceProblems, ...(x.offline.problems ?? []),
    ...((x.onchainDetail?.problems) ?? [])].map((p) => `- ${x.name} ${x.address}: ${p}`));
  if (wiring.length) {
    md.push(``, `On-chain wiring (eth_call; "info" rows are owner-controlled state, reported only):`, ``,
      `| Result | Contract | Call | Expected | Got |`, `|---|---|---|---|---|`,
      ...wiring.map((w) => `| ${w.result === "MISMATCH" ? "**MISMATCH**" : w.result} | ${w.contract} | ${w.call} | ${w.expected} | \`${w.got}\` |`));
    for (const w of wiring) if (w.result === "MISMATCH") problems.push(`- wiring ${w.contract} ${w.call}: expected ${w.expected}, got ${w.got}`);
  }
  if (problems.length) md.push(``, `Problems:`, ``, ...problems);
  writeFileSync(join(out, "report.md"), md.join("\n") + "\n");
  console.log(`== ${verdict}`);
  process.exit(ok ? 0 : 1);
}

// solc-sha <list.json> <longVersion>: the sha256 the official list publishes for that build.
function solcSha(list, long) {
  const b = readJson(list).builds.find((x) => x.longVersion === long);
  if (!b || !/^0x[0-9a-f]{64}$/.test(b.sha256)) process.exit(1);
  console.log(b.sha256.slice(2));
}

switch (cmd) {
  case "plan": plan(args[0], args[1]); break;
  case "manifest": manifest(args[0], args[1]); break;
  case "offline": offline(args[0], args[1], args[2]); break;
  case "onchain": onchain(args[0], args[1], args[2]); break;
  case "report": report(args[0], args[1]); break;
  case "solc-sha": solcSha(args[0], args[1]); break;
  case "selector": console.log(keccak256(Buffer.from(args[0])).slice(0, 10)); break;
  default: fail(`unknown helper command ${cmd}`);
}
#==HELPER-END==

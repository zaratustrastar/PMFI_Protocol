# Hookr-1 bytecode verification (2026-10-21)

**Result so far: offline phase PASS for all 23 selected contracts; on-chain phase not yet run** (this environment
cannot reach the Robinhood RPC). The on-chain phase is one command on the VPS (below). Until it reports `PASS
(offline + on chain)`, the claim "the code MILORD calls is the published Hookr source" rests on Hookr's own record.

Tool: `hookr-verify/verify-hookr.sh`. Output: `hookr-verify/out/report.md` and `report.json` (git-ignored).

## Run it on the VPS (on-chain phase)

From `milord/`:

```sh
RPC_URL=https://rpc.mainnet.chain.robinhood.com ./hookr-verify/verify-hookr.sh
```

It re-runs the offline phase (fresh clone of the pinned Hookr commit, official solc 0.8.37 downloaded once to
`~/.cache/milord-hookr-verify/` and sha256-checked), then does the on-chain checks. Exit status 0 only if everything
matches. Expected cost on the VPS: about 70 s of single-core compile time at `nice 19` / `ionice` idle, peak
**124 MB** RAM for solc, ~100 MB of git clone in a temp dir that is deleted on exit. It only calls `eth_chainId`,
`eth_blockNumber`, `eth_getCode` and `eth_call` on view getters; it never reads a key and never sends a transaction.
Options: `HOOKR_SRC=<checkout>` (reuse a checkout at the pin), `SOLC=<path>`, `SOLC_SHA256=<hex>` (if the checksum
list cannot be fetched), `ONLY=<addr,...>`, `NO_CACHE=1`.

## What is verified, and how

| Step | Check | Phase |
|---|---|---|
| Source identity | `cholakovvv/hookr-contracts` cloned at `7e771b3b244c812c699abe3e74d166289709ff0f`, `git rev-parse HEAD` = pin, no tracked modifications; submodules `lib/v4-core` (46c6834), its `openzeppelin-contracts` (dbb6104) and `solmate` (4b47a19) at the commits the pinned tree records | offline |
| Source bytes | every source file each selected contract compiles from hashes to the record's `sourceKeccak256` (342 files are listed; the selection uses a subset) | offline |
| `SOURCE_MANIFEST.json` | port of `check-source-manifest.mjs`: inventory and sha256 of the repo's top-level `src/` (47 files). Note: that manifest covers the **older pre-hookr-1 export**, not `hookr-1/`; it passes but proves nothing about the contracts below. The `--source-git` provenance mode needs the non-public `Hookr-fun/hookr` repo and is not run | offline |
| Compiler | official `solc-static-linux` v0.8.37 from GitHub releases; sha256 `5de843c2…14404a` checked against the published `linux-amd64/list.json` (binaries.soliditylang.org, falling back to the same list in `ethereum/solc-bin` on GitHub; this run used the fallback because binaries.soliditylang.org is blocked here). The value also equals the hash in Hookr's README. `solc --version` must be `0.8.37+commit.f401782d` | offline |
| Settings | the record's `compiler` is `0.8.37+commit.f401782d` and each contract's settings profile has `viaIR: true`, `optimizer {enabled: true, runs: 200}`, `evmVersion: prague`, empty `libraries`, IPFS metadata hash, and no other keys (anything else fails) | offline |
| Compile | standard-json input rebuilt exactly as `hookr-1/standard-input.mjs` does (source unit names from the verified metadata); no compiler errors; the IPFS metadata hash embedded in the compiled runtime equals the record's `metadataIpfs` (this hash commits to every source file, its unit name and every setting) | offline |
| Runtime keccak (offline) | where the runtime has no immutables, `keccak256(compiled runtime with the record's library addresses linked)` must equal `runtimeKeccak256` | offline |
| Byte for byte | `eth_getCode` equals the compiled runtime with libraries linked, taking only the immutable slots solc reports from the chain (as `hookr-1/check-runtime.mjs`), and the chain code's keccak equals the record's `runtimeKeccak256` | on chain |
| Chain | `eth_chainId` (or `cast chain-id`) is 4663 | on chain |
| Wiring | 23 getters that expose the immutables MILORD relies on, compared with the record's constructor arguments: Launcher `registry`/`poolManager`; FamilyLock `launcher`; Root `registry`/`poolManager`/`router`/`quoter`; both Rules `registry`/`protocolRecipient` (= HookrTreasury)/`minProtocolShareBps` (= 2000); tax A `registry`/`routeRegistry`/`queueImplementation`/`minProtocolShareBps`/`calendar`; TaxQueue `executor`/`routeRegistry`; tax B `root`/`partnerRegistry`/`calendar`. Owner-controlled state is printed for review, not judged: registry `owner`/`guardian`, treasury `owner`/`target`, launcher `launchFee`, route-registry and partner-registry `owner` | on chain |

**What `runtimeKeccak256` means.** It is the keccak256 of the code at the address as deployed, immutables included.
Proven here: for the four contracts without constructor-set immutables (HookrLaunchChecks, HookrTokenDeployer,
HookrRegistryAdmin, HookrRegistryChecks, all linked libraries) the compiled runtime, with the record's library
addresses linked and the library's own address in its via-IR `library_deploy_address` slot, hashes exactly to the
record's value. For every other contract the immutables are written by the constructor and only the chain has them,
so that hash can only be compared on chain. The wiring step then checks the values of the immutables that matter.

## Selected contracts (23)

| Contract | Address | Why |
|---|---|---|
| HookrLauncher | `0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4` | launches the MILORD family; `launchFee()`; `transferFamily` |
| HookrLaunchChecks | `0x8567369a70100828611BB6F44ec051B48F10fb59` | library linked by the launcher: launch checks and the **launch fee** |
| HookrTokenDeployer | `0x1B23D6d7beBcB61E39317E9F0Bc02AEB91aFCcAe` | library linked by the launcher |
| HookrFamilyLock | `0xfAFEcE64099C1E791C4B46189c277f00c0c7e00b` | permanent lock; pays LP fees to HookrLockFeeCollector |
| HookrRoot | `0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc` | the pool's v4 hook |
| HookrLane | `0x9A8a1006469BA196f8FEfc017a53505C83A060BB` | module the root DELEGATECALLs (runs with the root's storage) |
| HookrRules (A, release) | `0x4a71c270b1520F94064268102AebD744b0DE7942` | Rules candidate |
| HookrRecapture | `0xF83569E564d70daebc35950f66a73f2e2B915583` | module Rules A DELEGATECALLs |
| HookrRules (B, whole pot) | `0x10f684361b35C8411a2c07F68A6fB1F4B7e555F7` | Rules candidate |
| HookrRecapture | `0x60Efc4a41498e9433be2155b6cc1677AEC31D649` | module Rules B DELEGATECALLs |
| HookrRegistry | `0x463c82beA1a52F4Cc05ca0741a24F0AD24bA4602` | admissions MilordAtomicLaunch checks (`launcher.registry()`) |
| HookrRegistryAdmin | `0x675ac2102E8c69E2467F64656E3c235AF3901Ac1` | library linked by the registry |
| HookrRegistryChecks | `0xB9F5c54E5Cc7ac6c7a96335E5A334Cf4eA496E98` | library linked by the registry and RegistryAdmin |
| HookrTreasury | `0x2242F7FC75606500503075f29ea8cDA684EfB4a9` | Rules' `protocolRecipient`: fee terms and protocol-share destination |
| HookrDirectionalTax (A) | `0xE73fAD0d773b9571a7a9d2A1edc01132fF5f1e1F` | Tax + Conversion: the 1.5% buy/sell tax and its queues |
| HookrTaxQueue | `0x2C2Fd39520165e5Ee5a9DD69297fe0BE02F29619` | **queue implementation**: every per-pool tax queue is an ERC-1167 clone of it |
| HookrFeeSwapExecutor | `0x0Eb5076E91803194281Dcfd94aDF453d5D1575B0` | executor wired into tax A and the queue implementation |
| HookrFeeRouteRegistry | `0xC9B5Ded0c6f1eeFFf2a41607d6EE4e71be88763A` | route registry of tax A and the queues |
| HookrSessionAdvisory | `0x78Bc6b7302Dda8632e661c8d74966bdBea1c66b1` | session calendar fixed in both tax candidates |
| HookrDirectionalTax (B) | `0x99012422e1acBdf53C42BB378eb2f4e468005d3c` | tax candidate B (see "Findings") |
| HookrPartnerRegistry | `0x48FE32A6a1b1507D6981A51f907d53595C16e862` | partner registry fixed in tax B |
| HookrRouter | `0xC2Bc9a3fe6c4Ce8C2F7EB4655b0869b01D8c93C9` | Hookr's authenticated router (HOOKR_FINAL_CONFIG); root immutable |
| HookrQuoter | `0x445D7891381Aa7F91F5048EdBC6a611ccBDe7865` | Hookr's quoter (HOOKR_FINAL_CONFIG); root immutable |

Not selected: the other 66 record entries (Programs, Paymaster, LP Boost, owned roots, other Hook Blocks). MILORD
does not call them and none of the selected contracts links or DELEGATECALLs them. HookrLockFeeCollector and the
rest of MILORD are our own code and are covered by MILORD's CI.

## Offline result (this container, 2026-10-21)

Linux x86_64, 4 CPUs, solc 0.8.37 official build, hookr-contracts at the pin. Each contract compiled alone at
`nice 19`/`ionice -c3`. Peak memory is VmHWM, sampled every 0.1 s; `getrusage` maxrss on the five largest gave the
same values (Rules 123 MB, Root 115 MB, Registry 100 MB, Launcher 93 MB, Lane 81 MB).

| Contract | Address | Settings | Sources | Compile + metadata = record | Runtime keccak = record | Time s | Peak MB |
|---|---|---|---|---|---|---|---|
| HookrLauncher | 0x1cf2…8Ef4 | OK | OK (59) | OK | needs chain (18 immutables) | 4.9 | 91 |
| HookrLaunchChecks | 0x8567…fb59 | OK | OK (59) | OK | **OK** | 4.3 | 90 |
| HookrTokenDeployer | 0x1B23…CcAe | OK | OK (18) | OK | **OK** | 0.7 | 28 |
| HookrFamilyLock | 0xfAFE…e00b | OK | OK (63) | OK | needs chain (8) | 5.4 | 95 |
| HookrRoot | 0x89c9…6aCc | OK | OK (48) | OK | needs chain (38) | 6.0 | 115 |
| HookrLane | 0x9A8a…60BB | OK | OK (39) | OK | needs chain (36) | 2.6 | 81 |
| HookrRules A | 0x4a71…7942 | OK | OK (67) | OK | needs chain (32) | 6.4 | 124 |
| HookrRecapture (A) | 0xF835…5583 | OK | OK (67) | OK | needs chain (38) | 6.2 | 123 |
| HookrRules B | 0x10f6…55F7 | OK | OK (67) | OK | needs chain (32) | 6.2 | 122 |
| HookrRecapture (B) | 0x60Ef…D649 | OK | OK (67) | OK | needs chain (38) | 6.7 | 123 |
| HookrRegistry | 0x463c…4602 | OK | OK (33) | OK | needs chain (24) | 4.3 | 99 |
| HookrRegistryAdmin | 0x675a…1Ac1 | OK | OK (28) | OK | **OK** | 1.9 | 52 |
| HookrRegistryChecks | 0xB9F5…6E98 | OK | OK (25) | OK | **OK** | 0.9 | 35 |
| HookrTreasury | 0x2242…B4a9 | OK | OK (29) | OK | needs chain (6) | 1.6 | 59 |
| HookrDirectionalTax A | 0xE73f…1e1F | OK | OK (51) | OK | needs chain (13) | 3.2 | 80 |
| HookrTaxQueue | 0x2C2F…9619 | OK | OK (39) | OK | needs chain (20) | 1.1 | 43 |
| HookrFeeSwapExecutor | 0x0Eb5…75B0 | OK | OK (12) | OK | needs chain (11) | 0.6 | 30 |
| HookrFeeRouteRegistry | 0xC9B5…763A | OK | OK (7) | OK | needs chain (2) | 0.6 | 31 |
| HookrSessionAdvisory | 0x78Bc…66b1 | OK | OK (31) | OK | needs chain (2) | 2.0 | 60 |
| HookrDirectionalTax B | 0x9901…5d3c | OK | OK (25) | OK | needs chain (8) | 1.0 | 38 |
| HookrPartnerRegistry | 0x48FE…e862 | OK | OK (15) | OK | needs chain (13) | 1.3 | 45 |
| HookrRouter | 0xC2Bc…93C9 | OK | OK (39) | OK | needs chain (23) | 1.9 | 55 |
| HookrQuoter | 0x445D…7865 | OK | OK (34) | OK | needs chain (14) | 0.9 | 40 |

Settings profiles used: `release` (17 contracts), `hook-blocks-2` (4), `hook-blocks-3` (2). All equal the expected
settings; no mismatch was found. Total compile time 71 s, whole run 81 s including clone. The comparison logic was
also tested here against a local fake RPC: a tampered byte, a wrong library address, a wrong chain-code keccak and
wrong getter values are each reported as MISMATCH with exit status 1, and a correct library returns MATCH.

## Findings

1. **Tax candidate B (`0x9901…5d3c`) is a different contract from candidate A.** The record lists it as role
   `HookrPartnerTax` in the builder-attribution block. It has no tax queues and no `terms(root, id)`: it charges a
   frozen take credited as HookrRules claims to a partner revenue vault, and `bind` accepts only pools whose vault the
   HookrPartnerRegistry recorded, so "a pool opened through any other launcher cannot bind it" (its own NatSpec):
   only pools launched through HookrAttributionLauncher. MilordAtomicLaunch launches through HookrLauncher and reads
   `IHookrDirectionalTaxFork.terms(...)` (queues), which only candidate A has. **Candidate A (`0xE73f…1e1F`) is the
   only one compatible with the current MILORD launch code.**
2. **No upgradeable proxy among the 23.** No `upgradeTo`, implementation slot or `selfdestruct`. The record's
   `deployment.proxy` field is the CREATE3 deploy helper the factory `0xc7c6…3F7d` used, not a proxy in front of the
   contract. Every DELEGATECALL target is an immutable created in the constructor and pinned by code hash (root to
   HookrLane, each Rules to its own HookrRecapture), or a linked library whose address is in the bytecode. The code
   checked here can never change.
3. **Clones.** MILORD's two tax queues will be ERC-1167 clones that HookrDirectionalTax A deploys at `bind` (CREATE2,
   `HookrCloneArgs`), with the frozen `QueueTerms` appended to the clone's code. They do not exist before launch, so
   this tool cannot verify them. Their code is fully determined: `363d3d373d3d3d363d73` + `2C2Fd395…F29619` +
   `5af43d82803e903d91602b57fd5bf3` + `abi.encode(QueueTerms)`. After launch, compare `eth_getCode(buyQueue|sellQueue)`
   with that. MilordAtomicLaunch already reads and checks `terms()` through the clone. The implementation refuses
   direct use (`NotAClone`).
4. **Owner-controlled parameters relevant to MILORD** (none can change the bytecode or the pool's frozen terms):
   - HookrRegistry owner (constructor: `0xc0967485a25f18e70262a2ee80c7595cc64a5fc`, 30-minute timelock): admissions
     and revocations of Rules and advisories, launchers and quotes. The **guardian can `pauseNewMarkets` with no
     timelock**, which would make MILORD's launch transaction revert. It cannot affect a pool that is already open.
   - Launch fee: up to 0.01 native, changed through the registry owner's timelock. MilordAtomicLaunch caps it
     (`maxLaunchFee`).
   - HookrTreasury owner (same address at deploy): `proposeTarget`/`acceptTarget` (30 min) moves where the
     **protocol share** (Hookr's 20% of the tax) is paid. It also changes the rule-share floor and integrators for
     **new** pools. MILORD's 80% goes to the splitter frozen in the queue's terms, which the owner cannot reach.
   - Protocol-share floor: `minProtocolShareBps` is an immutable 2000 (20%) in both Rules and in tax A (constructor
     arguments; the on-chain wiring step checks it).
   - HookrFeeRouteRegistry owner: can retire conversion routes immediately and `sweepStray`. MILORD uses no route
     (`routeId = 0`, creator share paid in ETH), so retirement does not apply to it.
   - HookrPartnerRegistry owner: partners and payees (relevant only if candidate B were used).
5. HookrRoot's constructor also binds a "curated router" `0x8876789976dEcBfCbBbe364623C63652db8C0904` (the Universal
   Router in HOOKR_FINAL_CONFIG). Its code is Uniswap's, not Hookr's, and is not verified here.

## What remains unverified

- **The on-chain phase itself**: run the VPS command above. Until it passes, the chain code is trusted only through
  Hookr's record (and its Sourcify "exact match" claims).
- **Immutable values not covered by a getter**: the byte comparison takes every immutable from the chain. The wiring
  step checks the 23 that MILORD's behaviour depends on. The rest (for example code hashes of the router, quoter and
  lane, Lane/Recapture internals) are trusted through the record's `runtimeKeccak256` match, not re-derived from
  constructor arguments.
- **Tax-queue clones** (finding 3) and anything else created after launch (the MILORD token is ours).
- **Uniswap v4 PoolManager, PositionManager, Universal Router, Permit2, WETH**: not Hookr, not in the record, not checked.
- **Source provenance before hookr-contracts**: we verify that the deployed code equals this repository at the pin.
  Whether that repository equals Hookr's internal source (`--source-git` against `Hookr-fun/hookr`) cannot be checked
  publicly, and it does not matter for this purpose: the bytecode is what runs.
- **Audit**: matching bytecode proves identity, not safety. Hookr's README states that no independent audit of Hookr 1
  has been completed.

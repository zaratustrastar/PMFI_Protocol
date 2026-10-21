# Fork clock and the ArbSys stub

## What the stub is
An Anvil fork does not emulate Arbitrum's ArbSys precompile at 0x64 (the forked code there is Nitro's `0xfe`
placeholder). `MilordForkBase` etches a stand-in whose `arbBlockNumber()` returns `block.number`.

## Which MILORD results depend on it: none
Hookr-1 reads ArbSys only through `HookrClock.l2Block()`, which only HookrRecapture calls (recapture pools and King of
the Pool; `src/core/HookrRecapture.sol` lines 266 and 756 at 7e771b3). MILORD uses neither: HookrRules, HookrLauncher,
HookrDirectionalTax, HookrTaxQueue and HookrFamilyLock count in `block.number` and `block.timestamp` only.
Proof on the VPS: `run-fork-tests.sh` now runs the whole suite a second time with `FORK_NO_ARBSYS_STUB=true` (no
stand-in). Every result must match; a difference would mean a MILORD result relies on the simulated clock.

## What the fork does not reproduce: block.number
On Robinhood Chain `block.number` is the parent chain's height as the sequencer last saw it (Hookr assumes 12 s,
`PARENT_BLOCK_SECONDS`), advancing in steps and shared by many L2 blocks; `eth_blockNumber` is the L2 height (about 10
a second). On the fork, `block.number` is the L2 height and the tests set it with `vm.roll`. So the fork proves each
rule at a given `block.number`/`block.timestamp`, not how long those blocks take on mainnet.

| Result | Clock | Fork shows | On mainnet it depends on |
|---|---|---|---|
| Flat 1.5% tax, 20% commission, LP fee, FDV, 0 ETH, buyback, collector, Option D split, tax queues | none | final | nothing clock-related |
| Snipe value at time t (40% → 0 over 3,600 s) | `block.timestamp` since launch | final | L2 timestamps (seconds, monotonic) |
| Snipe ends at the guard, exact-out buys and outside LPs refused during the guard | `block.number < guardEndBlock` | logic | how long 300 parent blocks take: ~60 min if 12 s each. If they pass faster the Snipe stops early (below ~1% in the last minutes); if slower, the guard's restrictions last longer at a 0% Snipe |
| Bind check: decay ≤ guard × 12 s | `block.number` | accepted at 300 / 3,600 s | same arithmetic on mainnet (Hookr's constant) |
| Guard buy cap per parent block | `block.number` | not used (cap 0) | n/a for MILORD |
| Principal withdrawable at `principalLockedUntil` | `block.number` | exact block | wall time of 7,200 parent blocks (~24 h at 12 s) |
| Tax queue stale recovery | `block.timestamp` | not used (no route) | n/a |

## Still to validate on Robinhood Chain (read-only)
`RPC_URL=<robinhood rpc> L1_RPC_URL=<ethereum rpc, optional> node offchain/robinhood-clock-probe.mjs 30 600`
(read-only: `eth_getBlockByNumber`, `eth_call`). It reports seconds per parent block, the largest jump and longest
stall, how long 300 and 7,200 parent blocks actually took, the lag behind the real L1 head (with `L1_RPC_URL`), and
whether ArbSys equals the L2 height. Use the 300-block duration to set the guard if the Anti-Snipe candidate is
chosen, and the 7,200-block duration for the missed-handover alert thresholds. A first launch on Robinhood testnet
(46630) would show the same live.

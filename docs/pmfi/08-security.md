# Security

PMFI employs multiple layers of protection to safeguard user funds.

## Security Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    SECURITY LAYERS                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ON-CHAIN (Smart Contract Enforced)                              │
│  ─────────────────────────────────                              │
│  • Reentrancy protection on all operations                      │
│  • Safe token transfer handling                                 │
│  • Owner-only administrative functions                          │
│  • Emergency pause capability                                   │
│  • Conservation bounds (NAV can't drop too fast)                │
│  • 1% withdrawal fee                                            │
│  • Deposit caps per wallet and total                            │
│  • NAV signature verification                                   │
│  • Time-limited signatures (5 minutes)                          │
│  • Sequential roundId (no replay)                               │
│                                                                  │
│  OFF-CHAIN (Bot/Operator Controlled)                             │
│  ──────────────────────────────────                             │
│  • Withdrawal rate limiting (soft cap, not guaranteed)          │
│  • Position liquidation                                         │
│  • Cross-chain bridging                                         │
│  • Monitoring and alerts                                        │
│  • Manual intervention capability                               │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**Note:** On-chain protections are enforced by the smart contract and cannot be bypassed. Off-chain protections depend on bot uptime and operator availability.

## Smart Contract Protection

### Reentrancy Guard

Every function that moves money is protected against reentrancy attacks. This prevents malicious contracts from draining funds through recursive calls.

### Safe Token Transfers

All USDC transfers use battle-tested libraries that handle edge cases and non-standard token behaviors.

### Access Control

Only the contract owner can:
- Pause/unpause the vault
- Update configuration parameters
- Execute emergency withdrawals
- Make accounting corrections

### Pause Mechanism

The owner can pause the vault in emergencies:
- New deposits blocked
- New withdrawal requests blocked
- **Existing claims still work** (users can always exit)

## Oracle Security

### Why an Oracle?

The vault is on Base, but trading happens on Polygon. An oracle reports current asset values.

### Signature Verification

Every NAV update is cryptographically signed. The contract verifies:
- Signature is from authorized oracle
- Data hasn't been tampered with
- All fields are present and valid

### Time Limits

NAV signatures expire after 5 minutes. This prevents:
- Stale data being used
- Old signatures being replayed
- Timing-based manipulation

### Sequential Numbering

Each NAV update has an incrementing number. The contract only accepts higher numbers than previously seen, preventing replay attacks.

### Breakdown Verification

The contract checks that individual components (cash + positions + pending + in-flight) add up to the claimed total. Mismatches cause rejection.

## Economic Protection

### Conservation Bound

The NAV cannot drop more than a set percentage (e.g., 10%) below expected in a single update. This prevents:
- Oracle manipulation attacks
- Flash loan exploits
- Extreme pricing errors

If the bound is violated, operations pause until the issue is resolved.

### Withdrawal Fee

The 1% withdrawal fee isn't just revenue—it's security:
- Makes attack loops expensive
- Discourages rapid deposit/withdraw manipulation
- Creates a cost for any exploit attempt

### Deposit Caps

| Cap Type | Purpose |
|----------|---------|
| Per-wallet | Prevents single user from dominating vault |
| Total vault | Limits overall exposure |
| Limbo mode | Caps deposits when funds are bridging |

### Off-Chain Rate Limiting

The withdrawal servicer bot implements soft limits (currently $50,000/day) to prevent:
- Bank run scenarios
- Coordinated drain attacks
- Oracle manipulation leading to mass exits

**Note:** This is an off-chain control in the bot, not enforced by the smart contract. The contract allows any withdrawal amount if USDC is available.

## Operational Security

### Private Key Management

| Key | Purpose | Protection |
|-----|---------|------------|
| Oracle Key | Signs NAV data | Secure server environment |
| Trading Key | Executes trades | Isolated service |
| Owner Key | Contract admin | Hardware wallet |

### Off-Chain Infrastructure

PMFI relies on off-chain bots for:
- NAV calculation and signing
- Position liquidation
- Cross-chain bridging
- Order execution

**Important:** These services can experience downtime, bugs, or delays. The system is designed to fail safely (operations pause until fixed), but there is inherent risk in off-chain dependencies.

### Monitoring

Current monitoring includes:
- Telegram notifications for key events
- Logging of all bot operations
- Manual oversight of critical processes

### Emergency Controls

**On-chain (contract owner):**
- Pause deposits and withdrawal requests
- Claims always remain available (users can exit)

**Off-chain (operator):**
- Stop trading bot
- Halt bridging operations
- Manual intervention capability

## Attack Resistance

### NAV Manipulation

**Attack:** Try to inflate NAV, deposit, then withdraw at higher price.

**Defense:**
- Conservation bound limits NAV increases
- Withdrawal fee makes loop unprofitable
- Oracle signatures can't be forged

### Flash Loan Attacks

**Attack:** Borrow massive funds, manipulate markets, profit.

**Defense:**
- NAV comes from external oracle (not same transaction)
- Cross-chain bridging adds delay
- No instant liquidity to exploit

### Front-Running

**Attack:** See pending deposit, front-run to benefit.

**Defense:**
- NAV is pre-signed (can't be manipulated in mempool)
- Base chain has less MEV than mainnet
- Time limits ensure fair pricing

### Oracle Compromise

**Attack:** Hack the oracle to sign false NAV data.

**Defense:**
- Conservation bound limits damage per transaction
- Sequential numbering prevents mass replays
- Breakdown verification catches inconsistencies
- System can be paused immediately

## Bugs We've Fixed

### V7.3.4: Illiquid Position Inflation

**Issue:** Positions without buyers used theoretical prices.
**Impact:** NAV could be inflated by unsellable positions.
**Fix:** No bids = $0 value.

### V7.3.3: Cumulative Tracking Error

**Issue:** Used cumulative deposit total that never decreased.
**Impact:** After claims, phantom pending credit inflated NAV.
**Fix:** Use expected assets which decreases on claims.

### V7.3.3: Withdrawal Recalculation

**Issue:** Bot recalculated amounts instead of using locked values.
**Impact:** Wrong bridge amounts when NAV was corrupted.
**Fix:** Read actual locked amounts from requests.

### V7.3.2: Minimum Bridge Threshold

**Issue:** Amounts under $5 couldn't bridge, causing stuck funds.
**Impact:** Small remainders blocked forever.
**Fix:** Bump small amounts to $5 minimum.

## Audit Status

- ✅ Extensive internal testing and bug fixing
- ✅ Multiple production incidents resolved
- ⏳ Formal external audit pending
- ✅ Open source for community review

## What You Should Know

### Your Responsibilities

- Secure your wallet and private keys
- Verify contract addresses before interacting
- Understand the risks of prediction markets

### Our Responsibilities

- Maintain secure infrastructure
- Respond quickly to issues
- Be transparent about incidents
- Continuously improve security

### Inherent Risks

Despite all protections, risks remain:
- Smart contract bugs (mitigated by testing)
- Oracle failures (mitigated by time limits)
- Bridge exploits (external dependency)
- Prediction market volatility (inherent to strategy)

**Never deposit more than you can afford to lose.**

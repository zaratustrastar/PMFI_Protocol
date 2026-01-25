# How Pricing Works

PMFI uses Net Asset Value (NAV) to ensure fair pricing for all depositors and withdrawers.

## What is NAV?

NAV (Net Asset Value) is the total value of everything in the vault divided by the number of shares:

```
NAV per Share = Total Vault Assets ÷ Total Shares Outstanding
```

**Example:**
- Vault holds $100,000 in assets
- 95,000 shares exist
- NAV = $100,000 ÷ 95,000 = $1.053 per share

## Why NAV Matters

### For Depositors

When you deposit $1,000 at NAV = $1.053:
- You receive: $1,000 ÷ $1.053 = **950 shares**
- You own 1% of a $100,000 vault

This prevents:
- Late depositors buying in cheap after gains
- Early depositors getting diluted

### For Withdrawers

When you withdraw 950 shares at NAV = $1.20:
- You receive: 950 × $1.20 = **$1,140** (minus 1% fee)
- Your gain reflects actual vault performance

## What Counts as "Assets"?

```
┌─────────────────────────────────────────────────────────────────┐
│                    TOTAL ASSETS                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────────┐                                            │
│  │   CASH          │  USDC sitting in Polymarket account        │
│  │   $45,000       │                                            │
│  └─────────────────┘                                            │
│           +                                                      │
│  ┌─────────────────┐                                            │
│  │   POSITIONS     │  Market value of all prediction tokens     │
│  │   $50,000       │  (valued at what we could sell them for)   │
│  └─────────────────┘                                            │
│           +                                                      │
│  ┌─────────────────┐                                            │
│  │   IN-FLIGHT     │  USDC currently being deposited            │
│  │   $2,000        │  (at Polymarket deposit address)           │
│  └─────────────────┘                                            │
│           +                                                      │
│  ┌─────────────────┐                                            │
│  │   PENDING       │  USDC being bridged between chains         │
│  │   $3,000        │  (will arrive soon)                        │
│  └─────────────────┘                                            │
│           =                                                      │
│  ┌─────────────────┐                                            │
│  │   TOTAL         │                                            │
│  │   $100,000      │                                            │
│  └─────────────────┘                                            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Conservative Position Valuation

We don't use theoretical prices. We calculate what positions are actually worth.

### How We Value Positions

For each position, we ask: "If we sold this right now, how much would we get?"

We check the actual order book to see real buy orders, then simulate selling through them.

**Example:**

Position: 1,000 YES tokens

| Buyer Offering | Amount They'll Buy |
|----------------|-------------------|
| $0.50 | 300 tokens |
| $0.48 | 400 tokens |
| $0.45 | 500 tokens |

Selling 1,000 tokens:
- First 300 sell at $0.50 = $150
- Next 400 sell at $0.48 = $192
- Final 300 sell at $0.45 = $135
- **Total: $477**

This is more accurate than saying "1,000 × $0.50 = $500"

### No Bids = $0 Value

If a position has no buyers at all, we value it at **$0**.

Why? Because you literally cannot sell it. Using a theoretical price would inflate the NAV and hurt new depositors.

## The Oracle System

Since the vault is on Base but trading happens on Polygon, we need a system to report accurate values.

```
┌─────────────────────────────────────────────────────────────────┐
│                    NAV ORACLE FLOW                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. GATHER DATA                                                  │
│     • Check Polymarket cash balance (Polygon)                   │
│     • Check each position's orderbook                           │
│     • Check deposit address balance (Base)                      │
│     • Check vault contract state (Base)                         │
│                                                                  │
│  2. CALCULATE                                                    │
│     • Sum all asset states                                       │
│     • Apply conservative valuation rules                        │
│     • Compute NAV per share                                      │
│                                                                  │
│  3. SIGN                                                         │
│     • Cryptographically sign the NAV data                       │
│     • Include timestamp and expiry                              │
│                                                                  │
│  4. SUBMIT                                                       │
│     • User submits signed NAV with their transaction            │
│     • Contract verifies signature and data                      │
│     • Transaction executes at verified price                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Safety Mechanisms

### Time Limits

NAV signatures expire after 5 minutes. This prevents using stale prices.

### Sequential Updates

Each NAV update has a sequence number. The contract only accepts higher numbers, preventing replay attacks.

### Conservation Bound

The NAV cannot drop more than a set percentage (e.g., 10%) below expected. This prevents oracle manipulation.

### Breakdown Verification

The contract checks that all the pieces (cash + positions + pending + in-flight) actually add up to the claimed total.

## Price Protection for Withdrawals

When you request a withdrawal, the NAV at that moment is locked:

| Without Locking | With Locking (PMFI) |
|-----------------|---------------------|
| Request at $1.00 NAV | Request at $1.00 NAV |
| Wait for liquidation | Wait for liquidation |
| NAV drops to $0.90 | NAV drops to $0.90 |
| Receive $0.90/share ❌ | Receive $1.00/share ✅ |

You always get the price you saw when you requested.

## Frequently Asked Questions

**Q: How often is NAV updated?**
A: Fresh NAV is calculated for each deposit/withdrawal transaction.

**Q: What if the oracle goes down?**
A: Deposits and withdrawals pause until a valid NAV can be provided.

**Q: Can the oracle lie about NAV?**
A: Multiple safety checks (conservation bound, breakdown verification) make manipulation very difficult.

**Q: Why not just use Polymarket's API prices?**
A: API prices can be stale or theoretical. Orderbook-based valuation reflects reality.

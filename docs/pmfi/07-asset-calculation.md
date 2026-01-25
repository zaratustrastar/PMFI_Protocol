# Asset Tracking

PMFI uses a sophisticated system to track funds across multiple chains and states. This ensures accurate NAV calculation and prevents any money from being "lost" during transfers.

## The Challenge

Traditional vaults are simple: assets = balance in the contract.

PMFI is more complex because:
- Trading happens on Polygon
- The vault contract is on Base
- Funds bridge between chains
- Positions have varying liquidity

We solved this with **3-state asset tracking**.

## The Three States

Every dollar in the system exists in exactly one of these states:

```
┌─────────────────────────────────────────────────────────────────┐
│                    3-STATE ASSET MODEL                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  STATE 1: IN-FLIGHT                                              │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  Location: Polymarket deposit address on Base                ││
│  │  Duration: 0-5 minutes (swept frequently)                    ││
│  │  What it is: Fresh deposits waiting to be processed          ││
│  └─────────────────────────────────────────────────────────────┘│
│                          ↓                                       │
│  STATE 2: PENDING CREDIT                                         │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  Location: Bridging infrastructure (Base → Polygon)          ││
│  │  Duration: 5-20 minutes typically                            ││
│  │  What it is: Funds in transit, not yet visible in PM         ││
│  └─────────────────────────────────────────────────────────────┘│
│                          ↓                                       │
│  STATE 3: CREDITED ASSETS                                        │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  Location: Polymarket account on Polygon                     ││
│  │  Duration: Ongoing (until withdrawal)                        ││
│  │  What it is: Cash + positions available for trading          ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                  │
│  TOTAL ASSETS = In-Flight + Pending + Credited                   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## The Bucket Invariant

**Key Principle**: Every dollar lives in exactly one bucket at any time.

This prevents double-counting and ensures accurate totals.

```
DEPOSIT JOURNEY:

$1,000 deposit
    ↓
[In-Flight: $1,000] [Pending: $0] [Credited: $0] = Total: $1,000
    ↓ (swept)
[In-Flight: $0] [Pending: $1,000] [Credited: $0] = Total: $1,000
    ↓ (bridged)
[In-Flight: $0] [Pending: $0] [Credited: $1,000] = Total: $1,000

At every step, total = $1,000. Nothing lost, nothing duplicated.
```

## Credited Assets Breakdown

Once funds arrive in Polymarket, they're tracked as:

### Cash
USDC sitting in the Polymarket account, ready to trade or withdraw.

### Positions
Prediction tokens the vault holds. Valued at liquidation price (what we could actually sell them for).

### Reserved
USDC locked in open buy orders. Technically ours, but committed to potential trades.

```
CREDITED ASSETS:

┌─────────────────────────────────────────────────────────────────┐
│                                                                  │
│  Cash: $45,000                                                   │
│  (Free USDC in Polymarket)                                      │
│                                                                  │
│  Positions: $50,000                                              │
│  (YES/NO tokens at liquidation value)                           │
│                                                                  │
│  Reserved: $5,000                                                │
│  (Locked in open buy orders)                                    │
│                                                                  │
│  ─────────────────────────────────                              │
│  Total Credited: $100,000                                        │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## How Pending Credit Is Calculated

We can't directly see funds that are bridging. Instead, we calculate them:

```
Pending = Expected Assets - Cash - Positions - In-Flight - Vault Buffer
```

Where:
- **Expected Assets** = Contract's record of what should exist
- **Cash** = Actual Polymarket balance
- **Positions** = Liquidation value of tokens
- **In-Flight** = Balance at deposit address
- **Vault Buffer** = USDC in vault contract

The "gap" between what should exist and what we can see = funds in transit.

## Position Valuation

Not all positions are valued equally.

### Liquid Positions

If buyers exist, we simulate selling through the orderbook:

| Buyers | Quantity | Our Position | What We'd Get |
|--------|----------|--------------|---------------|
| $0.50 | 300 | Sell 300 | $150 |
| $0.48 | 400 | Sell 400 | $192 |
| $0.45 | 500 | Sell 300 | $135 |
| **Total** | | **1,000** | **$477** |

### Illiquid Positions

If no buyers exist, the position is valued at **$0**.

Why? Because:
- Theoretical prices mean nothing if no one will buy
- Including phantom value inflates NAV
- This protects new depositors from overpaying

## Withdrawal Tracking

When withdrawals happen, a fourth state appears:

### Vault Buffer

USDC that has been bridged back to Base and sits in the vault contract, ready for claims.

```
WITHDRAWAL JOURNEY:

Positions sold on Polygon: $5,000
    ↓
Cash in Polymarket: $5,000
    ↓ (bridged to Base)
Vault Buffer: $5,000
    ↓ (user claims)
User wallet: $4,950 (after 1% fee)
```

## Why This Matters

### For Depositors

Your NAV reflects real, verifiable assets. You're not buying into:
- Phantom pending credits
- Overvalued illiquid positions
- Double-counted funds

### For Withdrawers

Your locked price is based on actual assets. The vault knows exactly:
- How much cash is available
- What positions need selling
- When funds will arrive

### For Everyone

Complete transparency. At any moment, you can see:
- Where every dollar is
- What state it's in
- How it contributes to NAV

## Historical Bugs We Fixed

### V7.3.3: The TotalForwarded Bug

**Problem:** Used cumulative deposits to calculate pending, but this never decreased when users withdrew.

**Result:** After withdrawals, phantom pending credit appeared, inflating NAV to $6/share!

**Fix:** Use "expected assets" which correctly decreases when users claim.

### V7.3.4: Illiquid Valuation

**Problem:** Positions without buyers used theoretical API prices.

**Result:** Worthless positions showed false value, inflating NAV.

**Fix:** No bids = $0 value. Conservative, but accurate.

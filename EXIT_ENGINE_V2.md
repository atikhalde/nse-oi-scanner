# EXIT ENGINE v2 — setup-aware SL/trailing (user spec)

Live from: see git history. Backtest driver: 22-Jul-2026 forward paper test
(70 real signals). Engine signals are unchanged — only the trade manager
(`trader.py`) changed.

## Setup classification (at entry bar)
| Setup | Condition (BUY mirror for SELL) |
|---|---|
| **BREAKOUT** | close within 0.30×ATR(14) of the 20-bar / day high |
| **PULLBACK** | close above EMA20 and the last ~6 bars dipped into the EMA20 zone (0.3×ATR) and held |
| **CONTINUATION** | deep in trend (riding EMAs), not at a fresh extreme |
| **REVERSAL** | entry against the day's move, near the opposite day extreme (and beyond EMA20) |

## Initial SL — always at true structure, never artificially tightened
| Setup | SL |
|---|---|
| Breakout / Continuation | last confirmed 5-min swing (2+2 pivot) ∓ 0.02% |
| Pullback | pullback low/high of last ~6 bars ∓ 0.02% (tight, low-risk) |
| Reversal | day extreme ∓ 0.02%; if extreme > 2×ATR away, SL = 1.5×ATR from entry |

## ₹1,000 max-loss cap (user)
Planned loss per trade ≤ **₹900** (₹1,000 minus ₹100 gap/slippage buffer).
`qty = floor(50000/entry)`; if `qty × risk_pts > 900 → qty = floor(900/risk_pts)` (min 1).
The SL price itself is never moved from structure.

## Targets + trailing
- **50% booked at +2R**, **30% booked at +3R**, final **20%** trails.
- Trail arms once the trade prints **+1R** (MFE), applied from the next bar:
  - Breakout / Pullback → confirmed 5-min swing structure
  - Continuation → 5-min EMA9
  - Reversal → 2×ATR chandelier from best price
- SL checked **first inside a bar** (gap-through fills at open); TP fills exact.
- Square-off at 15:20 bar close.

## Verified replay (22-Jul-2026, real bars, 1-open-trade rule)
| | OLD exits | Exits v2 |
|---|---|---|
| M1 (26 tr) | ₹11,581 · +19.5R | **₹9,930 · +16.9R (18W)** · worst −₹869 |
| M2 (44 tr) | ₹3,681 · +3.3R | **₹4,102 · +2.7R (23W)** · worst −₹889 |

Notes: pullbacks lose ~57% less; reversals-in-breakouts save/harvest better
(ICICIPRULI −435→−25, PIIND −322→+300, VMM +262→+590); a few strong runners
give back more on the structure trail (VOLTAS, SRF, MPHASIS, PGEL).
Tests: `python3 test_trader.py` (18 checks) + engine self-test stays green.

## Policy addendum (24-Jul): B2 quality filter

- Trade only chart signals: NORMAL (80/280) + name-number EX1..EX8 + bare BUY-EX (base exception).
- Blocked all day: weak variants name >= EX9 (buy codes 102,108-112 | sell codes 210-220).
- Blocked before 09:45: strictest class (101 BUY-EX, 201 SELL-EX1, 202 SELL-EX2) - razor-edge open class.
- Buy codes are name-offset: 101=BUY-EX, 102=BUY-EX17, 103..112=BUY-EX4..13; sell 201..220=SELL-EX1..19.

## 24-Jul: exits v3 — pure runner (user-approved)

- NO fixed targets. Structure SL ∓0.02%; full qty rides structure-swing trail after +1R to 15:20.
- Sizing: MIS 5× — ₹10,000 margin → ₹50,000 notional per trade; qty ≤ notional, shrunk to ₹900 planned risk (₹1,000 hard cap − buffer).
- 2-day evidence: NET ₹8,033 vs ₹6,482 (partials) on same 57 final-rule entries; capture 34%→41%+ of winner MFE.
- BE floor stays dormant (no TP1 to arm it); trail ratchets only with confirmed swing pivots.

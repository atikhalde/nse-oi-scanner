# M3 / M4 — OPTIONS BUYING paper models (approved spec 23-Jul-2026: 1B 2A 3A 4A 5A)

Mirrors of M1 (`live_runner`) / M2 (`m2_runner`): **identical entries** — same intact
master engine, 09:26 chart gate, 90/290 block, B2 quality filter (EX9+ blocked,
BUY-EX/SELL-EX1/SELL-EX2 from 09:45), same OI-spurt gates, 1-open-trade-per-stock.

Position per signal
- BUY → nearest-expiry **1st-ITM CE**, SELL → **1st-ITM PE** (Dhan chain, exchange expiry list).
- Entry premium = option LTP at signal-bar close (paper).

Risk / exits
- UL SL = signal-bar low/high ∓ 0.02% buffer. Premium risk R = |delta| × UL-distance (set once).
- **Skip** the trade if ONE lot's risk > ₹1,900 (₹2,000 hard cap − ₹100 buffer) or 1-lot outlay > ₹50,000.
- Lots = max s.t. risk ≤ ₹1,900 AND premium outlay ≤ ₹50,000.
- **TP1 = 1:1 on premium → book 50%**. Remaining 50% trails on **underlying structure swings**
  (5-min pivot trail ±0.02%); underlying crosses trail stop → exit option at that bar's close.
- Square-off 15:20 IST. Costs NOT included (like other models).

Data ladder at runtime: Dhan chain LTP/greeks → option 5-min candles (OPTSTK intraday)
→ Black-Scholes synthetic marks (frozen IV/delta from entry) for any quote outage.
Synthetic fills are always labeled `prem_src: synth`.

Files: `options_common.py` (engine), `m3_runner.py` (state3.json), `m4_runner.py` (state4.json),
workflows `5_live_m3.yml`, `6_live_m4.yml`, tests `test_options.py` (17 checks).

Known trade-off (2-day sim, BS-priced, real lots/strikes, 30 of 57 B2 entries tradeable):
options = high convexity on trending days (+₹16,088 on 23-Jul) vs premium-noise bleed on
choppy days (−₹3,065 on 22-Jul); ~45% of signals skip on the ₹2,000/1-lot cap. Treat M3/M4
as research until ~1 week of live paper data.

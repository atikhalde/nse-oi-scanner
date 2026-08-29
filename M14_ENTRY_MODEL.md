# M14 Ultra High-Conviction A+ — Standalone Entry Model

**Status:** Research & Production Paper Model  
**Universe:** 210 `fno_universe.csv` NSE F&O underlying stocks  
**Target Trade Frequency:** **1 to 3 max entries per day** (hard daily safety ceiling: 3). Zero trades on non-aligned/choppy days is expected and valid.  
**Accuracy & Risk Profile:** High win rate, positive expectancy after full realistic transaction costs and slippage (brokerage + STT + txn charges + GST + fill haircut), max 2 concurrent open positions.

---

## 1. Executive Summary & Thesis

M14 was created following a deep empirical audit of all 13 prior entry models (M1–M13) and 222 paper test report files.

### Key Learnings from Prior Models
1. **Over-Trading Trap**: High-frequency models (M1, M2, M5, M6, M7, M8, M10, M11) executed between 10 and 140+ trades per day. Small edge win rates (34%–48%) combined with high trade volume resulted in transaction fees and slippage destroying net profitability.
2. **Operational Latency & Staleness Outages**: Diagnosis of M12 and M13 revealed that strict age limits (`MAX_SIGNAL_AGE_MIN = 5.0`) killed 100% of qualified candidates when runners were dispatched on 15–30 minute cadences. Furthermore, stale history files (truncated at intraday stubs) caused previous close baselines to compare against 10-day-old prices, causing valid signals to fail anti-chase and breadth gates.
3. **Multi-Gate Synergy**: High-conviction setups require combining **top OI spurt rank**, **causal price setups (S1 Morning Base / S3 Flag Breakout)**, **same-clock relative volume**, **market regime alignment**, **strong candle close (CLV >= 0.60)**, and **anti-chase displacement limits**.

M14 synthesizes these lessons into a single ultra-selective model designed to capture only 1–3 A+ momentum scalps per day.

---

## 2. Frozen Hard Rules

Every M14 candidate must satisfy **all 10 mandatory rules** at the signal-bar close using data available up to that timestamp:

### 1. Instrument & Time Window
- Liquid NSE F&O underlying equity stock (MIS 5x leverage).
- Signal close time must be between **09:45 IST and 11:30 IST** (the peak morning trend window).
- Signal must be evaluated within **5.0 minutes** of bar close (`MAX_SIGNAL_AGE_MIN = 5.0`). Old/queued signals are rejected.

### 2. Whitelisted Real Master Variants
- Signal code must belong to the verified real chart whitelist:
  - **BUY**: `BUY-EX17` (102), `BUY-EX5` (104), `BUY-EX7` (106), `BUY-EX` (101).
  - **SELL**: `SELL-EX8` (209/208), `SELL-EX12` (212/213), `SELL-EX1` (201), `SELL-EX2` (202), `NORMAL SELL` (280).
- Scanner preview codes `90` (`ENTRY BUY`) and `290` (`ENTRY SELL`) are strictly blocked.

### 3. Top OI Spurt Rank
- Candidate must be ranked in the **Top 10 OI Spurts** (`spurt_rank <= 10`) at signal time.

### 4. Causal Video Setup Confirmation
- At least one video setup must be active on the signal bar:
  - **S1 Morning Base** (BUY: 09:15-09:45 low is not breached; SELL: 09:15-09:45 high is not breached).
  - **S3 Flag Breakout** (consolidation flag breakout).
  - **S2 Pivot Pullback** (pullback to prior session daily pivot or EMA20).

### 5. Volume Confirmation
- Same-clock relative volume `clock_relvol >= 1.0` (signal-bar volume >= 1.0x 20-session median at the exact same clock time).

### 6. Market Regime & Breadth Alignment
- **BUY**: Opening market breadth >= 52% (or live breadth >= 50%) AND market regime is NOT `TREND-DOWN` or `V-REVERSAL / bear-trap`.
- **SELL**: Opening market breadth <= 48% (decline ratio >= 52% or live decline ratio >= 50%) AND market regime is NOT `TREND-UP` or `V-REVERSAL / bull-trap`.

### 7. Anti-Chase & Anti-Crowding
- Side-normalized price displacement from previous close:
  `-1.00% <= s × (close / prev_close − 1) × 100 <= +1.50%`
  (Rejects chasing extended moves beyond +1.50%).
- Sector breadth vs previous close <= 0.45 (sector is not crowded).

### 8. Candle Quality / CLV
- **BUY**: Candle Location Value `(close - low) / (high - low) >= 0.60` (strong close near high).
- **SELL**: Candle Location Value `(high - close) / (high - low) >= 0.60` (strong close near low).

### 9. VIX Gate
- Immediately preceding session India VIX return > **-2.0%** (no trading following violent VIX collapse).

### 10. Composite A+ Quality Score Floor
- Composite A+ score >= **70.0 points** (out of 100).

---

## 3. Portfolio Controls & Sizing

- **Daily Cap**: Maximum **3 trades per day** (hard safety ceiling).
- **Max Concurrent**: Maximum **2 open positions** at any time.
- **Symbol Cap**: Maximum **1 trade per symbol per day**.
- **Sector Cap**: Maximum **1 trade per sector per day**.
- **Side Cap**: Maximum **3 trades per side per day**.
- **Daily Loss Limit**: Stop taking new entries after 2 full-risk losses.
- **Sizing**:
  - MIS 5x leverage: ₹10,000 margin -> **₹50,000 notional per trade**.
  - Rupee Risk Budget: **₹900 max planned risk** (`qty = min(50000/entry, 900/risk_pts)`).

---

## 4. Operational & Data Baseline Fixes

M14 incorporates the critical data freshness fixes resolved in this audit:

1. **Fail-Closed Previous Close Baseline**:
   - `load_prev()` requires `date == expected_previous_weekday`. If history or cache is stale or missing, `status: STALE` is returned and M14 prints `M14 STRICT NO-ENTRY: previous closes are stale/unavailable`.
2. **Complete EOD Cache Seeding**:
   - `seed_prev()` seeds `data/m14_prev_context.json` using complete session data (capturing completed 15:25 bars or final bar of the day for >= 180 stocks).
3. **Decoupled Workflow & History Push**:
   - Workflow `2. Bootstrap` commits refreshed `data/` immediately after history download, preventing frozen history files.
4. **Three-Bot Telegram Alerts**:
   - Fanout to main bot and Target A / Target B targets with strict at-most-once reservation (`reserve_once`) to eliminate duplicate or missed notifications.

---

## 5. File Architecture

- `m14_entry.py` — Pure entry rules, causal features, ranking, and decision engine.
- `m14_trader.py` — Trade execution, risk caps, structure SL ∓0.02%, trailing, and exit engine.
- `m14_runner.py` — Live paper runner, state management (`state14.json`), candidate logging (`learn/m14_candidates_*.csv`), raw logging (`learn/raw_M14_*.csv`), EOD Excel report (`paper_test_M14_*.xlsx`), and Telegram alerts.
- `m14_alerts.py` — Three-target Telegram alert dispatcher.
- `test_m14_entry.py`, `test_m14_trader.py`, `test_m14_runner.py` — Comprehensive unit test suites.
- `.github/workflows/14_live_m14.yml` — Independent GitHub Actions workflow.
- `analysis/evaluate_m14.py` — Chronological replay evaluator.

---

## 6. Commands & Execution

```bash
# Run unit tests
python test_m14_entry.py
python test_m14_trader.py
python test_m14_runner.py

# Test Telegram alerts
python m14_runner.py --test-alert

# Run replay evaluator
python analysis/evaluate_m14.py

# Run live paper runner cycle
python m14_runner.py --loop 1
```

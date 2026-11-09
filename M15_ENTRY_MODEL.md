# M15 — PERSIST + MASTER 5m Entry Model (paper-only)

**Source:** `Persist-check` @ `175b868dd80c63d75abd6408eb431847d7595e97` → `nse_scanner/` (full port of two TradingView engines).

**Why:** After deleting M7–M14 (frozen history, Yahoo 15:20 floor, 429 storms), the only surviving live path was the heartbeat scanner with:
- fresh 5m Yahoo (1m-repaired 09:15 volume),
- `WAIT` on CHOP/NEUTRAL/VIX-spike,
- persistent-sector complement to Nifty,
- at-most-once per symbol per IST day,
- close-confirmed EMA9 trail.

This model is that scanner, isolated as `M15` inside `nse-oi-scanner` so it shares secrets, history, and paper plumbing but keeps its own state/ledger.

## 0. Data feed — the shared M1–M6 pipeline (no yfinance)

M15 reads every bar through the **same `feeds.py` ladder as M1–M6** (live_runner /
m2_runner / m5_runner / m6_runner / fast_feed). `nse_scanner/data.py` no longer
calls `yfinance`; it is a thin adapter over the shared feed:

| Interval | Ladder |
|---|---|
| 5m | `feeds.fetch_bars_yahoo(interval="5m")` → Dhan 5m (`DHAN_TOKEN` + `fno_universe.csv` securityId) → committed `data/history/*.csv` |
| 1m | `feeds.fetch_bars_yahoo(interval="1m")` → Dhan 1m — requested **only** for symbols with a `Volume=0` 5m bar |
| 1d | committed `data/history/*.csv` (local, zero network) → Yahoo daily — indices (Nifty/VIX/15 sectors) always use Yahoo daily |

Notes:

- Yahoo is reached with the same cookie + crumb session, `query1 → query2`
  host retry and 401 re-handshake as M1–M6 (`feeds.fetch_bars_yahoo` now also
  accepts `^NSEI` / `*.NS` tickers and `1m`/`1d` intervals).
- `data/history/*.csv` is the 5m history bootstrapped by
  `live_runner.py --bootstrap` and committed by workflow 2 — the same
  local-history-first source M2/M5/M6 use. Daily context therefore survives a
  Yahoo 429 storm, and local hits skip the inter-symbol sleep.
- One dead symbol returns an empty frame; it never aborts the sector scan.
- Every cycle stores a `feed` audit in `state/state15.json`
  (`yahoo_calls`, `dhan_calls`, `history_calls`, `symbols`, `sources`,
  `fallback`) and prints a `FEED ...` line in the Actions log.
- Knobs: `M15_FEED_SLEEP` (default `0.05s` between network symbols),
  `FEED_PERIOD_5M`/`FEED_PERIOD_1D` (legacy `YF_PERIOD_*` still honoured).
- `pip install -r requirements.txt` no longer installs `yfinance`.

## 1. Engines

### L1 — Market + Sector Brain (`NIFTY Institutional Core + Flow v4.0`)

Inputs: `^NSEI` (Nifty), `^INDIAVIX`, 15 sector indices (AUTO, FIN, IT, MET, DEF, ENE, FMCG, OIL, PHA, PSE, PSUBK, BANK, REALTY, CDUR, MEDIA).

Computes per sector: `day_ret`, `intra_ret`, `RS`, `pos`, `recent`, `hold_bull/bear`, `bull/bear`, `persist_bull/bear`, `side`, `score`. Persist = smoothed 8/5 multi-bar leadership.

Commit logic (Pine-faithful):
- Nifty direction (`BULL/BEAR/NEUTRAL/CHOP`) via VWAP, EMA9/21/50, MTF 15/30/60m vs EMA20, ADX ≥20/25, opening-range break, concrete commits, VIX spike filter.
- Live flow `Q/uQ` + score.
- Breadth: 15-sector advance/decline, regime `TREND UP/DOWN`, MTF `3/3`.
- **Complementary-sector lock:** Only the strongest `PERSIST +` (for BULL) or `PERSIST −` (for BEAR) that still has correct RS sign and is not qualified the opposite way is kept. Else `WAIT`.

No pair-trades, no “strongest bull while Nifty bear”.

### L2 — Stock 5m Master Sector Batch Scanner

Runs **only on the picked sector’s full batch** (both TV batches — Python has no 12-symbol cap).

Per 5m bar:
- HTF `15m` + `daily` via `lookahead_on` on completed days (every 5m inside a completed 15m/daily sees that HTF bar’s final OHLC).
- Dual VWAP, first-hit + rank upgrade, 90/290 cleared after 09:45 (and 90/290 do not lock later exceptions).
- Interior 5m gaps filled; trailing 15:15–15:25 drops not invented.
- Yahoo 09:15 `Volume=0` repaired from 1m sums when 1m history exists.
- 22-condition EX codes: `BUY-EX` (80/90-112) vs `SELL-EX` (200-290) with W1/W2/W3 windows; FIN special windows.

**Eligibility:** Only **newly-fired entries from the latest completed 5m candle** → Telegram. Each stock once per IST day; rank upgrades are not re-sent.

## 2. Risk

- Entry = signal candle close (2-decimal).
- EMA9 includes that completed candle; SL = EMA9 × (1 ∓ 0.0002) rounded outward to 0.01 (BUY: floor, SELL: ceil).
- `risk = |entry − sl|`; T1/T2/T3 = 1/2/3 R. If SL on wrong side or no EMA9 → skip (no invalid alert).
- SL/targets are **reference levels**, not broker orders.
- Positions carry across sessions if `state/state15.json` survives; EMA9 trail alert is independent of later sector/WAIT.

## 3. State & Dedupe

`state/state15.json` (`state15.json` legacy alias also syncs):

```json
{
  "session": "2026-11-09",
  "market": {"dir_state": 1, "label": "BULL", "conf": 74, "regime": "TREND UP"},
  "sector": {"name": "IT", "side": 1, "reason": "PERSIST + IT qualified (78)"},
  "sent": {"INFY": {"code": 101, "name": "BUY-EX", "time": "10:30", "price": 1842.5}},
  "positions": {"INFY": {"side": "BUY", "entry": 1842.5, "sl": 1838.28, "entry_ema9": 1838.65, "entry_bar": "2026-11-09T10:30:00+05:30", "last_bar": "2026-11-09T10:30:00+05:30", "t1": 1846.72, "t2": 1850.94, "t3": 1855.16}},
  "alerts": [],
  "feed": {"yahoo_calls": 61, "dhan_calls": 0, "history_calls": 20, "symbols": 59,
           "sources": {"yahoo": 39, "history": 20, "none": 0}, "fallback": null}
}
```

- `reset_if_new_day` clears `sent`/`alerts` but keeps `positions`.
- `InvalidStateError` fails closed — corrupt ledger never resends.
- `MAX_ALERT_LAG_SECONDS=180` (default 3m) — stale closed bars are skipped, not backfilled (prevents historical replay).

## 4. Telegram

```
MARKET BULL  conf 74% STRONG
Nifty 24810.4  day +0.42%  intra +0.31%
VIX 12.80 ↓ -3.10%   ADX 26   MTF 3/3
Breadth 11▲/4▼  TREND UP
...
SECTOR PICK: IT  BULL
PERSIST + IT qualified (score 78)
Action: SCAN LONG

🟢 BUY-EX  INFY
Sector IT  |  Market BULL (74%)
Entry (candle close) 1842.50
Entry EMA9 1838.6500  |  SL 1838.28 (0.02% below EMA9)
Risk 4.22  |  T1 1:1 1846.72  T2 1:2 1850.94  T3 1:3 1855.16
Trail: alert on first completed 5m close below EMA9 (even after T3).
Score 68 Strong   base 20/21  cond22=0  score=68
5m bar 10:25–10:30 IST (signal confirmed at close)
Signal calculated 10:31:12 IST

🔔 EMA9 TRAIL EXIT — BUY  INFY
5m bar 2026-11-09 11:00–11:05 IST closed below EMA9
Close 1849.00  |  EMA9 1850.2000
Entry 1842.50  |  Initial SL 1838.28
```

Fanout: primary `TELEGRAM_BOT_TOKEN/_CHAT_ID` + optional `TELEGRAM_BOT_TOKEN_2/_CHAT_ID_2` + `M15_BOT_TOKEN_A/B` (falls back `M14→M13→M11` vault pairs).

## 5. Operation

```bash
python -u m15_runner.py --dry-run              # print, no Telegram
python -u m15_runner.py --wait-for-bar --loop 1  # live cycle (Actions)
python -u m15_runner.py --sector IT --dry-run  # force sector
python -u m15_runner.py --backfill --dry-run   # diagnose last session
python -u m15_runner.py --test-alert           # fanout check
python -m nse_scanner backtest --date 2026-11-06 --dry-run  # replay
```

Actions: `.github/workflows/15_live_m15.yml` — crons `52,57 3 * * 1-5`, `2,7,12,... 4-9 * * 1-5`, `2 10 * * 1-5` (09:22–15:32 IST, 2m after each 5m close). `MAX_ALERT_LAG_SECONDS=180`, `run_until_ready` retries Yahoo publish within deadline.

## 6. Honest latency

A 5m entry is known only after candle close (OPEN-stamped). Yahoo NSE (via `feeds.py`) can lag/429. Actions cron can queue. Neither guarantees on-time alert. Lowest delay: `python -m nse_scanner live` on a Mumbai VPS with persistent `state/scan_state.json`. Actions is backup heartbeat.

## 7. Files added

- `nse_scanner/` (12 modules), `config/universe.yaml`, `m15_runner.py`, `m15_alerts.py`, `M15_ENTRY_MODEL.md`, `.github/workflows/15_live_m15.yml`, `test_m15_runner.py`, `test_m15_data_feed.py`
- M15 feed integration: `nse_scanner/data.py` (rewritten onto `feeds.py`), interval/ticker support in `feeds.py`, lazy 1m repair + `st["feed"]` audit in `nse_scanner/pipeline.py`, `yfinance` dropped from `requirements.txt`
- State: `state/state15.json` + alias `state15.json` (git-committed by workflow)

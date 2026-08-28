# M12 / M13 outage — full breakdown

**Audit date:** 28-Aug-2026 · **Branch evidence:** `state12.json`, `state13.json`, `learn/m12_candidates_*.csv` (1,599 rows / 11 sessions), `learn/m13_candidates_*.csv` (1,735 rows / 12 sessions)
**Reproduce:** `python -u diagnose_m12_m13.py`

---

## 1. Verdict

**Nothing is broken in the Telegram path, and the entry models are not "finding no setups". M12 and M13 cannot physically fire on the schedule they are actually being run on, and the price baseline both models gate on is 10 calendar days old.**

Two defects stack. **RC1 alone fully explains the silence** — every fully-qualified candidate M12 produced in 11 sessions (10 of them) was discarded for lateness, and 2 of M13's 3 qualified candidates were too (the third, on 25-Aug, is the only alert either model has fired). RC2 then corrupts the gates that generate those candidates at all, so even a correct cadence would be firing on the wrong numbers.

| # | Defect | Effect |
|---|---|---|
| **RC1** | Both runners enforce `MAX_SIGNAL_AGE_MIN = 5.0`, but they are being dispatched every **30 min (M12)** and **15 min (M13)** — the in-repo 5-minute `schedule:` produced **0 runs on 28-Aug** | 82.1% (M12) and 66.8% (M13) of recorded signals exceed the 5-minute deadline — and **100% of the candidates that passed every other gate were killed by exactly this rule** (10/10 M12, 2/3 M13) |
| **RC2** | `data/history/` is frozen at **2026-08-18**, and its last bar is a **09:34 intraday stub**, not a close | Every previous-close-derived gate (M12 anti-chase, M12 sector breadth, M13 opening breadth, M13 pivots, both `engine_frame` ATR/EMA warmups) is computed against the wrong number |
| **RC3** | Cursor reset (`if known > n: known = 0`) replays the whole session inside one job when a later cycle returns fewer bars than the cursor expects | Single cycles re-evaluate 4–14 bar-times per symbol at ages up to **425 min** (M12, 18-Aug) and **511 min** (M13, 19-Aug) → job overrun → the *next* cycle is late too. The three worst fresh-share days (12%/19%/4% for M12) are precisely the replay days |

RC2 exists because **workflow `2. Bootstrap` is being cancelled at its 55-minute timeout while `label_learn.py` grinds**, so the commit step that would publish the refreshed history is skipped. That is the upstream domino.

### Differential diagnosis — delivery is fine

`state11.json` (same workflow family, same secrets, same day):

```
state11.json   alerts: 97   ENTRY alerts: 47   ✓ firing
state12.json   alerts: 2    ENTRY alerts: 0    ✗ (EOD summary + EOD document only)
state13.json   alerts: 2    ENTRY alerts: 0    ✗ (EOD summary + EOD document only)
```

`telegram_bot.py`, `m13_alerts.py`, the three-target fanout and the at-most-once registry all behave correctly — M13 fired a real entry alert for `RVNL SELL 10:30` on 25-Aug. **M12/M13 are silent because `dispatch()` never reaches `send_message`, not because `send_message` fails.**

---

## 2. M12 Selective Reversion — gate-by-gate

1,599 recorded candidates over 11 sessions. Pass rates are recomputed **independently for every gate** from the features stored in the ledger, because `m12_entry.decide()` short-circuits and its `model_reason` only reports the *first* failing gate (which is why the state-level counters made the whitelist look like the whole story).

| Frozen rule | Rows passing | Notes |
|---|---:|---|
| 1. `code` ∈ {102, 201, 202, 209, 213, 216, 220, 280} | 744 / 1,599 (46.5%) | 855 killed here. `101` alone accounts for 267 rejections — it is the *most frequent* real signal in the scanner and is excluded by design |
| 2. time in 09:45–12:00 | 1,000 / 1,599 (62.5%) | Correct-by-design loss |
| 3. anti-chase `dir_prev_pct` ∈ [−1.00%, +0.20%] | **204 / 1,599 (12.8%)** | **Poisoned by RC2** — see §4 |
| 4. `sector_breadth_prev_dir` ≤ 0.43 | **353 / 1,599 (22.1%)** | **Poisoned by RC2**; 15/144 rows on 28-Aug sit at exactly `1.000` (saturated) |
| 5. ≥ 1 video setup (S1–S4) | 908 / 1,599 (56.8%) | |
| 6. `clock_relvol` ≥ 1.0 | 1,094 / 1,599 (68.4%) | |
| **All six simultaneously** | **10** | |
| …that are also ≤ 5 min old | **0** | |

**Every single fully-qualified M12 signal in 11 sessions was rejected by the freshness rule:**

```
day         symbol       side  signal        time   score  age(min)
2026-08-12  TATASTEEL    SELL  SELL-EX8      09:55   79.0     8.38
2026-08-12  NMDC         SELL  NORMAL SELL   11:25   76.0     5.52
2026-08-17  M&M          SELL  SELL-EX1      09:50   79.0     5.12   ← 7 seconds over the limit
2026-08-17  ASIANPAINT   SELL  SELL-EX1      10:20   78.0     6.70
2026-08-19  SUNPHARMA    BUY   BUY-EX17      10:15   80.0    16.00
2026-08-27  KFINTECH     SELL  SELL-EX8      10:00   76.0    30.87
2026-08-27  PHOENIXLTD   SELL  SELL-EX2      10:45   75.0    16.58
2026-08-27  BAJAJFINSV   SELL  SELL-EX2      11:45   76.0    12.01
2026-08-28  OIL          BUY   BUY-EX17      11:00   76.0    31.56
2026-08-28  ICICIPRULI   SELL  NORMAL SELL   11:30   76.0    29.67
```

A+ quality scores 75–80 on a 70-point floor. **M12 is not failing its own checklist — it is being denied the chance to act on it.** Research expectation was 1.39 trades/day; the delivered rate is 0.00.

---

## 3. M13 Equity Momentum A+ — gate-by-gate

1,735 recorded candidates over 12 sessions, same independent recomputation.

| Mandatory rule | Rows passing | First-failing-gate tally (as recorded) |
|---|---:|---|
| 1. real enabled chart master variant (`90`/`290` preview excluded) | 1,503 (86.6%) | 232 (13.4%) |
| 2. time in 09:45–12:00 | 1,079 (62.2%) | 424 (24.4%) |
| 3. S1 Morning Base mandatory | 944 (54.4%) | 135 (7.8%) |
| 4. opening market breadth ≥ 55% | **554 (31.9%)** | **639 (36.8%)** ← largest killer |
| 5. prior-session VIX return > −2% | 919 (53.0%) | 102 (5.9%) |
| 6. live breadth ≥ 45% (opposite-regime veto) | 1,261 (72.7%) | — |
| 7. A+ score ≥ 70 | 198 (11.4%) | — |
| **All seven** | **3** | |
| …also ≤ 5 min old | **1** | → `RVNL SELL 10:30 @ ₹220.34`, 25-Aug (the only M13 alert in 12 sessions) |

The two lost ones: `CGPOWER SELL 11:20` on 19-Aug rejected at **6.40 min — 84 seconds past the deadline**, and `INOXWIND SELL 11:45` on 25-Aug at 12.93 min.

M13 delivered 1 trade in 12 sessions against a designed 1–3/day.

---

## 4. RC2 proven numerically: the "previous close" is not a close

`m12_runner.load_previous_closes()` (and the twin `m13_runner.load_prev()`) fall back to `data/history/*.csv` whenever the seeded cache is unusable, and the fallback branch **returns `status:"OK"` without checking that the date equals the prior weekday**:

```python
if common and len(vals) >= 180:
    return vals, {"status": "OK",
                  "source": "data/history bootstrap" if common == str(expected)
                            else "data/history fallback (latest available)", ...}
```

All 210 history files end at `2026-08-18 09:34:17+05:30` — a 5-bar truncated session, so the "previous close" is actually **an intraday price from six trading days earlier**:

```
RELIANCE, 2026-08-28, SELL @ 11:25 @ ₹1284.20, recorded f_dir_prev_pct = +2.9987%
  → implied baseline = 1284.20 / (1 − 0.029987) = ₹1323.900
  → data/history/RELIANCE.csv last row close      = ₹1323.9000244140625   ← exact match
```

RELIANCE was *below* its true 27-Aug close at that moment; M12 recorded it as +3.0% "extended in the trade direction" and vetoed the anti-chase gate. Consequences:

* **M12 gate 3** tests ~6-session drift instead of an overnight gap → pass rate collapses to 12.8% and becomes a lottery on how far the stock drifted since 18-Aug.
* **M12 gate 4 / M13 gate 4** measure breadth against the same dead baseline → `sector_breadth_prev_dir` pins to `1.000`/`0.000` on many rows; M13 opening breadth sits at a near-random 31.9% pass rate (mean 0.500 BUY / 0.491 SELL).
* **`L.engine_frame()`** stitches `[history … 08-18 09:34] + [today's bars]`, so `trader.ATR(14)`, `EMA9/20/50` and every `*_atr` feature — plus `trader.load_warmup()` used for the structure stop — are seeded across a 10-day hole with a 4-bar tail.
* **`clock_relvol`**'s 20-session same-clock median is drawn from a frozen, stale volume regime.

This is a direct contradiction of `M12_ENTRY_MODEL.md`:

> "A cache/history date must equal the prior weekday and contain completed 15:20-or-later bars for at least 180 stocks. If freshness cannot be proved … M12 takes no entries."

The code **fails open** where the spec says fail closed.

### Why the cache can never heal

EOD seeding keeps only bars at/after `trader.SQOFF` (`15:20`):

```
data/m12_prev_close.json → {"date":"2026-08-28","count":3,
                             "symbols":["DALBHARAT","EXIDEIND","NUVAMA"],"last_bar_min":"15:25"}
```

`count: 3` (identical in `m13_prev_context.json`) — permanently below the 180 threshold, so the cache is rejected every next morning, forever. Cause: the EOD run lands at 15:30–15:49 IST on a **Yahoo-only feed that is delayed**, so almost no symbol has a completed ≥15:20 bar yet. The repo history is supposed to be the backstop — and it is the part that is frozen (next section).

### Why `data/history` is frozen

`2. Bootstrap — daily history download` is the **only** workflow that writes `data/`, and it is dying at the last-but-one step:

```
run 33081108599 (27-Aug, schedule) → conclusion: cancelled  (timeout-minutes: 55)
  5  live_runner.py --bootstrap refresh-60d   success      ← history IS downloaded
  6  m9_bootstrap.py                            success
  7  label_learn.py                             cancelled   ← ran 14:20:56 → 15:09:33 (48m37s), never finished
  8  commit data safely against moving main     SKIPPED     ← nothing is ever pushed
```

14 of the last 15 bootstrap runs are `cancelled`; the single success (4 days ago) took 45m25s inside a 55m ceiling. A currently-running dispatch is at 45m+.

Measured locally on this checkout:

* `label_learn.day_universe_stats()` = **27 s per day** × 25 days ≈ 11 min.
* `label_learn.label_row()` = **~11 rows/s** on days that have history (it calls `trader.load_warmup()`, which re-reads the whole ~4,200-row per-symbol CSV **once per candidate row**, uncached) → 39,392 pending raw rows ≈ **58 min**.
* Total ≈ **69 min vs a 55-min timeout** → deterministic self-kill, and the `label_learn` step sits *before* the commit.

It also re-labels *everything* every run: its skip guard is `out_fp.stat().st_mtime > f.stat().st_mtime`, and after `actions/checkout@v5` all files in `learn/` carry near-identical checkout mtimes, so the guard is meaningless.

---

## 5. RC1: the cadence law

`signal_age_min` is measured as `detection_time − (bar_close + 5 min)`, evaluated **inside a serial 210-symbol sweep**. The maximum age observed in a day is almost exactly the dispatch interval, which proves the ages are cadence-bound rather than market-bound:

| day | M12 median age | M12 max age | M13 median | M13 max | fresh share (M12 / M13) |
|---|---:|---:|---:|---:|---|
| 2026-08-12 | 16.4 | 34.1 | 8.7 | **15.0** | 20% / 32% |
| 2026-08-14 | 17.1 | 33.3 | 7.5 | **14.7** | 28% / 44% |
| 2026-08-26 | — | — | 8.1 | **15.4** | — / 31% |
| 2026-08-27 | 17.9 | 34.8 | 8.2 | **15.2** | 12% / 27% |
| 2026-08-28 | 21.7 | 33.9 | 8.4 | **15.2** | 10% / 29% |
| 2026-08-18 | 27.3 | **425.1** | 17.8 | 66.4 | 12% / 19% |
| 2026-08-21 | 17.0 | 61.6 | 21.3 | 64.3 | **4%** / **1%** |

Therefore: **reachable fraction of the session ≈ 5 min ÷ dispatch interval.** Confirmed against GitHub:

```
2026-08-28 runs   M12: 23, all workflow_dispatch, spaced 30 min (:00/:30)   schedule: 0
                  M13: 44, all workflow_dispatch, spaced 15 min (:00/:15/:30/:45)  schedule: 0
last 100 runs     M12: 15 schedule / 85 dispatch      M13: 3 schedule / 97 dispatch
expected at the in-repo 5-min cron   77 runs/day/model
```

* The five-minute `schedule:` blocks in `12_live_m12.yml` / `13_live_m13.yml` are effectively **dead** (0 fired on 28-Aug). Per `CRON_M12_M13.md`, an external cron-job.org backup was added — and it is polling at 15/30 min, which the same file explicitly warns against ("Do not run a full-frequency external cron *and* the repo's five-minute schedule at the same time"); the opposite happened: a *low*-frequency external cron replaced the internal one.
* A real M12 paper cycle takes **5m57s–9m47s** of job time (`pip install` + two test files + a 210-symbol Yahoo sweep). Even at a perfect 5-min cadence, a >5-minute job overruns its own freshness budget and queues behind itself (`concurrency.cancel-in-progress: false`), so drift compounds.
* Because the sweep is serial, only symbols reached in the first ~5 minutes of the cycle are eligible at all. Per day, **only 3–13 of ~30 signal-bearing bars** (M12) and **4–13 of ~29** (M13) are ever reachable in-window.

M11, M7, M8 etc. keep firing because **none of them implement an age gate** — `MAX_SIGNAL_AGE`/`signal_age` appears only in `m12_entry.py`, `m12_runner.py`, `m13_entry.py`, `m13_runner.py`. Same feed, same cadence, same secrets: the only difference is this one rule.

### Sessions where nothing was evaluated at all

Two days produced an **empty** (1-byte) candidate ledger for *both* models — `learn/m12_candidates_2026-08-20.csv` and `-08-24.csv`, and the same two dates for M13. Those are days where `prev_meta.status != "OK"` held (the documented fail-closed branch won by accident), so `collect_candidates()` never ran and the log printed `M12 STRICT NO-ENTRY: previous closes are stale/unavailable`. Across the 14 weekdays from 11-Aug to 28-Aug: **M12** has rows on 11 days, dead-empty ledgers on 2, and **no ledger and no report at all for 2026-08-26**; **M13** has rows on 12 days and empty ledgers on 2.

The same fail-closed guard that is supposed to *protect* the models is therefore also a live, recurring source of dead days — and it is the only defence currently working, because the `status:"OK"` fallback defeats it whenever the stale history file exists.

---

## 6. Secondary defects confirmed

1. **Dhan is down for both models.** `feed: {dhan_calls: 1, yahoo_calls: 210, fallback: "HTTP 401"}` — the circuit breaker trips on symbol #1 and the entire universe runs on Yahoo's ~15-min-delayed data. `Dhan 401` = expired/invalid `DHAN_TOKEN`. This both *adds* latency to a latency-critical gate and is why the 15:20 EOD seeding captures only 3 symbols.
2. **The freshness rule is a hidden, unlogged-to-the-user veto.** `if accepted and age > MAX: accepted = False` runs *after* `decide()`, so the report "skipped" tab and the EOD summary attribute the miss to the *model* rather than the *pipeline*. The `signal_age` distribution is never summarised anywhere.
3. **`test_m12_entry.py::test_previous_close_freshness` locks in the wrong behaviour.** Its "stale previous-close cache fails closed" case points `L.HIST` at an **empty** temp dir, so the fallback has no data to return. It never exercises the production case (non-empty but stale history) where `load_previous_closes()` returns `status:"OK"`. Tests all pass (`test_m12_entry`, `test_m13_entry`, `test_m13_alerts`, `test_m13_runner`, `test_m13_trader`) while the pipeline is silently feeding garbage.
4. **README.md's "M12 and M13 are now technically working — current history is fresh; previous context is OK" is the exact symptom of RC2, not evidence against it.** `prev_meta.status == "OK"` with `date: 2026-08-18`, `expected_previous_weekday: 2026-08-27` is the bug firing as designed. The README also frames "Zero trades is therefore a strategy result" — that conclusion is not supported: 10 M12 + 3 M13 candidates *did* satisfy every rule.
5. **Alert delivery is fire-and-forget-after-reservation.** `reserve_once()`/`reserve_alert_batch()` write the key into `state12/13.json` **before** the network call and the send result is never checked. Any Telegram failure (429 after one retry, bad chat id, transient 5xx) burns the key permanently → an alert can never be resent. If you did **not** receive the daily 🅼12/🅼13 EOD pair, that is this path, not the models.
6. **Whitelist vs. flagship quality filter contradiction.** M12's frozen whitelist contains 102, 213, 216, 220 — four codes that `live_runner.EX_WEAK_CODES` (`{102} ∪ 108–112 ∪ 210–220`) hard-blocks in the main runner as "weak EX variant (EX9+) … net loser group". 156 of M12's 744 whitelist survivors (21%) sit in that contested set. One of the two beliefs is stale.
7. **The evidence base for the frozen thresholds is not in the repo.** `M12_ENTRY_MODEL.md` cites `analysis/evaluate_m12.py`, `analysis/gate_interaction_audit.py`, `analysis/output/*.csv`, `analysis/DEEP_AUDIT.md` — `analysis/` **does not exist** in this checkout. The 88.52%-win replay cannot be reproduced, and it was run on data whose prev-close semantics differ from what live sees today.
8. **Holiday-blind prior-weekday logic.** Both runners define the prior session as "last weekday ≠ Sat/Sun". Any NSE holiday (or a truncated download like the current 09:34 stub) is indistinguishable from "fresh", so M12/M13 trade on a bogus baseline instead of failing closed.

---

## 7. Fix plan, in order

**P0 — unblock the data (nothing else matters until this lands)**

1. In `2_bootstrap.yml`, move the `commit data safely…` step **above** `label_learn.py`, and give the labeler its own job (or `if: always()` + `continue-on-error` + a hard `timeout 1200` wrapper). Refreshed history must be committed even when labelling dies.
2. Bound `label_learn.py`: default to the newest session only (`--day` default = latest raw day), cache the `trader.load_warmup()` frame per `(sym, day)` in `_DF`, and vectorise `day_universe_stats` (one groupby over the loaded frames instead of 210 mask passes per day).
3. Replace the mtime guard with a content/date guard (e.g. relabel only days whose `labeled_*.csv` is missing or whose `max(day)` differs).

**P0 — make M12/M13 fail closed as documented**

4. In `m12_runner.load_previous_closes()` and `m13_runner.load_prev()`, require `day == expected_previous_weekday` on the *fallback* path too; otherwise return `status:"STALE"`. Today's run would have correctly printed `STRICT NO-ENTRY` and taken zero trades **for the right reason**, instead of silently gating on a 10-day-old intraday price.
5. Seed the prev-close cache from a source that is guaranteed complete at EOD (Yahoo `range=2d`, or the Dhan daily candle), and accept `>=15:25` *or* the final bar of the session — not "≥15:20 or nothing".

**P1 — restore a cadence the model can survive**

6. Pick one scheduler and make it 5-minute in-market: re-enable the internal `schedule:` and **delete the external cron-job.org jobs** (they are the reason cadence is 15/30 min), or move M12/M13 to the always-on VM path (`run-live.ps1` / `vm-setup.sh`) with a 5-minute timer, which is what `M12_ENTRY_MODEL.md` already recommends ("no cloud cron can guarantee zero latency").
7. Make the cycle fit inside 5 minutes: skip `test_*.py` on scheduled runs (separate lint job), parallelise the 210-symbol engine sweep (threads — it is pandas-bound), and compute all bars of one symbol in one pass instead of re-running `run_symbol` per bar.
8. Fix the cursor reset: `if known > n: known = 0` should be `known = min(known, max(n - 1, 0))` (or clamp to the newest closed bar), so a short feed response cannot trigger a whole-session replay. Add a per-cycle wall-clock budget (`M12_M13_MAX_SWEEP_SEC`) that stops *collecting* before the deadline rather than after it, and log `cycles_skipped_for_budget`.
9. Surface the age distribution (median / max / % over-limit) in the EOD summary text, so a cadence regression is visible in the alert you actually read instead of only in the CSV.

**P1 — restore the real-time feed**

10. Rotate `DHAN_TOKEN` (401 = expired). Until then, mark M12/M13 cycles `DEGRADED-YAHOO` in the EOD message, since Yahoo latency alone can exceed the 5-minute budget on the 12:00 cut-off.

**P2 — make the loss visible**

11. Emit a distinct `STALE` skip bucket (with median/max age and % over-limit) in `report.py` output and the EOD text — the single biggest reason this went unnoticed for 11 sessions.
12. Check the `tg.send_message` return value; on failure delete the reserved key (or move to a `pending → sent` two-state key) so an alert is not silently burned.
13. Fix `test_previous_close_freshness` to populate a **stale but non-empty** history dir, and add a test asserting `status != "OK"` when `date != expected_previous_weekday`.
14. Resolve the whitelist vs `EX_WEAK_CODES` contradiction and restore or delete the `analysis/` artefacts the thresholds cite.

**Do not** loosen `SECTOR_BREADTH_MAX`, `DIR_PREV_*`, `MIN_A_PLUS_SCORE` or `MAX_SIGNAL_AGE_MIN` in response to this — `M12_ENTRY_MODEL.md` is explicit that thresholds must not be re-tuned on a sample already consumed. Fix data and cadence first, then re-measure on untouched sessions.

---

## 8. Appendix — audit output

```
$ python -u diagnose_m12_m13.py
```

```text
==============================================================================
M12 / M13 ENTRY FUNNEL, TIMING AND DATA-FRESHNESS AUDIT
==============================================================================

### M12 Selective Reversion  ·  1599 recorded candidates (all committed sessions)
   1. code in frozen reliability whitelist                744/1599  ( 46.5%)
   2. signal time inside 09:45-12:00 window              1000/1599  ( 62.5%)
   3. anti-chase displacement in [-1.00%, +0.20%]         204/1599  ( 12.8%)
   4. sector breadth vs prev close <= 0.43                353/1599  ( 22.1%)
   5. at least one causal video setup                     908/1599  ( 56.8%)
   6. same-clock relative volume >= 1.0                  1094/1599  ( 68.4%)
   7. model verdict accepted (all six + causality)          0/1599  (  0.0%)
   --- timing (bar close + 5 min deadline) ---         
   median signal age 16.66 min · p90 31.22 min · 82.1% over the 5.0-min limit
   passed EVERY hard gate: 10   of which rejected only for staleness: 10   actionable (fresh): 0
   session coverage: 20/33 signal-bearing bars reachable in-window · 157/210 symbols ever evaluated fresh

### M13 Equity Momentum A+  ·  1735 recorded candidates (all committed sessions)
   1. real enabled chart master variant                  1503/1735  ( 86.6%)
   2. signal time inside 09:45-12:00 window              1079/1735  ( 62.2%)
   3. S1 Morning Base mandatory                           944/1735  ( 54.4%)
   4. opening market breadth >= 55% in signal direction   554/1735  ( 31.9%)
   5. prior-session VIX return > -2%                      919/1735  ( 53.0%)
   6. live breadth not flipped opposite (<45% veto)      1261/1735  ( 72.7%)
   7. A+ score >= 70                                      198/1735  ( 11.4%)
   8. model verdict accepted                                1/1735  (  0.1%)
   --- timing (bar close + 5 min deadline) ---         
   median signal age 8.26 min · p90 14.94 min · 66.8% over the 5.0-min limit
   passed EVERY hard gate: 3   of which rejected only for staleness: 2   actionable (fresh): 1
   session coverage: 24/33 signal-bearing bars reachable in-window · 193/210 symbols ever evaluated fresh

### Data freshness (previous-close baseline for both models)
   data/history files: 210 · latest session present: 2026-08-18 (210 files) · history is 10 days behind the newest audited session; correct baseline would be 2026-08-27
   RELIANCE tail bar: 2026-08-18 09:34:17+05:30 @ 1323.9000244140625
   m12 prev-context cache: {'present': True, 'date': '2026-08-28', 'count': 3, 'meets_180_coverage': False, 'symbols': ['DALBHARAT', 'EXIDEIND', 'NUVAMA'], 'last_bar_min': '15:25'}
   m13 prev-context cache: {'present': True, 'date': '2026-08-28', 'count': 3, 'meets_180_coverage': False, 'symbols': ['DALBHARAT', 'EXIDEIND', 'NUVAMA'], 'last_bar_min': '15:25'}
   state12.json: date=2026-08-28 cycles=13 trades=0 decisions=144
        prev_meta={'status': 'OK', 'source': 'data/history fallback (latest available)', 'date': '2026-08-18', 'age_days': 10, 'count': 210, 'expected_previous_weekday': '2026-08-27'}
        alerts=2 (ENTRY alerts=0)  feed={'dhan_calls': 1, 'yahoo_calls': 210, 'fallback': 'HTTP 401', 'fed': 210}
   state13.json: date=2026-08-28 cycles=25 trades=0 decisions=144
        prev_meta={'status': 'OK', 'source': 'data/history fallback (latest available)', 'date': '2026-08-18', 'count': 210, 'expected_previous_weekday': '2026-08-27'}
        alerts=2 (ENTRY alerts=0)  feed={'dhan_calls': 1, 'yahoo_calls': 210, 'fallback': 'HTTP 401', 'fed': 210}

==============================================================================
BLOCKING FINDINGS:
  ✗ M12: 10 fully-qualified signals were all discarded by the 5.0-minute freshness rule
  ✗ M12: prev-close cache covers only 3 symbols (< 180) so seeding never succeeds
  ✗ M13: prev-close cache covers only 3 symbols (< 180) so seeding never succeeds
  ✗ state12.json: previous-close baseline reported OK but is 2026-08-18 instead of 2026-08-27
  ✗ state12.json: zero ENTRY alerts reserved today — only the EOD pair was sent
  ✗ state13.json: previous-close baseline reported OK but is 2026-08-18 instead of 2026-08-27
  ✗ state13.json: zero ENTRY alerts reserved today — only the EOD pair was sent
```

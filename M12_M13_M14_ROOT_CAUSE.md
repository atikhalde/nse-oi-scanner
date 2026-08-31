# M12 / M13 / M14 — why they alone fire no alerts

**Audit date:** 31-Aug-2026 · **Evidence in-repo:** `state11..14.json`, `learn/m1{2,3,4}_candidates_*.csv` (1,599 / 1,735 / 0 rows), `data/history/*.csv` (210 files), `paper_test_M1{1,2,3,4}_*.xlsx`, `.github/workflows/`
**All numbers below were recomputed from the repo, not copied from the previous audit.**

---

## 0. Verdict in one paragraph

M12/M13/M14 are not failing to *find* setups and the Telegram path is not broken. They are the **only three models in the fleet that refuse to trade unless a previous-close baseline passes a strict precondition**, and that precondition is currently **unsatisfiable by construction**. Two independent, stacking defects:

* **RC1 — the "official close" quality floor can never be met.** Every writer of the prev-close cache requires the session's final 5-minute bar to be `>= 15:20`. Across the last 8 sessions **only 2–3 of 210 symbols** ever satisfy it. The cache is therefore *never written*, and all three models fall back permanently to `data/history`.
* **RC2 — the history fallback is fail-closed on an exact-date match, and its own "gap" escape hatch is dead code.** Whenever `data/history` does not contain *precisely* the previous weekday, all three models emit `STRICT NO-ENTRY` and evaluate **zero** candidates for the entire session. M10/M11 have no such precondition and keep trading on the same days.
* **RC3 (why they're near-zero even on good days) — `MAX_SIGNAL_AGE_MIN = 5.0` vs a measured ~30-minute real dispatch cadence.** Only 17.9% of recorded signals are ever ≤ 5 minutes old.

Today, 31-Aug, is a pure RC2 day: **M11 fired 47 entry alerts; M12/M13/M14 fired zero and logged zero decisions.**

---

## 1. The differential: same day, same secrets, same workflow family

```
state11.json   cycles 7    alerts 50   -> ['ABB:ENTRY','ADANIGREEN:ENTRY','APOLLOHOSP:ENTRY', ...]  28 trades
state12.json   cycles 14   alerts 2    -> ['M12:2026-08-31:EOD_SUMMARY','M12:2026-08-31:EOD_DOCUMENT']
state13.json   cycles 26   alerts 2    -> ['M13:...:EOD_SUMMARY','M13:...:EOD_DOCUMENT']
state14.json   cycles 8    alerts 2    -> ['M14:...:EOD_SUMMARY','M14:...:EOD_DOCUMENT']
```

M12/13/14 ran **48 cycles between them** and produced `decisions: []`, `trades: {}`. The two alerts each are the EOD summary + EOD document — i.e. the bot path works fine; it is `dispatch()` that is never reached, not `send_message` that fails.

Candidate rows written per session (from the EOD workbooks; `1` = header only = engine never evaluated anything):

| day | M11 | M12 | M13 | M14 |
|---|---:|---:|---:|---:|
| 2026-08-19 | 266 | 168 | 220 | – |
| **2026-08-20** | **247** | **1** | **1** | – |
| 2026-08-21 | 288 | 170 | 82 | – |
| **2026-08-24** | **219** | **1** | **1** | – |
| 2026-08-25 | 265 | 255 | 273 | – |
| 2026-08-26 | 188 | – | 156 | – |
| 2026-08-27 | 189 | 236 | 156 | – |
| 2026-08-28 | 195 | 271 | 155 | – |
| **2026-08-31** | **178** | **1** | **1** | **1** |

`learn/m12_candidates_2026-08-{20,24,31}.csv` and `learn/m13_candidates_2026-08-{20,24,31}.csv` are **1 byte on disk**. M11 never has a blank day. **M14's only ledger ever written is 08-31 — 1 byte. M14 has never evaluated a single candidate in its life.**

---

## 2. RC1 — the `>= 15:20` official-close floor is unsatisfiable

Every path that could write the cache applies the same quality floor:

* `m12_runner.seed_previous_close_cache()` — `if last_min < trader.SQOFF: continue` (`trader.SQOFF = "15:20"`)
* `m13_runner.seed_prev()` — `if lm<'15:20':continue  # 15:10/15:15 mark is not an official close`
* `m14_runner.seed_prev()` — `if last_min < "15:20": continue`
* `seed_prev_context.daily_prev_context(min_bar="15:20")` — used by `top_up()`, `self_heal()` **and** the CLI reseed job

Measured over `data/history` — count of the 210 symbols whose final bar of the day is `>= 15:20`:

```
2026-08-20  total 210  >=15:20:  2   last-bar mix {'15:15':128,'15:10':80,'15:25':2}
2026-08-21  total 210  >=15:20:  2   last-bar mix {'15:15':163,'15:10':45,'15:25':2}
2026-08-24  total 210  >=15:20:  2   last-bar mix {'15:15':133,'15:10':75,'15:25':2}
2026-08-25  total 210  >=15:20:  2   last-bar mix {'15:15':137,'15:10':71,'15:25':2}
2026-08-26  total 210  >=15:20:  2   last-bar mix {'15:15':144,'15:10':64,'15:25':2}
2026-08-27  total 210  >=15:20:  2   last-bar mix {'15:15':130,'15:10':78,'15:25':2}
2026-08-28  total 210  >=15:20:  3   last-bar mix {'15:15':134,'15:10':73,'15:25':3}
2026-08-31  total 210  >=15:20:  3   last-bar mix {'15:10':140,'15:15':67,'15:25':2,'15:30':1}
```

The quorum required is **180**. The supply is **2–3**. It is not a lag, not a bad day, not a race — it is a structural property of the feed: with `dhan_calls: 0 / yahoo_calls: 210` (Dhan's token is dead, everything is on Yahoo), Yahoo's NSE 5-minute series simply ends at 15:10/15:15 for ~99% of symbols.

The three symbols that do qualify are the same ones every single day:

```
2026-08-28  [('DALBHARAT','15:25'), ('EXIDEIND','15:25'), ('NUVAMA','15:25')]
2026-08-31  [('DALBHARAT','15:25'), ('EXIDEIND','15:30'), ('NUVAMA','15:25')]
```

And that is exactly, to the symbol, what the self-heal recorded in state today:

```json
"prev_heal": { "date": "2026-08-28", "count": 3, "last_bar_min": "15:25",
               "total_fed": 210, "status": "INSUFFICIENT",
               "policy": "cache not written; existing cache left untouched" }
```

**`count: 3` is not a network failure.** `self_heal()` re-fetched all 210 symbols live from Yahoo with `rng="1mo"` and 2 retries, and got the identical 3 symbols the offline history predicts. The recovery path is deterministically incapable of recovering.

Consequences that follow mechanically:

1. `data/m12_prev_close.json`, `data/m13_prev_context.json`, `data/m14_prev_context.json` **do not exist in the repo** (`git ls-files data` returns only `data/history/*` and `data/oi_prev.csv`). They have never been successfully written.
2. The cache branch — the *primary and intended* source — is dead in all three loaders. 100% of the load falls to the `data/history` fallback.
3. M14's dedicated 16:10 IST reseed job (`cron: "40 10 * * 1-5"` → `python -u seed_prev_context.py --models m14`) inherits `--min-bar 15:20` by default, so the one piece of infrastructure built specifically to break this loop **also always returns INSUFFICIENT**.
4. The `< 180 → do not overwrite` policy is correct and is *not* the bug — but combined with a supply of 3 it means the guard fires unconditionally, forever.

---

## 3. RC2 — fail-closed on exact previous-weekday, with a dead escape hatch

With the cache permanently absent, everything rests on `data/history` containing **precisely** the expected previous weekday.

`m14_runner.load_prev()` is the clearest statement of the policy:

```python
if len(vals) >= 180:
    return vals, piv, {"status": "OK", "source": "data/history bootstrap", ...}
# Strict Fail Closed: If date != expected previous weekday, do NOT pass as OK
return {}, {}, {"status": "STALE", ...}
```

and the caller:

```python
if prev_meta.get("status") == "OK":
    fresh = collect_candidates(...); dispatch_candidates(...)
else:
    print("M12 STRICT NO-ENTRY: previous closes are stale/unavailable")
```

`m13_runner.py:278` and `m14_runner.py:629` gate identically (M13/M14 additionally `and finite(vix)`).

### 3a. The "holiday/gap" fallback is dead code

`m12_runner.load_previous_closes()` builds a second candidate set explicitly labelled as the fix for this:

```python
# Fallback: allow latest available previous session when exact previous weekday
# is missing (e.g., holiday/gap). This is the fix for stale previous close.
if not (common and day == expected and len(vals) >= 180):
    ...recompute `vals` from each symbol's latest day < today...
if common and day == expected and len(vals) >= 180:      # <-- still demands exact match
    return vals, {...OK...}
return {}, {...STALE...}
```

The fallback recomputes the values, then hands them to a guard that **still requires `day == expected`**. If the exact previous weekday were present, the first branch would already have returned. The fallback can therefore never change the outcome. `m13_runner.py:72` has the same defect (`if parse_day(common_fallback) == exp and ...`). M14 doesn't even attempt a fallback.

So the intended tolerance for holidays, gaps and late bootstraps does not exist in any of the three models.

### 3b. Why the exact previous weekday goes missing — the upstream domino

`data/history` is published by workflow **`2. Bootstrap`**:

```yaml
schedule:
  - cron: "15 3 * * 1-5"    # 08:45 IST weekdays, before market open
timeout-minutes: 55
steps:
  - python -u live_runner.py --bootstrap refresh-60d     # 210 symbols
  - python -u m9_bootstrap.py                            # ~1,050 symbols
  - python -u label_learn.py                             # ML dataset build
  - name: commit data safely against moving main         # <-- commit is LAST
```

The commit is the final step, so **any timeout or failure anywhere in that 55-minute job publishes nothing at all**. The M12/13/14 live crons start at `45-59/5 3 * * 1-5` = 09:15 IST — 30 minutes after Bootstrap starts and well before a 3-stage, ~1,260-symbol refresh can finish. The only bootstrap commit in this snapshot is:

```
74c9c49  2026-08-31 10:21:21 +0000  bootstrap 2026-08-31_1021Z
```

10:21Z = **15:51 IST — after the close**. So on 31-Aug the refreshed history landed roughly six hours after the models needed it. During the live session the checkout's newest history day was older than 28-Aug, `day != expected`, and all three models sat in `STRICT NO-ENTRY` for all 48 cycles.

Because Bootstrap runs `refresh-60d`, the post-close commit backfills the whole window — which is why a post-mortem looks clean. Verified: running the loaders against the repo **as it stands now** returns OK for every date:

```
M12 2026-08-31 -> OK 2026-08-28 count 210
M12 2026-08-28 -> OK 2026-08-27 count 210
M12 2026-08-24 -> OK 2026-08-21 count 210
M12 2026-08-20 -> OK 2026-08-19 count 210
```

**The evidence is self-erasing.** The loaders are healthy at audit time and were STALE at run time; only the 1-byte ledgers preserve the fact. This is why the outage keeps looking unreproducible.

The blank days — 08-20, 08-24 (Mon), 08-31 (Mon) — are the days Bootstrap didn't land in time. M10/M11 read the same directory but take each symbol's last row before today with **no date assertion and no quorum** (`m11_runner.py:328`, `m10_runner._prev_close`), so a late bootstrap merely makes their baseline one day stale; they keep firing. That is the entire asymmetry the question asks about.

---

## 4. RC3 — the 5-minute freshness rule vs the real cadence

On the days the engines *do* run, they still convert almost nothing:

| | rows | sessions | accepted | taken |
|---|---:|---:|---:|---:|
| M12 | 1,599 | 11 | **0** | **0** |
| M13 | 1,735 | 12 | **1** | **1** |
| M14 | 0 | 0 | 0 | 0 |

All three enforce `MAX_SIGNAL_AGE_MIN = 5.0`. Measured age of recorded signals:

```
M12  median 16.66  mean 26.06  p90 31.22  max 425.12   -> 82.1% exceed 5 min
M13  median  8.26  mean 21.71  p90 14.94  max 511.48   -> 66.8% exceed 5 min

M12 cumulative coverage:  <=5min 17.9% | <=10min 31.0% | <=15min 44.4% | <=20min 60.3% | <=30min 86.9%
```

The reason is the dispatch cadence. Reconstructing distinct cycle start times from `detected_at` shows the jobs do **not** arrive every 5 minutes as the cron declares — they arrive in bursts roughly every 30 minutes (GitHub's scheduler heavily throttles `*/5` crons, and each job additionally pays checkout + `pip install` + the test suite before the cycle runs):

```
2026-08-12  cycles at 09:31, 09:32, 10:01..10:09, 10:32, ...
2026-08-28  cycles at 09:31, 10:00..10:08, 10:31, ...
```

A 5.0-minute deadline against a ~30-minute arrival interval means the model can only ever act on the single newest bar in each burst — ~1 of every 6 bars, which is precisely the measured 17.9%.

The cost is not theoretical. Candidates that passed **every other gate** and were then killed solely by the age rule: **10 for M12, 2 for M13** (`model_reason` containing `stale signal`) — including `M&M SELL-EX1 09:50` rejected at **5.12 min, 7 seconds over the limit**, and `CGPOWER SELL 11:20` at 6.40 min. M13's single lifetime trade (`RVNL SELL 10:30`, 25-Aug) is the one case where a burst happened to land within 5 minutes of the bar.

First-failing-gate distribution (note `decide()` short-circuits, so these are first-failures, not independent pass rates):

```
M12: whitelist 855 | outside 09:45-12:00 window 173 | sector crowded ~30 | anti-chase ~20
M13: opening breadth <55% 639 | outside 09:45-12:00 424 | not a real chart variant 232
     | S1 mandatory 135 | prior VIX <= -2% 102
```

Also worth noting the interaction: the entry window is 09:45–12:00 = 135 minutes. At a ~30-minute effective cadence that is **≈5 chances per day**, each of which must additionally clear a 5-minute freshness gate.

---

## 5. Failure chain

```
Dhan token dead -> 100% Yahoo -> Yahoo NSE 5m series ends 15:10/15:15
        |
        v  (RC1)  >=15:20 "official close" floor: supply 2-3 of 210, quorum 180
prev-close cache NEVER written  (all 3 data/*.json absent from the repo)
        |         self_heal() and the 16:10 M14 reseed inherit min_bar=15:20 -> also always INSUFFICIENT
        v
100% dependence on data/history containing EXACTLY the previous weekday
        |
        v  (RC2)  Bootstrap commits last, after a ~1,260-symbol / 55-min job,
                  starting only 30 min before the models -> often lands post-close
                  and the "latest available session" fallback is dead code
STRICT NO-ENTRY for the whole session -> 0 candidates  (08-20, 08-24, 08-31)
        |
        v  (RC3)  on the days it does run: MAX_SIGNAL_AGE_MIN=5.0 vs ~30-min real cadence
0 accepted (M12, 11 sessions) / 1 accepted (M13, 12 sessions) / M14 never evaluated a row
```

M10/M11 are immune at every step: no quality floor, no quorum, no date assertion, no freshness deadline.

---

## 6. Recommended fixes, in dependency order

**F1 — make the close floor match the feed (unblocks everything).**
Lower the floor to `15:10`, or better, make it relative: accept the day's final bar when it is within N minutes of the last bar the feed produced *for that session across the universe* (a per-session mode, not a hard clock). Apply in all four writers: `m12_runner.seed_previous_close_cache`, `m13_runner.seed_prev`, `m14_runner.seed_prev`, `seed_prev_context.MIN_BAR`. Note `m10/m11` have priced their prev-close off these same 15:10/15:15 bars for weeks without issue — the floor is stricter than the fleet's own working precedent.

**F2 — pass `--min-bar` explicitly in the M14 reseed job** so the post-close reseed cannot silently inherit an unsatisfiable default:
`python -u seed_prev_context.py --models m12,m13,m14 --min-bar 15:10` — and give M12 and M13 the same 16:10 job (today only M14 has one).

**F3 — repair the dead gap fallback.** In `m12_runner.load_previous_closes` and `m13_runner.load_prev`, the second guard must drop `day == expected` and instead bound staleness, e.g. accept when `0 < (today - day).days <= 4` with `count >= 180`, returning `status:"OK"` but `source:"latest available (gap)"` so the degradation is visible in state and in the EOD report. Add the same fallback to M14, which has none.

**F4 — decouple from the Bootstrap race.** Move the `git add data/ learn/` commit to run **immediately after** `live_runner.py --bootstrap refresh-60d` (an `if: always()` step), before `m9_bootstrap.py` and `label_learn.py`. Those two are the long poles and neither produces the history M12/13/14 depend on. Alternatively move Bootstrap to a post-close slot (~16:00 IST) so the data is committed the evening before it is needed rather than 30 minutes before.

**F5 — align freshness with reality.** Raise `MAX_SIGNAL_AGE_MIN` to ~15–20 min (recovers 44–60% of signals vs 17.9%), *or* keep 5.0 but measure age from cycle start rather than bar close and evaluate only the newest bar per symbol per cycle. Do not do both. Given the 09:45–12:00 window yields only ~5 cycles/day, 5.0 min is equivalent to disabling the models.

**F6 — make the silence loud.** `STRICT NO-ENTRY` and `INSUFFICIENT` are currently `print()` only. Send one throttled Telegram warning per session when a model is in no-entry state at, say, 10:00 IST. All three have been silent for weeks and the silence was indistinguishable from "no setups today".

**F7 — regression tests.** Assert that (a) `load_prev*` returns `OK` when history's newest session is 1–4 days stale, (b) the seed writes a cache when the universe's final bars are at 15:10, (c) `seed_models` is called with an explicit `min_bar` everywhere.

F1+F4 alone restore candidate flow; F5 is what converts candidates into alerts.

---

## 7. Note on the previous audit

`M12_M13_DIAGNOSIS.md` (28-Aug) identified the 5-minute-vs-cadence problem (RC1 there = RC3 here) and a stale `data/history` frozen at 2026-08-18 with a 09:34 stub. **That specific staleness is fixed** — all 210 files now carry a full 2026-08-31 session, and the anti-chase/breadth gates are computing against real prior closes again. What the earlier audit did not catch is that the *replacement* machinery — the prev-close cache and its self-heal, added to fix it — has never once produced output, because the `>= 15:20` floor it was built on cannot be met by the feed. The failure moved from "wrong baseline" to "no baseline, fail closed", which is why the symptom changed from bad gates to blank ledgers.

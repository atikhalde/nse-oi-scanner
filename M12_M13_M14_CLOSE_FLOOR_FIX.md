# 2026-08-31 (evening) — Why M12/M13/M14 were silent while M11 kept trading, and the fix

## Symptom

M11 fired alerts and recorded paper trades every session. M12/M13/M14 ran
green five-minute cycles but produced **zero entry decisions** — their state
files showed `prev_meta: INSUFFICIENT count 3` and
`M1x STRICT NO-ENTRY: previous closes are stale/unavailable` all session.
M14 additionally red-Xed its scheduled run at 17:41 UTC with a failing
"post-close prev-close reseed" step.

## The root cause: a 15:20 "official close" floor vs Yahoo's 15:10/15:15 reality

Every prev-close seeding path for M12/M13/M14 required the session's **final
5-minute bar to be ≥ 15:20**. Measured on the finalised daily history in this
repository:

| session   | files ending 15:10 | files ending 15:15 | files ending 15:25/15:30 |
|-----------|--------------------|--------------------|--------------------------|
| 2026-08-28 | 73                 | 134                | **3**                    |
| 2026-08-31 | 140                | 67                 | **3**                    |

Yahoo's NSE 5-minute feed simply **stops at 15:10–15:15 for ~99% of
symbols** — there is no 15:20/15:25 bucket after the 15:15 candle for most
names. The floor therefore passed exactly those ~3 symbols on **every** path:

1. the 15:25-cycle EOD intraday seed (`eod_cache_count: 3`),
2. the daily-history top-up (same floor → still 3),
3. the morning `self_heal` (3/210 — see `prev_heal` in the state files),
4. M14's 16:10 IST post-close reseed workflow (INSUFFICIENT → exit 2 → red X).

With the cache never reaching the required 180/210 symbols, the runners fell
back to `data/history` — which was itself frozen at 2026-08-18 (the bootstrap
job's commit step sat behind `label_learn.py` and was cancelled at the 55-min
timeout six days running) — so Monday 2026-08-31 enforced **strict no-entry
for the whole session** on all three models.

**M11 was never affected** because it reads the previous day straight from
local history with no bar-time floor — which is exactly the policy this fix
adopts for M12/M13/M14.

## The fix

### 1. Correct close floor (`seed_prev_context.py`, all three runners)
`CLOSE_BAR_FLOOR = "15:05"` — a session's last bar at/after 15:05 of a closed
session is the official close (identical to what M11 and the runners'
history-bootstrap fallback have always used). A truncated mid-afternoon
snapshot (e.g. a 14:55 last bar from a lagged fetch) is still rejected. The
old hard-coded 15:20 comparisons in `m12_runner` / `m13_runner` / `m14_runner`
EOD seeds now use this constant. A stricter floor can still be passed
explicitly (`--min-bar 15:20`, `min_bar=` parameter).

### 2. Local-history-first baseline (`seed_prev_context.py`)
New `local_prev_context()` reads `data/history/*.csv` (refreshed every
morning at 08:45 IST by `2. Bootstrap`) and `daily_prev_context(...,
use_local=True)` only fetches from Yahoo the symbols the local files miss.
The morning `self_heal`, the in-runner post-close reseed, and the CLI now all
run local-first, so rebuilding a baseline is **zero-network and cannot be
rate-limited** — it also stops the 210-fetch/hour retry storms that fed
Yahoo's 429s. Verified on real data:
`python -u seed_prev_context.py --models m12,m13,m14 --date 2026-08-31`
now returns **210/210 OK in ~10 s from local history** (previously 2-3/210).

### 3. Workflow parity for M12/M13 (`.github/workflows/12_live_m12.yml`, `13_live_m13.yml`)
Both now have the same 16:10 IST post-close reseed M14 already had
(`cron 40 10 * * 1-5` plus dispatch `mode=reseed`): tests and the paper cycle
are skipped for that event and `seed_prev_context.py --models m12|m13` runs
instead, followed by the same state commit + safe push.

### 4. Reseed no longer red-Xs the run (`seed_prev_context.py` CLI)
An INSUFFICIENT result is the documented fail-closed policy (existing cache
left untouched), so the CLI now prints a loud warning and exits **0**; exit 2
is reserved for a genuinely missing model result. This is what failed M14's
17:41 UTC scheduled run.

### 5. Bootstrap commits history before the slow learners (`2_bootstrap.yml`)
The refreshed `data/history` is committed **immediately** after the download;
`label_learn.py` runs afterwards, time-boxed to 30 minutes and non-fatal,
with a best-effort commit of `learn/`. The frozen-history lockout that
started this chain can no longer happen.

### 6. Committed baselines for the next session
`data/m12_prev_close.json`, `data/m13_prev_context.json` and
`data/m14_prev_context.json` (date 2026-08-31, count 210, status OK,
source "local history seed") are committed so Tuesday 2026-09-01 starts from
a good baseline regardless of anything else. Verified:
`load_previous_closes("2026-09-01")` / `load_prev("2026-09-01")` → `OK, 210`
for all three models.

## Defense-in-depth after this fix

| failure                          | before                                   | now                                                        |
|----------------------------------|------------------------------------------|-------------------------------------------------------------|
| Yahoo lags/429s at the 15:25 EOD cycle | cache missing, top-up returns 3     | 15:05 floor accepts final bars; top-up local+network         |
| overnight reseed fails           | M14 run red-Xs; M12/M13 have no reseed   | all three reseed local-first; INSUFFICIENT is a warning      |
| bootstrap cancelled mid-run      | history freezes; next session STALE      | history committed before the learners; caches also committed |
| everything above fails at once   | strict no-entry all session              | runners' history bootstrap (no floor) still reads local files |

## Tests

`test_seed_prev_context.py` (31 tests), `test_m12_entry.py`,
`test_m13_runner.py`, `test_m14_runner.py` updated for the corrected floor
(15:10/15:15 accepted, 14:55 rejected) plus new coverage for
`local_prev_context`, local-first merging, self-heal local mode and the CLI
exit codes. Full suite green.

## Still open (manual, unchanged)

The Dhan token returns HTTP 401 — every cycle runs on the Yahoo fallback.
Rotate the `DHAN_TOKEN` secret to restore the real-time primary feed.

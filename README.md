# Workflow checkout/race fix

## 2026-08-31 (evening) — the real M12/M13/M14 root cause: 15:20 close floor vs Yahoo's 15:10/15:15 data

The morning fix below was necessary but not sufficient: every prev-close path
still required a **final bar ≥ 15:20**, while Yahoo's finalised NSE 5-minute
data ends at **15:10/15:15 for ~99% of symbols** (measured: 08-28 → 73×15:10,
134×15:15, 3×15:25). Exactly those ~3 symbols passed on *every* path — EOD
seed, top-up, self-heal, reseed — so all three models sat in strict no-entry
again while M11 (no floor) kept trading. See `M12_M13_M14_CLOSE_FLOOR_FIX.md`.

Fixes now on main:

1. `CLOSE_BAR_FLOOR = "15:05"` in `seed_prev_context.py` (and the runners'
   EOD seeds use it): a closed session's last bar ≥ 15:05 is the official
   close; a truncated 14:55 snapshot is still rejected.
2. **Local-history-first baselines**: `local_prev_context()` +
   `use_local=True` rebuild any cache from `data/history/*.csv` with zero
   network (verified 210/210 OK in ~10 s). Morning self-heal, in-runner
   post-close reseed and the CLI are all local-first now — no more 210-fetch
   retry storms feeding Yahoo's 429s.
3. **M12/M13 reseed parity**: `12_live_m12.yml` / `13_live_m13.yml` gained
   the same 16:10 IST post-close reseed cron (`40 10 * * 1-5`) and dispatch
   `mode=reseed` that M14 already had.
4. **INSUFFICIENT ≠ failure**: the reseed CLI prints a warning and exits 0
   when it correctly refuses to write a partial cache (this is what red-Xed
   M14's 17:41 UTC run); exit 2 is reserved for a missing model result.
5. **Bootstrap order fixed for real**: `2_bootstrap.yml` commits the
   refreshed `data/history` immediately after the download; `label_learn.py`
   runs after, time-boxed to 30 min and non-fatal. The frozen-history
   lockout cannot recur.
6. **Baselines committed**: `data/m1{2,3,4}_prev_*.json` (2026-08-31, 210
   symbols, OK, local seed) are in the repo so the next session starts clean;
   verified `load_prev("2026-09-01")` → OK/210 for all three models.

Still open (manual): the Dhan token returns **HTTP 401** — every cycle runs on
the Yahoo fallback. Rotate the `DHAN_TOKEN` secret to restore the real-time
primary feed.

## 2026-08-31 — M12/M13/M14 silent-lockout fix

Symptom: M12/M13 workflows ran green every 5 minutes but fired zero entry
alerts; M14 never ran at all. Three stacked causes, three fixes:

1. **M14 had no workflow.** `14_live_m14.yml` now exists (same five-minute
   ticker, tests, state commit, safe push as M12/M13; alert targets fall back
   M14→M13→M11 env chains automatically).
2. **`2. Bootstrap` was cancelled at its 55-min job timeout 8+ days running**
   while `label_learn.py` grinded, so the commit publishing the refreshed
   history never executed and `data/history` stayed frozen at 2026-08-18. The
   workflow now commits the refreshed history **immediately** after the
   download; the slow learners run afterwards, time-boxed and non-fatal.
3. **The EOD prev-close seed could not reach 180/210 symbols** when Yahoo
   lagged the 15:25 IST cycle (Friday 08-28 produced a 3-symbol cache →
   Monday's runners enforced strict no-entry). Each runner's EOD seed now tops
   up from the daily 5-minute history via `seed_prev_context.py`
   (`SEED_PREV_DAILY_TOPUP=0` disables), and every live workflow runs a
   **16:10 IST post-close reseed** (`cron 40 10 * * 1-5`, or dispatch
   `mode=reseed`) that rebuilds the cache from finalised daily data.

Still open (manual): the Dhan token returns **HTTP 401** — every cycle runs on
the Yahoo fallback. Rotate the `DHAN_TOKEN` secret to restore the real-time
primary feed.

## Confirmed behavior

M12 and M13 are now technically working:

- current history is fresh;
- previous context is OK;
- both feed 210 stocks after Dhan 429 -> Yahoo fallback;
- both scanners populate 210 symbol cursors;
- both record candidate decisions.

Today M12 rejected 7/7 candidates on its anti-chase gate. M13 rejected 5/7 on opening breadth and 2/7 on missing S1. Zero trades is therefore a strategy result, not a scanner/feed failure.

## Why scheduled workflow runs still fail

A queued GitHub Actions event checks out the commit SHA captured when the event was created. While it waits, an earlier run of the same model commits its state. The queued run later modifies an old copy of the same state file; `git pull --rebase` conflicts and the commit step fails.

Manual runs often succeed because they start from a newer SHA. Paper-cycle steps pass; scheduled failures occur in the commit step.

## Fix

Every state-writing workflow now checks out the latest `main` when the job actually starts:

```yaml
- uses: actions/checkout@v5
  with:
    ref: main
    fetch-depth: 0
```

They also use one cycle per dispatch and the shared safe rebase/retry push helper.

## Upload

- Upload `workflow_safe_push.sh` to repository root.
- Upload all included `.yml` files to `.github/workflows/`.

## Operational cleanup

1. Cancel pending/queued runs created before the upload.
2. Disable external cron temporarily. Internal repository schedules are already active.
3. Let one clean schedule cycle complete for each model.
4. Re-enable only a market-hours-only backup scheduler if needed.

Do not run internal five-minute schedules and an external full-frequency scheduler simultaneously.

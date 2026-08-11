# Bootstrap fix required for M12/M13

## Diagnosis

M12 and M13 are running successfully, but both are fail-closed before scanning:

- M12 expects previous weekday 2026-08-10, but committed history ends 2026-07-30.
- M13 has no valid previous-close context; its prior VIX input is valid (+0.5766%).
- `state12.json` and `state13.json` therefore contain zero signals and zero decisions.

The daily Bootstrap workflow's download and label steps succeed, but its final push fails because other paper workflows advance `main` during the 20–30 minute refresh. The old step ran `git push` without rebasing.

## Fix

Upload the included `2_bootstrap.yml` to:

`.github/workflows/2_bootstrap.yml`

The replacement commits generated data, rebases against moving `main`, and retries the push up to six times.

## After upload

1. GitHub Actions → `2. Bootstrap — daily history download` → Run workflow.
2. Wait for the complete workflow, including `commit data safely against moving main`, to pass.
3. Confirm `data/history/*.csv` contains 2026-08-10 bars.
4. Run M12 and M13 once in `live` mode during 09:45–12:00 IST.
5. Confirm state metadata changes from `STALE` to `OK` and decisions begin appearing.

Do not loosen M12/M13 entry gates. They currently have no candidates because the scanner is intentionally disabled by stale context.

## Scheduler cleanup

Both workflows already have built-in five-minute market schedules. External cron is also dispatching them hourly outside market hours. Use either the internal schedule or a market-hours-only external backup; avoid two overlapping full-frequency schedulers because they create queue and branch churn.

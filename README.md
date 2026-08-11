# Final runtime stability fix

## Confirmed causes

1. Bootstrap was stale because old runs downloaded data but failed their direct push.
2. The corrected Bootstrap has now succeeded (`bootstrap 2026-08-11_0556Z`) and history includes current 11-Aug bars plus the required 10-Aug previous session.
3. Existing model workflows still use two full cycles and unsafe push handling; concurrent runs are cancelled or fail at `commit state + reports`.
4. M12/M13 states were created while history was stale. Their first post-Bootstrap run starts with no cursor and tries to replay every intraday bar for every stock, even though stale signals cannot be executed. That can exceed the job window.

## Fixes in this package

- `workflow_safe_push.sh`: rebase-and-retry state/report pushes.
- All live workflows: one cycle per dispatch and safe push helper.
- Bootstrap: safe data push.
- M12/M13 runners: on the first valid mid-session run, skip old bars and process only the newest closed bar. This is consistent with the no-late-entry rule and prevents thousands of pointless scanner replays.

No strategy gate, score, stop, exit, position size or alert rule is changed.

## Upload

- Upload `workflow_safe_push.sh`, `m12_runner.py`, and `m13_runner.py` to repository root.
- Upload all `.yml` files to `.github/workflows/`.

## Then

1. Cancel pending/queued runs created before this upload.
2. Temporarily disable external cron dispatches. Internal repository schedules are already active.
3. Run M12 once in live mode.
4. Run M13 once in live mode.
5. Confirm `prev_meta.status=OK`, `signals` populates, and decisions begin appearing.
6. Re-enable only a market-hours-only backup scheduler if needed.

# Workflow checkout/race fix

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

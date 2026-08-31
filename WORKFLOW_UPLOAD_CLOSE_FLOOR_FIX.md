# Upload instructions — workflow changes for the M12/M13/M14 close-floor fix

The automated agent token cannot push `.github/workflows/**` changes
(GitHub App without `workflows` permission), so the code fix is merged via
PR while the **three workflow files must be uploaded manually by you** —
same procedure as the previous `workflows_m12_m13_m14.patch`.

Everything needed is in **`workflows_close_floor_fix.patch`**, and the exact
final file contents are already in this branch's working tree
(`arena/01a05927-nse-oi-scanner`).

## What to change (3 files, on `main`)

### 1. `.github/workflows/12_live_m12.yml`
- `mode` input description: add `· 'reseed' = rebuild prev-close cache only`
- add schedule: `- cron: "40 10 * * 1-5"` (16:10 IST post-close reseed)
- `M12 entry/feed tests` step gets:
  `if: github.event.schedule != '40 10 * * 1-5' && github.event.inputs.mode != 'reseed'`
- `M12 paper cycle` step gets:
  `if: github.event.inputs.mode != 'test-alert' && github.event.inputs.mode != 'reseed' && github.event.schedule != '40 10 * * 1-5'`
- new step after the paper cycle:
  ```yaml
  - name: M12 post-close prev-close reseed
    if: github.event.schedule == '40 10 * * 1-5' || github.event.inputs.mode == 'reseed'
    run: python -u seed_prev_context.py --models m12
  ```

### 2. `.github/workflows/13_live_m13.yml`
Identical to the above with `M13` / `--models m13` names.

### 3. `.github/workflows/2_bootstrap.yml`
Replace the single `label_learn.py` + one-commit sequence with:
1. `commit refreshed history immediately` — commits **`data/` only** right
   after `m9_bootstrap.py`, before anything slow runs (this is what froze
   history at 2026-08-18 for 8 days when the commit waited behind
   `label_learn.py` and the 55-min job timeout cancelled it six runs in a
   row).
2. `learning-log labels (time-boxed, non-fatal)` —
   `timeout 30m python -u label_learn.py || echo "label_learn incomplete (non-fatal)"`
3. `commit learn/ best-effort` — same rebase/retry push loop but `exit 0` on
   failure.

## How to apply

Easiest (web UI, ~2 minutes):

1. Open each of the three files above on `main` in GitHub → pencil icon.
2. Replace the whole content with the version from the
   `arena/01a05927-nse-oi-scanner` branch (or apply
   `workflows_close_floor_fix.patch` locally and push from your own
   machine, where you have full permissions).
3. Commit directly to `main`.

Or from your own machine with a PAT that has workflow scope:

```bash
git checkout main && git pull
git apply workflows_close_floor_fix.patch
git add .github/workflows && git commit -m "workflows: m12/m13 post-close reseed + bootstrap commit order" && git push
```

## Verify after uploading

1. `gh workflow run "12. LIVE M12 — selective reversion (max 5/day, paper-only)" -f mode=reseed`
   → the run should log
   `seed_prev_context[m12]: wrote 210 closes (…) -> m12_prev_close.json`
   (source: `local history seed`) and stay green.
2. Same for M13/M14 with `mode=reseed`.
3. Next session open (09:15 IST), each M12/M13/M14 cycle should print
   `previous-close source: {'status': 'OK', …, 'count': 210}` instead of
   `STRICT NO-ENTRY`.

Until the upload is done, the code merged via the PR already fixes the
runners themselves: the morning self-heal rebuilds the cache from local
history (verified 210/210), and M14's existing reseed cron no longer red-Xs.

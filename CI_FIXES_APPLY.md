# M12 / M13 / M14 — root cause and the fix that must be applied

**Status (2026-08-31):** All **code** fixes are already merged on `main`
(strict fail-closed previous-close loader, cursor clamp, partial-seed refusal,
post-close self-heal reseed inside each runner — every M12/M13/M14 unit suite
passes). The models were still silent for three **CI/workflow** reasons:

| # | Root cause | Effect |
|---|---|---|
| 1 | **M14 never had a workflow** (`14_live_m14.yml` did not exist) | M14 never ran at all — no state, no alerts, no reports |
| 2 | `2_bootstrap.yml` ran `label_learn.py` **before** the data commit and timed out at 55 min | The history commit was skipped every night, so `data/history/` stayed frozen at 2026-08-18 → stale prev-close baseline → M12/M13 strict no-entry |
| 3 | No post-close reseed schedule | A short EOD seed (3/210 symbols on Yahoo-lagged days) never healed before the next open |

## Apply the fix (requires workflows permission)

The ready-to-apply diff is committed in the repo as **`ci_workflows_fix.diff`**
(it contains the new `14_live_m14.yml` plus the `2_bootstrap.yml`,
`12_live_m12.yml`, `13_live_m13.yml` changes). Run as the repo owner:

```bash
git checkout main && git pull
git apply ci_workflows_fix.diff
git add .github/workflows
git commit -m "ci: fix M12/M13/M14 — M14 workflow, commit-first bootstrap, 16:10 IST reseed"
git push origin main
```

Or merge the Arena PR carrying this change and click **"Allow edits / workflows
from this fork"** when prompted — the Arena automation token lacks the GitHub
App `workflows` permission, so workflow files cannot be pushed by the bot
directly (GitHub refuses the push with: *"refusing to allow a GitHub App to
create or update workflow … without `workflows` permission"*).

Verify afterwards:

- https://github.com/atikhalde/nse-oi-scanner/actions/workflows/14_live_m14.yml exists and fires on the 5-min ticker
- The next 08:45 IST bootstrap run commits `data/` immediately after the refresh step
- `state12.json` / `state13.json` / `state14.json` show `"prev_meta": {"status": "OK", "date": "<previous weekday>"}` with count ≥ 180

## Still needs a manual secret rotation

`DHAN_TOKEN` returns **HTTP 401** every cycle (expired). All models run on the
~15-min-delayed Yahoo fallback. Rotate at
**Repo → Settings → Secrets and variables → Actions → `DHAN_TOKEN`**.

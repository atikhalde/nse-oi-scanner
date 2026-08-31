# Apply the workflow patch (M14 go-live + bootstrap fix + post-close reseed)

This session's automation token cannot write `.github/workflows/**` (GitHub
Apps need an explicit Workflows permission for that). All **code** fixes are
already merged via the PR; the four workflow changes are bundled in
`workflows_m12_m13_m14.patch`. Apply them from a account/token that can edit
workflows (repo owner, maintainer, or an Arena reconnect with workflows
permission):

```bash
git pull
git apply workflows_m12_m13_m14.patch
git add .github/workflows
git commit -m "ci: M14 live workflow, bootstrap commit-first, 16:10 IST post-close reseed"
git push
```

Verify: https://github.com/atikhalde/nse-oi-scanner/actions/workflows/14_live_m14.yml
shows the new workflow, and the next scheduled cycle fires.

## What each change does

| File | Change | Fixes |
|---|---|---|
| `14_live_m14.yml` (new) | M14 on the same five-minute paper ticker as M12/M13: tests, `--loop 1` cycle, state/report commit, safe push. Alert targets fall back M14→M13→M11 env chains (already coded in `m14_alerts.py`). | **M14 has never run** — no workflow existed. |
| `2_bootstrap.yml` | Commit the refreshed `data/history` **immediately** after the download; `m9_bootstrap` + `label_learn` run afterwards, step-time-boxed and non-fatal. Job timeout 55→70 min. | Bootstrap was cancelled at its 55-min timeout 8+ days in a row, so the history commit never ran and `data/history` stayed frozen at **2026-08-18** — the stale prev-close baseline that put M12/M13 in strict no-entry. |
| `12_live_m12.yml` / `13_live_m13.yml` | Extra cron `40 10 * * 1-5` (16:10 IST): post-close reseed of the prev-close cache via `seed_prev_context.py`; also dispatchable with `mode=reseed`. | Belt-and-braces if the 15:25–15:35 EOD seed still cannot reach 180/210 symbols. |

Note: even **without** this patch, the merged code already self-heals M12/M13 —
their final scheduled cycle (15:35 IST) now retries the cache seed from
finalised daily data inside the runner itself. The patch is what brings M14
online (its workflow never existed) and makes the morning history bootstrap
survive its own slow learners.

## Still needs your attention

`DHAN_TOKEN` returns **HTTP 401** on every cycle (expired/invalid) — all
models currently run on the delayed Yahoo fallback. Rotate the secret to
restore the real-time primary feed:

Repo → Settings → Secrets and variables → Actions → `DHAN_TOKEN` → update.

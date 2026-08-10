# M12 + M13 fixed deployment package

Prepared against public `main` base commit:

`ab832315ffc1ec88c7c01474ce88813449a0f542`

Local implementation commits:

- `3b7cf3b8711514b77e6051610527bef412adce24` — M12 model + M13 alert/spec scaffold
- `a65953faeca06a7231a946d0048071090dc422d1` — runnable M13 paper model

## Package contents

### M12

- `m12_entry.py`
- `m12_runner.py`
- `test_m12_entry.py`
- `M12_ENTRY_MODEL.md`
- `.github/workflows/12_live_m12.yml`

### M13

- `m13_entry.py`
- `m13_trader.py`
- `m13_runner.py`
- `m13_alerts.py`
- `test_m13_entry.py`
- `test_m13_trader.py`
- `test_m13_runner.py`
- `test_m13_alerts.py`
- `M13_BEST_ENTRY_SETUP.md`
- `M13_EQUITY_MOMENTUM_EXECUTION_PLAN.md`
- `.github/workflows/13_live_m13.yml`

### Setup

- `CRON_M12_M13.md`
- `SHA256SUMS.txt`

No PAT, Telegram token, Dhan token, state file, cache, historical report or analysis output is included.

## Recommended upload method

Use a local clone of the latest repository and copy this package over it while preserving paths:

```bash
git clone https://github.com/atikhalde/nse-oi-scanner.git
cd nse-oi-scanner
cp -R /path/to/M12_M13_FIXED/. .
python test_m12_entry.py
python test_m13_entry.py
python test_m13_trader.py
python test_m13_runner.py
python test_m13_alerts.py
git add m12_entry.py m12_runner.py test_m12_entry.py M12_ENTRY_MODEL.md \
        m13_entry.py m13_trader.py m13_runner.py m13_alerts.py \
        test_m13_entry.py test_m13_trader.py test_m13_runner.py test_m13_alerts.py \
        M13_BEST_ENTRY_SETUP.md M13_EQUITY_MOMENTUM_EXECUTION_PLAN.md \
        .github/workflows/12_live_m12.yml .github/workflows/13_live_m13.yml
git commit -m "Add M12 and M13 paper models"
git push origin main
```

Do not force-push. Pull/rebase first if `main` advances.

## GitHub web upload

If using the website:

1. Upload all root `.py` and `.md` files to the repository root.
2. Upload `12_live_m12.yml` and `13_live_m13.yml` into `.github/workflows/`—not the repository root.
3. Commit normally.

## Required secrets

Existing:

- `DHAN_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Preferred M12 extras:

- `M12_BOT_TOKEN_A`
- `M12_CHAT_ID_A`
- `M12_BOT_TOKEN_B`
- `M12_CHAT_ID_B`

Preferred M13 extras:

- `M13_BOT_TOKEN_A`
- `M13_CHAT_ID_A`
- `M13_BOT_TOKEN_B`
- `M13_CHAT_ID_B`

When model-specific pairs are absent, complete M11 A/B pairs are used as fallback.

## Post-upload tests

Run the normal test jobs, then manually trigger each workflow with `mode=test-alert`:

- `12. LIVE M12 — selective reversion`
- `13. LIVE M13 — equity momentum A+`

Each test should appear once on main, A and B. Manual repetition of test mode is user-triggered; automatic trade alerts use strict at-most-once registry-first delivery.

## First-session behavior

M12 and M13 use separate previous-session caches. If fresh context is unavailable on their first session, they intentionally take no entry and seed their cache after market close. This is expected fail-closed behavior.

## Cron endpoints

- M12: `https://api.github.com/repos/atikhalde/nse-oi-scanner/actions/workflows/12_live_m12.yml/dispatches`
- M13: `https://api.github.com/repos/atikhalde/nse-oi-scanner/actions/workflows/13_live_m13.yml/dispatches`

Use a new fine-grained credential with Actions write permission. Never put credentials in the URL, repository, logs or chat.

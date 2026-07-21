# Forward Paper Test — Master Scanner + TOP-30 OI-Spurt Gate

Fully automated paper-trading of your verified strategy. **The entry logic is the
untouched `master_scanner.py` engine** (100% faithful to the TradingView Pine v6
indicator, Ex10/Ex11 OFF, verified 5/8 exact vs your screenshots). On top:

- **Gate**: stock must be in the **TOP-30** of NSE's strong OI-spurts list
  (futures+options %OI rise vs previous session, live from NSE).
- **Trade rules**: fill at signal-bar close · SL = last 5-min pivot low/high ∓ 0.02%
  (session-extreme fallback) · 50% booked at 1:2 · remaining 50% trails after 1:3
  prints (lock +2R, trail 1R) · square-off 15:20 · ₹50,000 per stock (whole shares).
- **Alerts**: every ENTRY / TP1 / TRAIL-ON / EXIT-SL / EXIT-EOD goes to your Telegram;
  the EOD Excel report lands in the same chat at ~15:25.

Monday 20/07 backtest reference: 15 trades, 12 wins, **+₹6,615 / +9.54R**.

---

## One-time setup (~10 min)

### 1. Upload the files (web only — no git, no folders)

Everything now sits **flat** — GitHub's "Upload files" button handles it fine.

1. Unzip `forward-paper-test.zip` and open the extracted `forward-paper-test` folder.
2. In your repo → **Add file → Upload files** → select **all files inside the folder
   EXCEPT the 3 helper folders** (drag-select or Ctrl+A inside the folder). Do **not**
   skip `master_scanner.py` (~99 KB) — that's the engine.
3. Commit to `main`.
4. The only folder GitHub still needs is `.github/workflows`. If your repo already has
   it (Actions tab shows the 3 workflows) you're done. Otherwise create each file with
   **Add file → Create new file**, type the path `.github/workflows/1_test.yml`
   (the `/` makes the folders), paste the matching text from `_PASTE_THESE_IN_GITHUB/`,
   commit. Repeat for `2_bootstrap.yml` and `3_live.yml`.

Final tree must look like: `live_runner.py`, `master_scanner.py`, `feeds.py`, `gate.py`,
`trader.py`, `report.py`, `telegram_bot.py`, `test_master_scanner.py`,
`fno_universe.csv`, `replay_trades.csv`, `requirements.txt`, `README.md`,
`.github/workflows/` with the 3 yml files. (`data/history/` is created automatically.)

### 2. Add 3 secrets — Settings → Secrets and variables → Actions → New repository secret

| Secret name | Value |
|---|---|
| `DHAN_TOKEN` | fresh Dhan access token — generate on web.dhan.co → DhanHQ Trading APIs. **Don't regenerate again while the test runs** (regeneration kills the previous token — happened twice). If left empty, the runner falls back to Yahoo (bars ~15 min delayed). |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TELEGRAM_CHAT_ID` | your chat id: message your bot once, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `"chat":{"id": …}` |

### 3. Prove it works — run the TEST workflow

Actions tab → **“1. TEST — connectivity & engine”** → Run workflow.
It checks secrets, the engine self-test, a Yahoo + Dhan data pull, the OI-gate probe,
and sends a ✅ test message to your Telegram. Green = ready.

### 4. First bootstrap (mandatory, once)

Actions → **“2. Bootstrap — daily history download”** → Run workflow (~15-25 min).
Downloads ~3 months of 5-min bars for all 210 stocks into `data/history/` and commits
them. It then auto-runs every weekday 08:45 IST to refresh.

### 5. That's it — hands-free from 09:16 IST

**“3. LIVE”** fires every 5 min in market hours (09:16–15:25 IST, Mon–Fri):
fetch → run engine on 210 stocks → gate by live top-30 OI-spurts → paper-enter →
manage SL/TP1/trail → Telegram alerts → commit `state.json`. At 15:25 it builds the
Excel report and sends it as a Telegram document.

---

## How the pieces behave (so you're never guessing)

- **Deterministic trader**: every cycle re-evaluates open trades from the day's full
  bar set, so restarts/queued runs can never duplicate a trade; events are
  Telegram-deduped via keys in `state.json`.
- **Strict gate** (your choice): if NSE's live spurts feed is unreachable, signals are
  logged — **no entry alert fires**. Check the Actions log if a quiet day seems odd.
- **Raw master signals also come to Telegram** (info-only, no paper trade): every
  master that fails the TOP-30 gate — or fires while the gate is offline — arrives as
  📡 `MASTER SIGNAL — SYM SIDE … no trade`, once per signal. Gated trades still send
  their usual ENTRY/TP1/TRAIL-ON/EXIT alerts exactly as before.
- **NSE live API** can be flaky from datacenter IPs; the runner retries and reports
  gate status every cycle (`gate: OK source=…` / `OFFLINE`). Bootstrap's `oi_prev.csv`
  is a secondary input, but strict mode requires the live NSE list.
- **Rate safety**: ~7 min per full cycle worst case (210 symbols via Dhan at ~1 req/0.75s).
  The `concurrency` lock queues cron ticks; nothing overlaps.
- **Costs not included** (brokerage/STT/slippage) — paper prices are raw fills.
- Pause everything: Actions → **3. LIVE** → `⋯` → Disable workflow.

## Repo layout

```
live_runner.py            orchestrator (--test / --bootstrap / --live / --replay)
master_scanner.py         YOUR ENGINE — byte-identical to the verified build (md5 a5dc43b2…)
trader.py                 exact SL/TP1/trail/EOD rules + ₹50k sizing + alert texts
gate.py                   live TOP-30 NSE OI-spurt evaluation
feeds.py                  Dhan (real-time) + Yahoo (backup) 5-min feeds
report.py                 EOD xlsx + Telegram summary
telegram_bot.py           sendMessage / sendDocument wrappers
test_master_scanner.py    engine self-test
fno_universe.csv          all 210 FnO stocks + Dhan securityIds
replay_trades.csv         Monday's 15 gated entries (for --replay validation)
state.json                today's signals/trades/alerts (auto-committed)
data/history/*.csv        ~63 sessions of 5-min bars per stock (auto-committed)
.github/workflows/        1_test · 2_bootstrap · 3_live
```

## VM mode (own cloud box — no GitHub dependence)

On any Ubuntu/Debian VM (Oracle Always Free recommended):

```bash
curl -sL https://raw.githubusercontent.com/atikhalde/nse-oi-scanner/main/vm-setup.sh | bash
cp .env.example .env && nano .env        # fill DHAN_TOKEN / TELEGRAM_* values
./run-live.sh && tail -5 logs/live.log   # smoke test
```

Done — VM cron runs every 5 min (`*/5 3-10 * * 1-5` UTC) + bootstrap 08:45 IST.
`flock` prevents overlaps; state.json lives on the VM; updates arrive via daily
`git pull` in run-bootstrap.sh. Logs: `tail -f logs/live.log`.
Disable the GitHub "2. Bootstrap" and "3. LIVE" workflows once the VM is live.

## Validate offline anytime (no market needed)

```bash
python live_runner.py --replay 2026-07-20 --trades-csv replay_trades.csv
```

re-runs Monday's 15 gated entries through the trader (bars from `data/history/`),
prints every alert text, and regenerates the report — exact match with the delivered
Monday backtest if run on Dhan-sourced bars.

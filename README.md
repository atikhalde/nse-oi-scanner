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

### 1. Create the repo and push

```bash
git init
git add -A
git commit -m "paper test v1"
git branch -M main
git remote add origin https://github.com/<YOU>/<REPO>.git     # private repo recommended
git push -u origin main
```

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
src/master_scanner.py     YOUR ENGINE — byte-identical to the verified build (md5 a5dc43b2…)
src/trader.py             exact SL/TP1/trail/EOD rules + ₹50k sizing + alert texts
src/gate.py               live TOP-30 NSE OI-spurt evaluation
src/feeds.py              Dhan (real-time) + Yahoo (backup) 5-min feeds
src/report.py             EOD xlsx + Telegram summary
src/telegram_bot.py       sendMessage / sendDocument wrappers
fno_universe.csv          all 210 FnO stocks + Dhan securityIds
state.json                today's signals/trades/alerts (auto-committed)
data/history/*.csv        ~63 sessions of 5-min bars per stock (auto-committed)
.github/workflows/        1_test · 2_bootstrap · 3_live
```

## Validate offline anytime (no market needed)

```bash
python live_runner.py --replay 2026-07-20 --trades-csv replay_trades.csv
```

re-runs Monday's 15 gated entries through the trader (bars from `data/history/`),
prints every alert text, and regenerates the report — exact match with the delivered
Monday backtest if run on Dhan-sourced bars.

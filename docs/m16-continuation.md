# M16 — independent small-pullback / BOS continuation alerts

M16 is an **alerts-only** model. It does not place orders or reuse M2/M6 trade ledgers, bot credentials, sector boards, persistent scores, leader ranks, sizing or exits. It imports M6's read-only history, normalization and feed adapters, so the universe and Yahoo-primary / Dhan-fallback provider path are shared. It never writes M6 state.

## Rules (IST; five-minute candles)

1. Evaluate the existing master indicator causally, with **all exception switches enabled**, including BUY EX10/EX11. Track all final chart-label normal/EX BUY/SELL codes. Scanner-table previews 90/290 are excluded. Do not apply M2's trade-quality, OI, early-EX or open-position selection gates.
2. The initial signal must be **confirmed strictly before 11:00**. Thus a 10:50 candle confirmed at 10:55 qualifies; a 10:55 candle confirmed at 11:00 does not. Only completed candles create seeds.
3. Each original label creates an independently tracked seed. The BUY anchor is the lowest low of the last five available session candles through the seed candle; the SELL anchor is the highest high. This is an explicit causal approximation, not a claim to reproduce a TradingView drawing's proprietary swing algorithm.
4. Track the favorable rally extreme following the seed, but **do not qualify sideways action as a rally**. At the pre-pullback extreme, the last up-to-six completed candles (beginning no earlier than the seed) must contain at least three candles and two directional close-to-close advances, at least 0.3% net progress in the trade direction, directional efficiency >=0.65 (net progress / sum of absolute close changes), and both EMA9 and EMA10 sloping in the trade direction across that window. Store this evidence at the extreme, before the pullback—not using the later breakout. Also require at least 0.1% progress from signal close to the favorable extreme. These are explicit starting parameters, not a profitability-tested definition.

   **EMA continuity:** from the original signal candle through the BOS confirmation candle, BUY closes must remain at or above **both EMA9 and EMA10**. SELL closes must remain at or below **both**. A close through either EMA permanently invalidates that seed; touching/crossing with a wick or closing exactly on an EMA is allowed. Missing EMA data or a missing completed candle fails closed, because continuity cannot be verified. A provisional running breakout whose candle violates this rule receives `CLOSE_FAILED`, not confirmation.

   After a qualifying rally, on a **later, separate completed candle**, recognize either:
   - A wick intersecting the 23.6%–38.2% retracement band of the anchor-to-extreme leg; **or**
   - A wick touching EMA9 **or** EMA10. EMA calculations include prior-session warm-up, like the shared scanner frame.
5. Freeze the extreme as BOS when the pullback is recognized. Do not extend that BOS using future candles. A candle making a new favorable extreme cannot also establish the pullback: OHLC cannot reveal that intrabar order.
6. Small-pullback safeguard: retracement greater than 50% of the leg invalidates the setup. An anchor break also invalidates it. These are starting defaults, not profitability-optimized parameters.
7. BUY when observed running price crosses above frozen BOS; SELL mirrors below BOS. Use the current received candle close/last price, not the bar's historical wick. Stop after one outcome per seed. Multiple seeds converging on the identical symbol/side/BOS/pullback/break candle produce only one alert pair.
8. Both setup and breakout confirmation must occur **strictly before 12:30**. The latest eligible breakout candle is therefore 12:20–12:25; a 12:25–12:30 candle is excluded. Unfinished setups expire at 12:30.

## Two-stage alert contract

- **RUNNING BOS — unconfirmed:** first eligible observed price beyond BOS during a forming candle. Can fail before candle close. It is not a guaranteed fill at the BOS price.
- **CANDLE CLOSE — BOS CONFIRMED:** the same candle closes beyond BOS with structure intact.
- If that candle does not hold BOS/structure: **CANDLE CLOSE — BOS FAILED (not an entry)** instead of a false confirmed-entry alert.
- If no running crossing was observed but a fresh close qualifies, send a clearly labeled **CLOSE_ONLY** notification. Never fabricate an intrabar alert after the candle has finished.
- If the trigger candle is missing, record `CLOSE_UNAVAILABLE`; do not confirm using a different candle.
- Historical reconstruction records events but suppresses stale alerts. Default close/send freshness budget is 30 seconds. Failed sends retry within that budget with a short backoff. An outbox left `sending` after a crash is uncertain and is not blindly duplicated.

## Timing / feed limitations

Default watched-symbol poll cadence is one second (`M16_POLL_SECONDS`, minimum 1). Universe discovery is aligned to five-minute close +1 second before 11:00. Asynchronous fetches and separate active/discovery analysis workers prevent a whole-universe batch from holding up active setups. Late discovery bars are re-fetched within the initial freshness budget.

**This does not guarantee one-second market-to-Telegram latency.** The shared Yahoo/Dhan five-minute bar APIs are not a real-time tick stream, can publish/cache forming bars late, and do not provide a trustworthy tick timestamp through this adapter. Request duration, rate limiting, computation, Telegram and GitHub Actions scheduling can all add delay. Alerts show observed times. A dedicated low-latency quote stream and continuously running host would be required for a stronger intrabar SLA. Multiple active requests and existing models share provider rate limits; monitor before increasing load.

## Separate Telegram bot activation

Create the new Telegram bot using BotFather, open/start it from the destination account/group, and configure **repository Actions secrets** via GitHub Settings → Secrets and variables → Actions:

- `M16_BOT_TOKEN`
- `M16_CHAT_ID`

Optional **second, independent M16 bot** (receives all running/close alerts, self-tests and the TXT review):

- `M16_BOT_TOKEN_2`
- `M16_CHAT_ID_2`

Set both mirror secrets, or leave both absent for single-bot mode. A half-configured mirror is ignored with a warning; an identical token/chat pair is deduplicated. The primary pair remains required. Delivery acknowledgements are recorded separately per destination using hashed identifiers (not credentials); a failed mirror retry does not resend to a primary that already accepted the alert, and vice versa. A crashed `sending` destination remains uncertain, while the other bot can still receive its copy. The self-test succeeds only when every configured destination confirms delivery.

Do **not** paste credentials in chat or commit them. These names never fall back to M6/default credentials. `DHAN_TOKEN` remains the optional shared market-data fallback token, not a bot token.

After the code is merged and the secrets are set, manually run workflow **16. LIVE M16 — pullback BOS continuation (alerts-only)** with mode `test-alert`. Verify the dedicated bot receives it, then run mode `session` during the weekday 09:15–12:30 IST window. The workflow includes a schedule backup and the existing M15 workflow-completion trigger, but GitHub scheduling is best effort. No bot creation or successful live send is implied by offline tests.

The session refuses stale, outside-window and weekend starts. A continuous session is required for intrabar monitoring; `--once` is a diagnostic snapshot, not an adequate intrabar scheduler.

## Files and commands

- `m16_model.py`: causal setup state machine, cutoffs and event creation.
- `m16_runner.py`: feed/indicator integration, independent session and outbox.
- `state16.json`: daily seeds, cursors, phases, observations, outbox and diagnostics. Atomic writes; corrupt state fails closed. Date rollover starts a new ledger.
- `activity_M16_YYYY-MM-DD.txt`: attachment-only review at the end of the tracking window, containing all saved setup/event details and delivery statuses. No fabricated P&L or automatic trade management. It summarizes before marking EOD delivered, with per-destination acknowledgements. An uncertain crash-send is not blindly duplicated; its status remains available for manual review.
- `.github/workflows/16_live_m16.yml`: isolated workflow/concurrency; saves only M16 state/reports.

```sh
python -m unittest -v test_m16_runner
python m16_runner.py --test-alert --dry-run
python m16_runner.py --test-alert
python m16_runner.py --session
python m16_runner.py --replay-date 2026-11-20 --symbols AXISBANK
```

Replay reads local history only, prints the audit, sends nothing and writes no state. It **cannot establish historical intrabar crossing times** from completed five-minute bars.

## AXISBANK reference check

Using the repository's 20-Nov-2026 AXISBANK bars and the actual causal indicator:

- Original BUY-EX candle: **09:30–09:35**.
- Frozen BOS: **₹1,394.00**. The reference also passes the directional-rally and unbroken EMA-close filters.
- Recognized pullback candle: **10:20–10:25**.
- Confirmed BOS-break candle: **10:35–10:40**, close approximately **₹1,394.90**.

This matches the requested close-based chart example. The offline regression records it as `CLOSE_ONLY`, not an invented running-candle alert. Historical indicators/feeds can differ from TradingView; this single example is not a profitability validation.

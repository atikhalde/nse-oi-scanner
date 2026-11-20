# M6 daily activity review

## Delivery

At EOD, M6 sends **attachments only**, with no EOD Telegram text messages:

- `activity_M6_YYYY-MM-DD.txt`: a UTF-8 trade summary and activity review, followed by the full saved audit (complete observed candle journal, trade ledger, skips, suppressed records and diagnostics).
- The existing `paper_test_M6_YYYY-MM-DD.xlsx` report.

Both configured M6 Telegram destinations receive these documents with short captions. No activity JSON is sent. Internal state remains JSON. Intraday alerts are unchanged.

The existing EOD done-marker prevents duplicate EOD runs; it is not a guarantee of network delivery. Dry runs do not write or send the audit file. The workflow and runner git-sync preserve the activity TXT alongside the normal report.

## Recorded at every evaluated candle

`state6.json.day_activity[HH:MM]` contains:

- All available sectors' momentum/flow, day returns, qualification and directional persistent scores.
- BUY and SELL gate status and exact rejection/acceptance reasons, including persistent >70, rank/qualification and HOLD requirements.
- Current top-five ranking snapshot with all scored peers and component scores.
- Scan counts (signals, candidates, skips, entries, pending, fetched), exits, feed diagnostics and cycle latency.
- Overall board tilt and data-availability status, including offline-board cycles.

A repeated evaluation replaces that candle's snapshot. The daily state loader resets the journal on a new date. Ranking data from an earlier candle is never presented as a current ranking.

## TXT activity review

Includes the market-tilt timeline, per-sector start/end/min/max returns and bull/bear flow, persistent-score ranges and counts above 70, gate-open candle counts and opening/closing transitions, eligible leadership-candle counts, exact block-reason counts, every saved skip and the saved trade ledger (exits/costs/P&L when present). The TXT includes the full per-candle gate reasons and ranking components in its full saved audit section for detailed analysis.

## Follow-through filter

M6 temporarily excludes a stock from that side's sector ranking after an opening gap, first-candle body, or close-to-close move **strictly larger than 2%**. Exactly 2% is allowed (with a tiny floating-point comparison tolerance).

Two subsequent consecutive closes must progress beyond the event close in the event direction. A new directional impulse restarts confirmation. Flat prices alone do not confirm. Opposite-side events do not block a trade on this side. Confirmation permits eligibility only: the stock still needs top-five rank, a fresh master signal, sector persistent score >70 and all existing entry checks. Old signals are never replayed.

## Coverage and interpretation

- This is an observed-session audit, not an all-universe counterfactual backtest. Stocks behind closed sector gates are not run through the signal engine.
- Skip records can include historical signals and data issues; they are not a count of unique profitable opportunities missed.
- Open-position symbols can be omitted from new-entry scans; ranking still includes available peers regardless of open positions.
- Sector return changes describe sector behavior, not hypothetical stock-trade P&L.
- No intraday history is invented for legacy state files or unobserved candles. Collection begins with deployment; it cannot reconstruct previous sessions.
- Volume participation is session-relative, not historical time-of-day RVOL. Ranking weights and the 2% threshold are starting parameters, not profitability-validated.

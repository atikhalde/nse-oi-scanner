# NSE OI scanner — paper-trading research

This repository contains multiple independent paper-trading runners, their
historical reports, and the shared scanner, feed, trade and reporting engines.
No runner places real orders. Install with `pip install -r requirements.txt`.

## Current experimental model: M15

`python -u m15_runner.py --loop 1` runs a paper cycle. The scheduled workflow is
`.github/workflows/15_live_m15.yml`; it writes `state15.json`, a daily
`paper_test_M15_YYYY-MM-DD.xlsx` report, and a learning journal. Set
`DHAN_TOKEN`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as repository secrets
for feed and notifications. Without a valid Dhan feed, the existing feed module
may use its Yahoo fallback; check freshness before trusting alerts.

M15 caps entries at three daily; it does not force trades when there is no
qualifying setup. **Its preliminary replay is negative.** Read [M15_AUDIT.md](M15_AUDIT.md)
before using its alerts for anything beyond research.

M1/M2/M5–M11 remain independent historical paper controls and are not changed
by M15. The retired M12–M14 implementations, workflows, state and reports were
removed at the user's request. Their aggregate outcomes are retained only in
the audit document.

## Prospective execution audit and shadow selection

All active runner reports now include an `Execution audit` worksheet separating
final simulated exits from OPEN marks and recording first-observation delay.
`two_trade_selector.py` produces a separate M5 SELL-EX8 EOD shadow report capped
at two observations per day; it does not open independent paper positions.
See [EXECUTION_AUDIT.md](EXECUTION_AUDIT.md) for important limitations and
workflow integration instructions.

# Prospective execution audit and two-trade shadow selector

All active M1/M2/M5/M6/M7/M8/M10/M11/M15 entry paths now stamp the **first
observation time** in IST, the lag after the signal bar closed and the latest
completed bar's close as an observation **proxy**. No claim of a real bid/ask
quote or actual fill is made. Revaluations preserve this immutable first stamp.
Historical positions already in state have no first-observation evidence and
are labeled LEGACY/UNVERIFIED; never invent a timestamp for them.

Every new EOD XLSX from `report.build()` gains an `Execution audit` sheet:
entry proxy, observation proxy, lag, FINAL vs OPEN/UNVERIFIED, reported marked net,
and **final-only** net. The previous `Paper test` sheet and legacy summary retain
the original marked metrics for comparability; the audit sheet distinguishes
unrealized marks. A FINAL trade means the simulator saw a confirmed closing
bar/stop **in the feed**, not a verified broker fill. Missing 15:20 candles
leave OPEN, never converted into winning exits. Net after costs uses the same
cost model as legacy reports.

`two_trade_selector.py` is a separate EOD **shadow**, not another order model.
Frozen hypothesis: M5 SELL-EX8, first two by *first-observed time*, <=10 minutes
signal-to-observation lag, one per symbol, max two daily. It never ranks by
future P&L. It requires M5's completed EOD state and writes
`paper_selector_YYYY-MM-DD.xlsx`, with final-only results separated from OPEN.
Zero selections are valid. M5 trades already exist independently: the shadow
MUST NOT be counted as new independent trades or alerts. The selector cannot
retroactively evaluate old states lacking entry observation timestamps.

The scheduled workflow runs only when present on GitHub's default branch; its
EOD input is `origin/main:state5.json`, while it publishes shadow reports to
this session branch. Until the audit code is integrated into the branch running
M5, the selector will produce zero auditable selections. For manual verification:
`python two_trade_selector.py --day YYYY-MM-DD --state path/to/state5.json`.

Limitations: no broker acknowledgments, order book, real fill, exchange official
close, or guaranteed daily setup. Future evidence requires a quote/fill feed at
alert time and an independently verified EOD closing source.

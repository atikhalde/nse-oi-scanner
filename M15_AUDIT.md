# Entry-model audit and M15 status (26 September 2026)

Historical EOD paper XLSX files were parsed using the trade rows (side BUY/SELL,
net P&L column); **not** the sheet's gross-win summary. These are overlapping
models on the same dates, not independent samples. Four early legacy workbooks
have a different layout and were not included in the numerical trade totals.

| Model | Reports | Trades | Net wins | Win % | Net ₹ (reported) |
|---|---:|---:|---:|---:|---:|
| M1 | 47 | 522 | 222 | 42.5 | -41,223 |
| M2 | 48 | 851 | 439 | 51.6 | +16,389 |
| M5 | 45 | 756 | 394 | 52.1 | +20,382 |
| M6 | 43 | 410 | 191 | 46.6 | -16,708 |
| M7 | 45 | 6,709 | 2,650 | 39.5 | -457,835 |
| M8 | 41 | 333 | 153 | 45.9 | -7,051 |
| M10 | 39 | 708 | 306 | 43.2 | -38,816 |
| M11 | 40 | 1,982 | 846 | 42.7 | -98,458 |
| M12 | 35 | 3 | 0 | 0 | -628 |
| M13 | 36 | 10 | 5 | 50 | -1,786 |
| M14 | 22 | 17 | 8 | 47.1 | -1,300 |

M9A/B/C each have one report and no trade rows. Counts are approximate
historical evidence, not an out-of-sample performance guarantee. In particular
M12–M14 were severely constrained by feed/previous-close and signal-age gates;
their tiny executed samples cannot establish their underlying signal quality.
M2/M5 are the only sizeable positive net cohorts, but each averages many more
than three trades per session; their aggregate results do not prove that their
first three trades are profitable.

## Fresh hypothesis: M15

M15 trades a *same-candle failed break and reclaim* of the initial six 5-minute
bars' range, with directional close and a minimum reclaim fraction. It ranks
simultaneous completed signals by reclaim strength, limits entries to three
per session and one per stock, and uses the existing paper exit engine and EOD
XLSX/Telegram path. It does **not** reuse M12–M14 gates or prior-close caches.
It cannot responsibly promise at least one qualifying trade every day.

**Important negative check:** exploratory chronological replay on the nine
available sessions from 15–25 September (all history symbols, first three
signals in timestamp/score order; no transaction-at-close delay, no warmup)
produced 27 paper trades, **25.9% net winners and ₹-2,230 net**. This fails the
requested high-accuracy goal. The strategy is enabled **for paper measurement
only**, not recommended for real orders. No threshold was optimized on these
nine sessions. The replay uses cached final bars, so live feed freshness and
fill slippage may make performance worse. Do not promote based on this sample.

The next credible step is to collect forward M15 reports and only retain a
variant with positive held-out, cost-adjusted expectancy, sufficient daily
coverage and acceptable drawdown. Forcing 1–3 entries on days without a valid
setup would undermine the requested consistency.

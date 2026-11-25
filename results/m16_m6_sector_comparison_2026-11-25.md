# M16 vs M6 sector gate — 25 November 2026

Analysis only: no live strategy or PR rule changes. Source: user-supplied M6 full saved audit, M16 normal replay, repository home-sector mapping.

## Timing and scope

M6 journal keys are candle OPEN times, not close times. Each M16 event uses the M6 snapshot for the same five-minute candle; the board is available only after that candle closes. This is a candle-close sensitivity comparison, not proof that the same board was available for an earlier running breakout. Reported cycle latency is 1 second; exact ordering within that second is not established. Full-sector means saved board.entry_ok, including qualification/top-2-or-hold/persistence; it does NOT include M6 stock top-5, leadership or follow-through filters.

## Counts

- Baseline: 30 candidates.
- Directional persistent score >70 only: 11.
- Persistent top-3 AND directional score >70: 10.
- Full saved M6 sector gate: 7.

| Stock | Side | Sector | Persistent score | Rank | Top-3 + >70 | Full sector |
|---|---|---|---:|---:|---|---|
| AUROPHARMA | BUY | PHA | 46.81 | 6 | Block | Block |
| BIOCON | BUY | PHA | 46.81 | 6 | Block | Block |
| DIVISLAB | BUY | PHA | 46.81 | 6 | Block | Block |
| TATACONSUM | BUY | FMCG | 70.60 | 2 | Pass | Pass |
| GMRAIRPORT | SELL | INFRA | 38.58 | 15 | Block | Block |
| HINDUNILVR | BUY | FMCG | 78.97 | 2 | Pass | Pass |
| ITC | BUY | FMCG | 78.97 | 2 | Pass | Pass |
| BHARATFORG | SELL | AUTO | 41.60 | 14 | Block | Block |
| DIXON | SELL | CDUR | 69.67 | 5 | Block | Block |
| NAM-INDIA | SELL | CAPMKTS | 70.38 | 4 | Block | Block |
| PAYTM | SELL | NEWAGE | 39.05 | 17 | Block | Block |
| COFORGE | SELL | IT | 46.31 | 12 | Block | Block |
| DRREDDY | BUY | PHA | 54.55 | 6 | Block | Block |
| JINDALSTEL | SELL | MET | 83.66 | 3 | Pass | Block |
| MOTHERSON | SELL | AUTO | 45.80 | 13 | Block | Block |
| DABUR | BUY | FMCG | 92.71 | 1 | Pass | Pass |
| DLF | SELL | REALTY | 65.82 | 5 | Block | Block |
| TVSMOTOR | BUY | AUTO | 21.44 | 20 | Block | Block |
| AXISBANK | BUY | BANK | 70.03 | 3 | Pass | Block |
| BAJAJFINSV | BUY | FIN | 83.05 | 2 | Pass | Pass |
| ALKEM | BUY | PHA | 58.99 | 7 | Block | Block |
| PIIND | BUY | CHEMICALS | 49.06 | 10 | Block | Block |
| SUNPHARMA | BUY | PHA | 58.99 | 7 | Block | Block |
| NHPC | BUY | ENE | 39.96 | 15 | Block | Block |
| RADICO | BUY | FMCG | 98.79 | 1 | Pass | Pass |
| TRENT | BUY | CONSUMPTION | 56.84 | 9 | Block | Block |
| KAYNES | SELL | CAPGOODS | 71.09 | 3 | Pass | Block |
| AMBER | BUY | CDUR | 1.46 | 30 | Block | Block |
| BRITANNIA | BUY | FMCG | 95.75 | 1 | Pass | Pass |
| ASTRAL | BUY | BUILDMAT | 50.14 | 14 | Block | Block |

## Interpretation

A lower candidate count is not proof of improved returns. The M6 six executed/paper trades use different entries and exits, so their P&L cannot be transferred to these M16 candidates. This single session cannot establish an optimal threshold. Validate common entry/stop/exit/cost rules on multiple sessions before enabling any gate.

Recommend shadow-recording sector rank, directional persistent score, full-sector eligibility and snapshot availability at both running breakout and close. Compare unfiltered M16 against persistence-only and full-sector variants without changing the live baseline first. Do not use whole-market tilt as an automatic veto: strong individual sectors can oppose the index.

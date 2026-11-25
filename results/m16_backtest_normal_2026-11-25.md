# Updated M16 results — 25 November 2026

**30 technical candidates: 20 BUY, 10 SELL. No top gainers/losers filter.**

Approved pre-signal rally context is enabled. Normal pullback/BOS must follow tracking; direct-original-BOS exception remains.

Saved-data replay of 209 symbols using recorded labels/EMAs, assuming close+1s availability. Not independent indicator recalculation, actual live delivery, intrabar reconstruction, or P&L/fill testing. Times below are candle CLOSE times in IST.

| Stock | Side | Close IST | BOS | Close price |
|---|---|---|---:|---:|
| AUROPHARMA | BUY | 09:55 | 1824.80 | 1826.30 |
| BIOCON | BUY | 09:55 | 362.50 | 363.00 |
| DIVISLAB | BUY | 09:55 | 11308.00 | 11315.00 |
| TATACONSUM | BUY | 09:55 | 1173.10 | 1174.90 |
| GMRAIRPORT | SELL | 10:00 | 102.22 | 102.10 |
| HINDUNILVR | BUY | 10:00 | 2051.20 | 2053.40 |
| ITC | BUY | 10:00 | 306.20 | 306.65 |
| BHARATFORG | SELL | 10:05 | 1712.60 | 1711.70 |
| DIXON | SELL | 10:05 | 13300.00 | 13260.00 |
| NAM-INDIA | SELL | 10:05 | 1021.40 | 1020.65 |
| PAYTM | SELL | 10:05 | 1722.00 | 1717.50 |
| COFORGE | SELL | 10:10 | 2102.00 | 2099.20 |
| DRREDDY | BUY | 10:15 | 1384.30 | 1389.00 |
| JINDALSTEL | SELL | 10:15 | 1122.10 | 1121.10 |
| MOTHERSON | SELL | 10:15 | 162.78 | 162.22 |
| DABUR | BUY | 10:20 | 413.30 | 414.45 |
| DLF | SELL | 10:25 | 784.10 | 783.85 |
| TVSMOTOR | BUY | 10:30 | 4571.60 | 4583.10 |
| AXISBANK | BUY | 10:40 | 1426.80 | 1433.90 |
| BAJAJFINSV | BUY | 10:40 | 1927.50 | 1930.40 |
| ALKEM | BUY | 10:50 | 5134.50 | 5139.50 |
| PIIND | BUY | 10:50 | 1585.00 | 1591.00 |
| SUNPHARMA | BUY | 10:50 | 1883.00 | 1886.00 |
| NHPC | BUY | 10:55 | 76.76 | 76.78 |
| RADICO | BUY | 10:55 | 4742.90 | 4744.00 |
| TRENT | BUY | 10:55 | 3315.80 | 3316.40 |
| KAYNES | SELL | 11:05 | 2735.00 | 2733.00 |
| AMBER | BUY | 11:30 | 6940.00 | 6965.00 |
| BRITANNIA | BUY | 11:30 | 4953.70 | 4958.60 |
| ASTRAL | BUY | 11:45 | 1245.70 | 1249.20 |

Compared with the earlier 26-candidate experiment, applying prior-rally initialization consistently to the direct-BOS search adds HINDUNILVR, NAM-INDIA, DRREDDY and AXISBANK.

Reproduction: `python m16_backtest.py --audit <saved-audit.txt> --output results/m16_backtest_normal_2026-11-25.json`

Validation: 91 M16 tests passed. M6 runner regression suite passed earlier in this branch session; no M6 code changes.

"""Exploratory historical replay. Not a forward validation or executable fill guarantee."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import trader
import costs
from m15_entry import candidate

root = Path(__file__).resolve().parents[1]
all_picks = {}
for fp in (root / 'data/history').glob('*.csv'):
    try:
        df = pd.read_csv(fp, parse_dates=['dt'])
        df['day'] = df.dt.dt.strftime('%Y-%m-%d')
        df = df[df.day >= '2026-09-15']
        for day, b in df.groupby('day'):
            b = b.reset_index(drop=True).copy()
            b['t'] = b.dt.dt.strftime('%H:%M')
            for j in range(7, len(b)):
                hit = candidate(b, j)
                if hit:
                    all_picks.setdefault(day, []).append((str(b.t.iloc[j]), -hit['score'], fp.stem, j, hit, b))
    except Exception as exc:
        print(fp.name, exc)

for period, days in [('early', sorted(all_picks)[:-12]), ('recent', sorted(all_picks)[-12:])]:
    nets, counts, daily_nets = [], [], []
    for day in days:
        seen = set(); daily = []
        for t, score, sym, j, hit, b in sorted(all_picks[day], key=lambda p: (p[0], p[1], p[2])):
            if sym in seen: continue
            seen.add(sym)
            tr = trader.evaluate(sym, hit['side'], t, float(b.close.iloc[j]),
                                 hit['signal'], b, warmup=None)
            if 'error' in tr: continue
            daily.append(costs.trade_costs(tr)['net'])
            if len(daily) == 3: break
        counts.append(len(daily)); daily_nets.append(sum(daily)); nets.extend(daily)
    wins = sum(n > 0 for n in nets)
    print(period, 'days', len(days), 'traded days', sum(c>0 for c in counts),
          'trades', len(nets), 'win%', round(100*wins/max(1,len(nets)),1),
          'net Rs', round(sum(nets)), 'positive days',sum(v>0 for v in daily_nets))

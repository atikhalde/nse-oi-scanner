"""Prospective observation metadata for paper trades (NOT broker execution fills)."""
from datetime import datetime
import pandas as pd
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')


def capture(tr, bars, now=None):
    """Stamp first observation; never mistake a historical signal close for a fill."""
    if 'audit' in tr:
        return tr
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        raise ValueError('observation must have a timezone')
    now = now.astimezone(IST)
    audit = {'observed_at': now.isoformat(), 'signal_time': tr.get('time'),
             'signal_close': tr.get('entry'), 'observation_price': None,
             'observation_bar': None, 'delay_minutes': None, 'price_type': 'last_completed_bar_close_PROXY'}
    try:
        rows = bars[bars['t'] == tr['time']]
        if not rows.empty:
            stamp = rows['dt'].iloc[0].to_pydatetime()
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=IST)
            audit['delay_minutes'] = round((now - stamp.astimezone(IST)).total_seconds()/60 - 5, 2)
        completed = bars[bars['dt'] + pd.Timedelta(minutes=5) <= now]
        if not completed.empty:
            row = completed.iloc[-1]
            audit['observation_bar'] = str(row['t'])
            audit['observation_price'] = float(row['close'])
    except (KeyError, ValueError, TypeError, AttributeError):
        pass
    tr['audit'] = audit
    return tr


def preserve(old, new):
    """Revaluation must not rewrite immutable first-observation evidence."""
    if old.get('audit'):
        new['audit'] = old['audit']
    return new


def is_final(tr):
    return bool(tr.get('closed')) and not any(str(leg[0]).startswith('OPEN') for leg in tr.get('legs', []))

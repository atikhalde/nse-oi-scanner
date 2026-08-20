"""Fail-fast intraday feed for M12/M13.

One Dhan failure opens a cycle-local circuit breaker. Remaining symbols use one
immediate Yahoo request—no retry loop and no deliberate sleep.
"""
from __future__ import annotations
import os
from urllib.parse import quote
import pandas as pd
import requests
import feeds

IST='Asia/Kolkata'
UA=feeds.UA

class FastFeedCycle:
    def __init__(self,dhan_timeout=(2,8),yahoo_timeout=(2,10)):
        self.dhan_enabled=bool(os.environ.get('DHAN_TOKEN'))
        self.dhan_timeout=dhan_timeout;self.yahoo_timeout=yahoo_timeout
        self.dhan_calls=0;self.yahoo_calls=0;self.trip_reason=None
    def _trip(self,reason):
        self.dhan_enabled=False
        if self.trip_reason is None:
            self.trip_reason=reason
            print(f'FAST-FEED: Dhan disabled for this cycle ({reason}); switching immediately to Yahoo')
    def _dhan(self,security_id,frm,to):
        self.dhan_calls+=1
        p={'securityId':str(security_id),'exchangeSegment':'NSE_EQ','instrument':'EQUITY','interval':'5','oi':False,'fromDate':frm,'toDate':to}
        r=requests.post('https://api.dhan.co/v2/charts/intraday',json=p,headers={'Content-Type':'application/json','access-token':os.environ['DHAN_TOKEN']},timeout=self.dhan_timeout)
        if r.status_code==429:
            raise requests.HTTPError('HTTP 429 rate limited',response=r)
        r.raise_for_status();j=r.json();ts=j.get('timestamp') or []
        if not ts:return None
        return pd.DataFrame({'dt':pd.to_datetime(ts,unit='s',utc=True).tz_convert(IST),'open':j['open'],'high':j['high'],'low':j['low'],'close':j['close'],'volume':j['volume']}).dropna()
    def _yahoo(self,symbol):
        self.yahoo_calls+=1
        ticker=quote(f'{symbol}.NS',safe='')
        u=f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=5m&range=1d'
        r=requests.get(u,headers=UA,timeout=self.yahoo_timeout);r.raise_for_status();j=r.json()['chart']['result'][0];ts=j.get('timestamp') or []
        if not ts:return None
        q=j['indicators']['quote'][0]
        return pd.DataFrame({'dt':pd.to_datetime(ts,unit='s',utc=True).tz_convert(IST),'open':q['open'],'high':q['high'],'low':q['low'],'close':q['close'],'volume':q.get('volume')}).dropna()
    def fetch(self,symbol,security_id,now_ist):
        frm=now_ist.strftime('%Y-%m-%d 09:15:00');to=now_ist.strftime('%Y-%m-%d %H:%M:%S')
        if self.dhan_enabled:
            try:
                d=self._dhan(security_id,frm,to)
                if d is not None and not d.empty:return d,'dhan-fast'
                self._trip('empty Dhan response')
            except Exception as exc:
                status=getattr(getattr(exc,'response',None),'status_code',None)
                self._trip(f'HTTP {status}' if status else type(exc).__name__)
        try:
            d=self._yahoo(symbol)
            return d,'yahoo-fast' if d is not None and not d.empty else 'none'
        except Exception as exc:
            print(f'FAST-FEED Yahoo {symbol}: {type(exc).__name__}: {exc}')
        # Final fallback: load latest available session from local history file
        # so the paper pipeline can continue even when external feeds fail.
        try:
            fp = feeds.L.HIST / f'{symbol}.csv' if hasattr(feeds, 'L') else None
            # Try to find history via common paths used by runners
            import live_runner as L
            fp = L.HIST / f'{symbol}.csv'
            if fp.exists():
                h = pd.read_csv(fp, usecols=['dt','open','high','low','close','volume'])
                h['dt'] = pd.to_datetime(h['dt'], utc=True).dt.tz_convert(IST)
                h['day'] = h['dt'].dt.strftime('%Y-%m-%d')
                today_str = now_ist.strftime('%Y-%m-%d')
                available = h[h['day'] < today_str]
                if not available.empty:
                    latest_day = available['day'].iloc[-1]
                    latest = available[available['day'] == latest_day].copy()
                    if not latest.empty:
                        latest = latest.sort_values('dt').reset_index(drop=True)
                        latest['t'] = latest['dt'].dt.strftime('%H:%M')
                        print(f'FAST-FEED {symbol}: using history fallback for {latest_day} ({len(latest)} bars)')
                        return latest, 'history-fallback'
        except Exception as exc:
            print(f'FAST-FEED history fallback {symbol}: {exc}')
        return None,'none'

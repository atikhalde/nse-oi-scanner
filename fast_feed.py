"""Fail-fast intraday feed (M12/M13/M14 test surface).

Same ladder as every other runner now: Yahoo is primary, Dhan is only a
secondary fallback. M12/M13/M14 fetch bars through feeds.fetch_today(); this
cycle wrapper exists to pin the fail-fast contract — one immediate Yahoo
request per symbol, no retry loop and no deliberate sleep, Dhan fallback only
when Yahoo comes back empty or fails.
"""
from __future__ import annotations
import os
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
        if self.trip_reason is None:
            self.trip_reason=reason
            print(f'FAST-FEED: Yahoo missed for a symbol ({reason}); using Dhan fallback for the rest of this cycle when needed')
    def _dhan(self,security_id,frm,to):
        self.dhan_calls+=1
        p={'securityId':str(security_id),'exchangeSegment':'NSE_EQ','instrument':'EQUITY','interval':'5','oi':False,'fromDate':frm,'toDate':to}
        r=requests.post('https://api.dhan.co/v2/charts/intraday',json=p,headers={'Content-Type':'application/json','access-token':os.environ['DHAN_TOKEN']},timeout=self.dhan_timeout)
        r.raise_for_status();j=r.json();ts=j.get('timestamp') or []
        if not ts:return None
        return pd.DataFrame({'dt':pd.to_datetime(ts,unit='s',utc=True).tz_convert(IST),'open':j['open'],'high':j['high'],'low':j['low'],'close':j['close'],'volume':j['volume']}).dropna()
    def _yahoo(self,symbol):
        self.yahoo_calls+=1
        return feeds.fetch_bars_yahoo(symbol,'1d')
    def fetch(self,symbol,security_id,now_ist):
        frm=now_ist.strftime('%Y-%m-%d 09:15:00');to=now_ist.strftime('%Y-%m-%d %H:%M:%S')
        # Yahoo is primary, exactly like every other runner.
        try:
            d=self._yahoo(symbol)
        except Exception as exc:
            d=None
            self._trip(type(exc).__name__)
        if d is not None and not d.empty:
            return d,'yahoo-fast'
        self._trip('empty Yahoo response')
        # Dhan is only the fallback when Yahoo misses.
        if self.dhan_enabled:
            try:
                d=self._dhan(security_id,frm,to)
                if d is not None and not d.empty:return d,'dhan-fast'
            except Exception as exc:
                status=getattr(getattr(exc,'response',None),'status_code',None)
                print(f'FAST-FEED Dhan fallback {symbol}: HTTP {status}' if status else f'FAST-FEED Dhan fallback {symbol}: {type(exc).__name__}: {exc}')
        return None,'none'

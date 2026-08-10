#!/usr/bin/env python3
import pandas as pd
import m13_trader as T

def ok(n,c):assert c,n;print('PASS',n)
def bars(rows,start='09:45'):
 t=pd.date_range(f'2026-08-10 {start}',periods=len(rows),freq='5min',tz='Asia/Kolkata');d=pd.DataFrame(rows,columns=['open','high','low','close']);d['dt']=t;d['t']=t.strftime('%H:%M');d['volume']=10000;return d
def warm():return pd.DataFrame({'open':[98+i*.01 for i in range(100)],'high':[98.2+i*.01 for i in range(100)],'low':[97.8+i*.01 for i in range(100)],'close':[98.1+i*.01 for i in range(100)]})
def main():
 # target, runner lock, then protected exit
 b=bars([(99.7,100.2,99.0,100.0),(100.0,101.1,99.8,100.9),(100.9,102.0,100.7,101.8),(101.8,102.2,101.5,102.0),(101.9,102.0,100.8,101.0)])
 tr=T.evaluate('X','BUY','09:45',100,'BUY-EX17',b,warmup=warm())
 ok('signal-candle BUY stop with buffer',abs(tr['sl']-99*.9998)<.02)
 ok('risk and notional sizing both enforced',tr['risk_rs']<=T.RISK_CAP and tr['capital']<=T.NOTIONAL_CAP)
 ok('60% scalp book occurs',any(x[0].startswith('BOOK075') for x in tr['legs']))
 ok('runner locks after expansion',tr['runner_locked'])
 ok('full trade closes and stays profitable',tr['closed'] and tr['pnl']>0)
 ok('deterministic replay',tr==T.evaluate('X','BUY','09:45',100,'BUY-EX17',b,warmup=warm()))
 br={t:.60 for t in b.t};br[str(b.t.iloc[2])]=.40
 tb=T.evaluate('X','BUY','09:45',100,'BUY-EX17',b,warmup=warm(),breadth_by_time=br)
 ok('runner exits on opposite breadth flip',any(e['key']=='EXIT_BREADTH' for e in tb['events']))
 # hard stop first
 s=bars([(99.7,100.2,99.0,100.0),(100.0,100.1,98.5,98.8)])
 ts=T.evaluate('X','BUY','09:45',100,'BUY-EX17',s,warmup=warm());ok('hard SL exits loss',ts['closed'] and ts['pnl']<0 and any(e['key']=='EXIT_SL' for e in ts['events']))
 # no new extreme for two bars -> failure exit
 f=bars([(99.7,100.2,99.0,100.0),(100.0,100.1,99.6,99.9),(99.9,100.15,99.5,99.8)])
 tf=T.evaluate('X','BUY','09:45',100,'BUY-EX17',f,warmup=warm());ok('no-extreme momentum failure exits early',tf['closed'] and any(e['key']=='EXIT_FAILURE' for e in tf['events']))
 # SELL mirror target
 q=bars([(100.3,101.0,99.8,100.0),(100.0,100.2,98.8,99.0),(99.0,99.2,97.9,98.1),(98.1,99.5,98.0,99.3)])
 tq=T.evaluate('Y','SELL','09:45',100,'SELL-EX1',q,warmup=warm());ok('SELL mirror books scalp and valid stop',tq['sl']>100 and tq['booked'])
 print('ALL M13 TRADER TESTS PASSED')
if __name__=='__main__':main()

#!/usr/bin/env python3
import json,tempfile,os
from pathlib import Path
import pandas as pd
import m13_runner as R

def ok(n,c):assert c,n;print('PASS',n)
def mkbars():
 t=pd.date_range('2026-08-10 09:45',periods=16,freq='5min',tz='Asia/Kolkata');d=pd.DataFrame({'dt':t,'open':[100]*16,'high':[100.2,101.1,102]+[102]*13,'low':[99,99.8,100.7]+[100.8]*13,'close':[100,100.9,101.8]+[101.5]*13,'volume':[10000]*16});d['t']=t.strftime('%H:%M');return d
def cand(sym,sector,time,score,minute):return {'decision_id':f'{sym}|BUY|{time}|102','symbol':sym,'side':'BUY','signal':'BUY-EX17','code':102,'time':time,'entry':100.0,'sector':sector,'bar_dt':f'2026-08-10T{time}:00+05:30','signal_age_min':0.5,'score':score,'accepted':True,'model_reason':'qualified','subtype':'MOMENTUM-CONTROLLED','features':{'s1':1}}
def main():
 old_state,old_send,old_warm=R.STATE,R.Alerts.send_message,R.warmup
 try:
  with tempfile.TemporaryDirectory() as td:
   R.STATE=Path(td)/'state13.json';R.Alerts.send_message=lambda text:1;R.warmup=lambda sym,today:None
   st=R.load_state('2026-08-10');bars={s:mkbars() for s in ['A','B','C','D','E']}
   cs=[cand('A','IT','09:45',90,585),cand('B','AUTO','09:45',80,585),cand('C','PHA','09:50',88,590),cand('D','BANK','09:55',87,595),cand('E','MET','10:00',86,600)]
   R.dispatch(st,'2026-08-10',cs,bars,{s:99.0 for s in bars})
   ok('same-bar ranking enters only highest score', 'A' in st['trades'] and 'B' not in st['trades'])
   ok('hard daily cap limits M13 to three',R.trade_count(st)==3)
   ok('entry alert keys are unique',len(st['alerts'])==len(set(st['alerts']))==3)
   saved=json.loads(R.STATE.read_text());ok('state including decisions is JSON durable',len(saved['trades'])==3 and len(saved['decisions'])==5)
   before=list(st['alerts']);ok('duplicate reservation cannot resend',not R.reserve(st,before[0]) and st['alerts']==before)
 finally:
  R.STATE,R.Alerts.send_message,R.warmup=old_state,old_send,old_warm
 # Regression: cursor reset and EOD seed guard (diagnosis §7.8 / §P0-4.5)
 ok('M13 pointer ahead of a shortened feed clamps to newest bar',R.clamp_cursor(100,5)==4)
 ok('M13 pointer ahead of empty feed clamps to zero',R.clamp_cursor(100,0)==0)
 ok('M13 normal pointer is untouched',R.clamp_cursor(3,5)==3)
 old_cache=R.PREV_CACHE
 os.environ['SEED_PREV_DAILY_TOPUP']='0'   # keep this test offline; top-up covered in test_seed_prev_context
 try:
  with tempfile.TemporaryDirectory() as td:
   R.PREV_CACHE=Path(td)/'m13_prev_context.json'
   def bars(first_t):
    t=pd.date_range('2026-08-28 09:15',periods=3,freq='5min',tz='Asia/Kolkata')
    d=pd.DataFrame({'dt':t,'open':[100.]*3,'high':[101.]*3,'low':[99.]*3,'close':[100.5]*3,'volume':[1000]*3})
    d['t']=d['dt'].dt.strftime('%H:%M');d.loc[d.index[-1],'t']=first_t;return d
   full={f'S{i}':bars('15:20') for i in range(180)};full['LATE']=bars('15:15')
   j=R.seed_prev('2026-08-28',full)
   ok('M13 full 15:20+ seed writes cache',j['status']=='OK' and R.PREV_CACHE.exists())
   saved=json.loads(R.PREV_CACHE.read_text());ok('M13 pre-15:20 final bar not recorded', 'LATE' not in (saved.get('close') or {}))
   R.PREV_CACHE.write_text(json.dumps({'date':'2026-08-27','close':{'KEEP':1.0}}))
   j=R.seed_prev('2026-08-28',{f'P{i}':bars('15:20') for i in range(3)})
   ok('M13 partial (<180) seed is refused',j['status']=='INSUFFICIENT')
   ok('M13 partial seed never overwrites an existing cache','KEEP' in (json.loads(R.PREV_CACHE.read_text()).get('close') or {}))
 finally:
  R.PREV_CACHE=old_cache
  os.environ.pop('SEED_PREV_DAILY_TOPUP',None)
 print('ALL M13 RUNNER TESTS PASSED')
if __name__=='__main__':main()

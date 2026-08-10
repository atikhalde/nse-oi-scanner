#!/usr/bin/env python3
import json,tempfile
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
 print('ALL M13 RUNNER TESTS PASSED')
if __name__=='__main__':main()

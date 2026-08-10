#!/usr/bin/env python3
import pandas as pd
import m13_entry as E

def ok(n,c):assert c,n;print('PASS',n)
def good():return {'dir_prev_pct':1.5,'dir_gap_pct':.2,'ema9_20_atr':.8,'ema20_50_atr':.5,'close_ema20_atr':1.5,'close_vwap_atr':1.2,'candle_clv':.95,'body_atr':1.0,'range_atr':1.4,'relvol20':2.0,'clock_relvol':4.0,'master_total_score':95,'opening_breadth_dir':.65,'live_breadth_dir':.60,'sector_breadth_dir':.75,'prior_vix_return':0.0,'video_setups':'S1+S3','video_setup_count':2,'s1':1,'s2':0,'s3':1,'s4':0}
def main():
 f=good();d=E.decide(102,'BUY-EX17','BUY','10:05',f);ok('A+ BUY-EX17 qualifies',d.accepted and d.score>=70)
 ok('SELL-EX1 qualifies',E.decide(201,'SELL-EX1','SELL','10:05',f).accepted)
 ok('BUY-EX base qualifies',E.decide(101,'BUY-EX','BUY','10:05',f).accepted)
 ok('preview code blocked',not E.decide(90,'ENTRY BUY','BUY','10:05',f).accepted)
 ok('S1 mandatory',not E.decide(102,'BUY-EX17','BUY','10:05',dict(f,s1=0)).accepted)
 ok('opening breadth hard gate',not E.decide(102,'BUY-EX17','BUY','10:05',dict(f,opening_breadth_dir=.549)).accepted)
 ok('prior VIX crush blocked',not E.decide(102,'BUY-EX17','BUY','10:05',dict(f,prior_vix_return=-2.0)).accepted)
 ok('opposite live breadth blocked',not E.decide(102,'BUY-EX17','BUY','10:05',dict(f,live_breadth_dir=.44)).accepted)
 rev=dict(f,dir_prev_pct=0.0,close_ema20_atr=1.0,close_vwap_atr=1.0)
 dr=E.decide(102,'BUY-EX17','BUY','10:05',rev);ok('reversion classification ranks but does not veto',dr.accepted and dr.subtype=='REVERSION-ALIGNMENT')
 ext=dict(f,dir_prev_pct=4.0,close_ema20_atr=3.0);de=E.decide(102,'BUY-EX17','BUY','10:05',ext);ok('extended momentum ranks but does not veto',de.accepted and de.subtype=='MOMENTUM-EXTENDED')
 low=dict(f,clock_relvol=1.0,master_total_score=50,dir_prev_pct=.1,sector_breadth_dir=.45,candle_clv=.5,body_atr=0,s3=0,video_setups='S1',video_setup_count=1)
 ok('absolute A+ score threshold prevents weak-day quota fill',not E.decide(102,'BUY-EX17','BUY','10:05',low).accepted)
 ok('hard trade cap is three',E.MAX_TRADES_PER_DAY==3)
 print('ALL M13 ENTRY TESTS PASSED')
if __name__=='__main__':main()

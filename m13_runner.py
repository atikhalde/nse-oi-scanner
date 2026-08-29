#!/usr/bin/env python3
"""Standalone M13 Equity Momentum A+ paper runner (1-3 trades/day)."""
from __future__ import annotations
import argparse,datetime as dt,json,time
from pathlib import Path
import pandas as pd
import live_runner as L
import feeds,fast_feed,flow_map as FM,learn_log,m11_runner as V,m12_entry as Context
import m13_alerts as Alerts,m13_entry as E,m13_trader as T
import costs,report

ROOT=L.ROOT;STATE=ROOT/'state13.json';PREV_CACHE=ROOT/'data'/'m13_prev_context.json';SECTOR_OF=dict(pd.read_csv(ROOT/'fno_sector_map.csv').values)

def save_state(st):STATE.write_text(json.dumps(st,indent=1))
def load_state(today):
 if STATE.exists():
  try:
   st=json.loads(STATE.read_text())
   if st.get('date')==today:return st
  except Exception:pass
 return {'date':today,'signals':{},'trades':{},'alerts':[],'decisions':[],'eod_done':False,'cycles':0,'prev_meta':{},'vix':{}}
def parse_day(s):
 try:return dt.date.fromisoformat(str(s))
 except:return None
def expected_prev(td):
 if not td:return None
 q=td-dt.timedelta(days=1)
 while q.weekday()>=5:q-=dt.timedelta(days=1)
 return q
def finite(v):
 try:return bool(pd.notna(float(v)))
 except:return False

def load_prev(today):
 td=parse_day(today);exp=expected_prev(td)
 if PREV_CACHE.exists():
  try:
   j=json.loads(PREV_CACHE.read_text());vals={k:float(v) for k,v in (j.get('close') or {}).items() if finite(v)}
   if parse_day(j.get('date'))==exp and len(vals)>=180:return vals,{k:float(v) for k,v in (j.get('pivot') or {}).items() if finite(v)},{'status':'OK','source':PREV_CACHE.name,'date':str(exp),'count':len(vals)}
  except:pass
 vals={};piv={}
 for sym in L.SYMS:
  try:
   h=pd.read_csv(L.HIST/f'{sym}.csv',usecols=['dt','high','low','close']);h['day']=h.dt.astype(str).str[:10];q=h[h.day==str(exp)]
   if len(q):
    cl=float(q.close.iloc[-1]);vals[sym]=cl;piv[sym]=(float(q.high.max())+float(q.low.min())+cl)/3
  except:continue
 if len(vals)>=180:return vals,piv,{'status':'OK','source':'data/history bootstrap','date':str(exp),'count':len(vals)}
 # Fallback: use latest available previous session if exact previous weekday missing
 latest_vals={};latest_piv={};latest_day=None;latest_days_all=[]
 for sym in L.SYMS:
  try:
   h=pd.read_csv(L.HIST/f'{sym}.csv',usecols=['dt','high','low','close']);h['day']=h.dt.astype(str).str[:10];q_all=h[h.day<str(today)]
   if len(q_all):
    d=str(q_all['day'].iloc[-1]);cl=float(q_all['close'].iloc[-1])
    latest_vals[sym]=cl;latest_piv[sym]=(float(q_all['high'].max())+float(q_all['low'].min())+cl)/3;latest_days_all.append(d)
  except:continue
 common_fallback=pd.Series(latest_days_all).mode().iloc[0] if latest_days_all else None
 latest_vals_filt={s:cl for s,cl in latest_vals.items() if s not in [k for k in latest_vals] or True}
 # Only use fallback if we have a common latest day with enough stocks
 if common_fallback:
  latest_vals_filt={k:v for k,v in latest_vals.items() if True}  # keep all from latest common
  # Re-filter properly: only stocks whose latest day matches common_fallback
  latest_vals_filt={}
  latest_piv_filt={}
  for sym in L.SYMS:
   try:
    h=pd.read_csv(L.HIST/f'{sym}.csv',usecols=['dt','high','low','close']);h['day']=h.dt.astype(str).str[:10];q_all=h[h.day<str(today)];q_filt=q_all[q_all['day']==str(common_fallback)]
    if len(q_filt):
     cl=float(q_filt['close'].iloc[-1]);latest_vals_filt[sym]=cl;latest_piv_filt[sym]=(float(q_filt['high'].max())+float(q_filt['low'].min())+cl)/3
   except:continue
  if parse_day(common_fallback) == exp and len(latest_vals_filt) >= 180:
   return latest_vals_filt,latest_piv_filt,{'status':'OK','source':'data/history bootstrap','date':str(common_fallback),'count':len(latest_vals_filt),'expected_previous_weekday':str(exp)}
 return {},{}, {'status':'STALE','source':'m13 cache/history','expected':str(exp),'count':len(vals),'policy':'no entry; seed at EOD'}
def seed_prev(today,bars_map):
 vals={};piv={};last=[]
 for sym,b in bars_map.items():
  if b is None or b.empty:continue
  q=b.sort_values('dt');late=q[q.t>='15:20']
  if late.empty:continue
  cl=float(late.close.iloc[-1]);vals[sym]=cl;piv[sym]=(float(q.high.max())+float(q.low.min())+cl)/3;last.append(str(late.t.iloc[-1]))
 j={'date':today,'close':vals,'pivot':piv,'count':len(vals),'last_bar_min':min(last) if last else None,'generated_utc':dt.datetime.now(dt.timezone.utc).isoformat()};PREV_CACHE.parent.mkdir(exist_ok=True);PREV_CACHE.write_text(json.dumps(j,indent=1));return j

def prior_vix_return(today,st):
 v=st.setdefault('vix',{})
 if v.get('date')==today and finite(v.get('prior_return')):return float(v['prior_return'])
 d=FM.fetch_index_bars(FM.VIX_TICKER,'5d')
 if d is None or d.empty:return None
 d=d.copy();d['day']=d.dt.dt.strftime('%Y-%m-%d');daily=d[d.day<today].groupby('day').close.last().sort_index()
 if len(daily)<2:return None
 r=(float(daily.iloc[-1])/float(daily.iloc[-2])-1)*100;v.update(date=today,prior_return=round(r,4),last_day=str(daily.index[-1]));save_state(st);return r

def reserve(st,key):return Alerts.reserve_once(st,key,save_state)
def trade_count(st):return sum(1 for t in st['trades'].values() if isinstance(t,dict) and 'symbol' in t)
def open_count(st):return sum(1 for t in st['trades'].values() if isinstance(t,dict) and 'symbol' in t and not t.get('closed'))
def taken_symbols(st):return {t.get('symbol') for t in st['trades'].values() if isinstance(t,dict) and t.get('symbol')}
def taken_sectors(st):return {t.get('m13_sector') for t in st['trades'].values() if isinstance(t,dict) and t.get('m13_sector')}
def side_count(st,side):return sum(1 for t in st['trades'].values() if isinstance(t,dict) and t.get('side')==side)
def full_losses(st):return sum(1 for t in st['trades'].values() if isinstance(t,dict) and t.get('closed') and float(t.get('r_total',0))<=-.8)
def next_key(st,sym):
 if sym not in st['trades']:return sym
 n=2
 while f'{sym}#{n}' in st['trades']:n+=1
 return f'{sym}#{n}'
def add_decision(st,c,taken,why,trade_key=None):
 q=dict(c);q.pop('ts',None);q.update(taken=int(taken),why=why)
 if trade_key:q['trade_key']=trade_key
 st['decisions'].append(q)

def manage(st,today,bars_map,prev):
 for tkey in list(st['trades']):
  old=st['trades'][tkey];sym=old.get('symbol');b=bars_map.get(sym)
  if not sym or b is None:continue
  try:
   new=T.evaluate(sym,old['side'],old['time'],float(old['entry']),old['signal'],b,warmup=warmup(sym,today),features=old.get('m13_features'),breadth_by_time=breadth_series(old['side'],bars_map,prev,b))
   if 'error' in new:
    print(f"M13 manage {tkey}: {new['error']}");continue
   for k in ('m13_score','m13_features','m13_sector','m13_code','m13_subtype','decision_id'):new[k]=old.get(k)
   st['trades'][tkey]=new
   for ev in new.get('events',[]):
    key=f"{tkey}:{ev['key']}"
    if ev['key']!='ENTRY' and reserve(st,key):Alerts.send_message(T.fmt_alert(new,ev['key']))
  except Exception as exc:print(f'M13 manage {tkey}: {type(exc).__name__}: {exc}')

def warmup(sym,today):return __import__('trader').load_warmup(L.HIST/f'{sym}.csv',today)
def breadth_series(side,bars_map,prev,candidate_bars):
 out={}
 for ts,t in zip(candidate_bars.dt,candidate_bars.t):
  up=total=0;stamp=pd.Timestamp(ts)
  for sym,b in bars_map.items():
   pc=prev.get(sym)
   if b is None or b.empty or not finite(pc):continue
   q=b[pd.to_datetime(b.dt)<=stamp]
   if q.empty:continue
   up+=int(float(q.close.iloc[-1])>=float(pc));total+=1
  if total>=180:
   bull=up/total;out[str(t)]=bull if side=='BUY' else 1-bull
 return out

def collect(st,today,now,bars_map,prev,piv,vixret):
 fresh=[];params=L.ms.Params(enable_buy_ex10=False,enable_buy_ex11=False);known_ids={str(x.get('decision_id')) for x in st.get('decisions',[])}
 for sym,tbars in bars_map.items():
  n=len(tbars);cursor=st['signals'].get(sym,{})
  if 'nbars' in cursor:known=int(cursor.get('nbars',0))
  else:
   # Skip historical intraday bars on the first valid post-bootstrap run. M13
   # never backfills old entries; processing thousands of stale prefixes only
   # causes timeout and queue growth.
   closed=[k for k,x in enumerate(tbars.dt) if pd.Timestamp(x)+pd.Timedelta(minutes=5)<=pd.Timestamp(now)]
   known=closed[-1] if closed else 0
  known=0 if known>n else known
  for j in range(known,n):
   tk=pd.Timestamp(tbars.dt.iloc[j]);tk=tk.tz_localize(now.tz) if tk.tzinfo is None else tk
   if tk+pd.Timedelta(minutes=5)>pd.Timestamp(now):break
   try:
    prefix=tbars.iloc[:j+1];frame=L.engine_frame(L.HIST/f'{sym}.csv',prefix,today);er=L.ms.run_symbol(frame,params).iloc[-1];st['signals'].setdefault(sym,{})['nbars']=j+1
   except Exception as exc:print(f'M13 engine {sym}: {exc}');break
   code=er.get('scan_code')
   if pd.isna(code) or int(code) not in L.MASTER_CODES:continue
   code=int(code);side='BUY' if code<200 else 'SELL';etime=tk.strftime('%H:%M');signal=str(er.get('scan_name',code));did=f'{sym}|{side}|{etime}|{code}'
   if did in known_ids:continue
   detected=pd.Timestamp(L.now_ist());age=max(0,(detected-(tk+pd.Timedelta(minutes=5))).total_seconds()/60);sector=str(SECTOR_OF.get(sym,'UNMAPPED'))
   base={'decision_id':did,'symbol':sym,'side':side,'signal':signal,'code':code,'time':etime,'entry':round(float(tbars.close.iloc[j]),4),'sector':sector,'bar_dt':tk.isoformat(),'signal_age_min':round(age,3)}
   try:
    ctx=Context.market_sector_context_at(bars_map,prev,SECTOR_OF,sym,sector,side,tk);ob,ncov=E.opening_breadth(bars_map,prev,side);setups=V.video_setups(prefix,j,side,piv.get(sym)) if etime>=E.ENTRY_START else []
    feats=E.causal_features(frame,prefix,side,prev.get(sym),er.get('totalScore',0),ob,ctx.get('market_breadth_dir'),ctx.get('sector_breadth_prev_dir'),vixret,setups);feats.update(ctx);dec=E.decide(code,signal,side,etime,feats);accepted=dec.accepted;reason=dec.reason
    if accepted and age>E.MAX_SIGNAL_AGE_MIN:accepted=False;reason=f'stale signal {age:.2f}m > {E.MAX_SIGNAL_AGE_MIN:.1f}m'
    base.update(score=dec.score,accepted=accepted,model_reason=reason,subtype=dec.subtype,features=feats,opening_breadth_coverage=ncov)
   except Exception as exc:base.update(score=0,accepted=False,model_reason=f'feature failure: {type(exc).__name__}: {exc}',subtype='UNKNOWN',features={})
   fresh.append(base);known_ids.add(did)
 return fresh

def dispatch(st,today,candidates,bars_map,prev):
 if not candidates:return
 f=pd.DataFrame(candidates);f['ts']=pd.to_datetime(f.bar_dt,utc=True)
 for _ts,g in f.sort_values(['ts','score','symbol'],ascending=[True,False,True]).groupby('ts',sort=True):
  entered_this_bar=False
  for c in g.sort_values(['score','symbol'],ascending=[False,True]).to_dict('records'):
   if not c['accepted']:add_decision(st,c,False,c['model_reason']);continue
   why=None
   if entered_this_bar:why='only one new M13 entry per five-minute bar'
   elif trade_count(st)>=E.MAX_TRADES_PER_DAY:why='hard 3-trade daily cap'
   elif open_count(st)>=E.MAX_CONCURRENT:why='maximum 2 concurrent positions'
   elif full_losses(st)>=2:why='two full-risk daily losses reached'
   elif c['symbol'] in taken_symbols(st):why='one trade per symbol/day'
   elif E.ONE_TRADE_PER_SECTOR_DAY and c['sector'] in taken_sectors(st):why='one trade per sector/day'
   elif side_count(st,c['side'])>=E.MAX_TRADES_PER_SIDE:why='side cap reached'
   if why:add_decision(st,c,False,why);continue
   try:
    cbars=bars_map[c['symbol']]
    tr=T.evaluate(c['symbol'],c['side'],c['time'],float(c['entry']),c['signal'],cbars,warmup=warmup(c['symbol'],today),features=c['features'],breadth_by_time=breadth_series(c['side'],bars_map,prev,cbars))
    if 'error' in tr:add_decision(st,c,False,f"trader rejected: {tr['error']}");continue
    tr.update(m13_score=float(c['score']),m13_features=c['features'],m13_sector=c['sector'],m13_code=int(c['code']),m13_subtype=c['subtype'],decision_id=c['decision_id']);tkey=next_key(st,c['symbol']);st['trades'][tkey]=tr;add_decision(st,c,True,'selected',tkey)
    if reserve(st,f'{tkey}:ENTRY'):Alerts.send_message(T.fmt_alert(tr,'ENTRY'))
    entered_this_bar=True;print(f">>> M13 ENTRY {tkey} {c['side']} {c['signal']} score={c['score']:.1f} {c['subtype']}")
   except Exception as exc:add_decision(st,c,False,f'entry failure: {type(exc).__name__}: {exc}')
 save_state(st)

def write_lab(st,today,bars_map,prev):
 rows=[]
 for d in st.get('decisions',[]):
  r=dict(d);feat=r.pop('features',{}) if isinstance(r.get('features'),dict) else {}
  r.update({f'f_{k}':v for k,v in feat.items()});b=bars_map.get(d.get('symbol'))
  if b is not None:
   try:
    tr=T.evaluate(d['symbol'],d['side'],d['time'],float(d['entry']),d['signal'],b,warmup=warmup(d['symbol'],today),features=feat,breadth_by_time=breadth_series(d['side'],bars_map,prev,b))
    if 'error' not in tr:
     fee=costs.trade_costs(tr);r.update(cf_net=fee['net'],cf_gross=fee['gross'],cf_drag=fee['drag'],cf_r=tr['r_total'],cf_exit=tr['exit_text'],cf_win=int(fee['net']>0))
   except Exception as exc:r['label_error']=str(exc)
  rows.append(r)
 out=ROOT/'learn'/f'm13_candidates_{today}.csv';out.parent.mkdir(exist_ok=True);pd.DataFrame(rows).to_csv(out,index=False);return out

def eod_ready(st,hhmm):return hhmm>='15:25'
def finish_eod(st,today,now,bars_map,prev):
 done=[t for t in st['trades'].values() if isinstance(t,dict) and 'symbol' in t];sk={}
 for d in st.get('decisions',[]):
  if d.get('taken'):continue
  sk.setdefault(d.get('why','not selected'),[]).append([d.get('symbol'),d.get('side'),d.get('signal'),d.get('time'),d.get('entry')])
 meta={'status':E.MODEL_NAME,'source':f"S1 + opening breadth>=55% + prior VIX>{E.PRIOR_VIX_RETURN_MIN}% + A+ score",'daily_cap':E.MAX_TRADES_PER_DAY,'prev':st.get('prev_meta'),'vix':st.get('vix')};lbl=now.strftime('%d-%b-%Y')+' (M13 Equity Momentum A+)'
 out=report.build(done,lbl,meta,str(ROOT/f'paper_test_M13_{today}.xlsx'),skipped=sk or None,rules_note='M13 equity momentum scalp · max 3/day · signal-candle SL ±0.02% · 60%@+0.75R · 40% EMA9/two-bar runner · 60m max · full equity costs/slippage')
 lab=write_lab(st,today,bars_map,prev);learn_log.harvest('M13',today,st,None,bars_map);cache=seed_prev(today,bars_map);st['eod_done']=True;st['eod_cache_count']=cache['count'];keys=[f'M13:{today}:EOD_SUMMARY',f'M13:{today}:EOD_DOCUMENT'];owned=Alerts.reserve_batch(st,keys,save_state);save_state(st)
 if keys[0] in owned:Alerts.send_message('🅼13 EOD · '+report.summary_text(done,lbl,meta)+f"\nCandidates {len(st.get('decisions',[]))} · trades {len(done)}/3")
 if keys[1] in owned:Alerts.send_document(out,caption=f'🅼13 📄 equity momentum report {today}')
 print(f'M13 EOD report={out} lab={lab}')

def mode_live():
 now=L.now_ist();today=now.strftime('%Y-%m-%d');hhmm=now.strftime('%H:%M');st=load_state(today)
 if st.get('eod_done'):print('M13 EOD done — idle');return False
 if hhmm<'09:16':save_state(st);print('M13 pre-market — idle');return False
 prev,piv,meta=load_prev(today);st['prev_meta']=meta;vix=prior_vix_return(today,st);bars_map={};feed_cycle=fast_feed.FastFeedCycle()
 for sym in L.SYMS:
  try:
   b,_=feed_cycle.fetch(sym,L.SID[sym],now)
   if b is not None and not b.empty:b=b.sort_values('dt').drop_duplicates('dt').reset_index(drop=True);b['t']=b.dt.dt.strftime('%H:%M');bars_map[sym]=b
  except Exception as exc:print(f'M13 feed {sym}: {exc}')
 st['feed']={'dhan_calls':feed_cycle.dhan_calls,'yahoo_calls':feed_cycle.yahoo_calls,'fallback':feed_cycle.trip_reason,'fed':len(bars_map)}
 manage(st,today,bars_map,prev)
 if meta.get('status')=='OK' and finite(vix):dispatch(st,today,collect(st,today,now,bars_map,prev,piv,vix),bars_map,prev)
 else:print(f'M13 strict no-entry prev={meta} vix={vix}')
 if eod_ready(st,hhmm):finish_eod(st,today,now,bars_map,prev)
 st['cycles']=int(st.get('cycles',0))+1;save_state(st);print(f"M13 cycle trades={trade_count(st)}/3 open={open_count(st)} candidates={len(st.get('decisions',[]))} feeds={len(bars_map)}");return True

if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--loop',type=int,default=1);ap.add_argument('--test-alert',action='store_true');a=ap.parse_args()
 if a.test_alert:Alerts.test_alert();raise SystemExit(0)
 for i in range(max(1,a.loop)):
  if not mode_live():break
  if i<a.loop-1:time.sleep(240)

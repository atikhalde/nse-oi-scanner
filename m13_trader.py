"""Deterministic M13 equity momentum-scalp paper exit engine."""
from __future__ import annotations
from dataclasses import dataclass
import pandas as pd

BUFFER=.0002
MARGIN_RS=50000
LEVERAGE=5
NOTIONAL_CAP=MARGIN_RS*LEVERAGE
RISK_CAP=900
BOOK_R=.75
BOOK_FRAC=.60
PROVE_R=.35
PROVE_BARS=3
NO_EXTREME_BARS=2
LOCK_R=1.50
LOCK_TO_R=.75
TRAIL_R=.75
MAX_HOLD_BARS=12
COST_LOCK=.0008

@dataclass(frozen=True)
class Config:
    book_r:float=BOOK_R;book_frac:float=BOOK_FRAC;max_hold_bars:int=MAX_HOLD_BARS;runner:bool=True
PRIMARY=Config()

def _indicators(warm,bars):
    base=pd.concat([warm,bars[['open','high','low','close']].reset_index(drop=True)],ignore_index=True) if warm is not None and len(warm) else bars[['open','high','low','close']].reset_index(drop=True)
    e9=base.close.ewm(span=9,adjust=False).mean().values;off=len(base)-len(bars);return e9,off

def evaluate(sym,side,etime,entry,signal,bars,warmup=None,features=None,breadth_by_time=None,config=PRIMARY):
    ids=bars.index[bars.t==etime].tolist()
    if not ids:return {'symbol':sym,'error':'entry bar missing'}
    ei=ids[0];side=str(side);s=1 if side=='BUY' else -1;entry=float(entry)
    siglo=float(bars.low.iloc[ei]);sighi=float(bars.high.iloc[ei]);sl=siglo*(1-BUFFER) if side=='BUY' else sighi*(1+BUFFER);risk=abs(entry-sl)
    if risk<=0:return {'symbol':sym,'error':'invalid candle stop'}
    qty_notional=int(NOTIONAL_CAP//entry);qty_risk=int(RISK_CAP//risk)
    if qty_notional<1:return {'symbol':sym,'error':'notional qty=0'}
    if qty_risk<1:return {'symbol':sym,'error':'one-share risk exceeds cap'}
    qty=max(1,min(qty_notional,qty_risk));qbook=int(qty*config.book_frac) if qty>1 else 0;qrun=qty-qbook
    target=entry+s*config.book_r*risk;e9,off=_indicators(warmup,bars)
    events=[{'key':'ENTRY','time':etime,'price':entry}];legs=[];stop=sl;best=entry;open_q=qty;booked=False;locked=False;closed=False;broke=False;against=0;mfe=0.0
    for j in range(ei+1,len(bars)):
        o=float(bars.open.iloc[j]);h=float(bars.high.iloc[j]);lo=float(bars.low.iloc[j]);c=float(bars.close.iloc[j]);t=str(bars.t.iloc[j]);age=j-ei
        if (s==1 and lo<=stop) or (s==-1 and h>=stop):
            px=o if (s==1 and o<stop) or (s==-1 and o>stop) else stop;kind='RUNNER' if booked else 'SL';legs.append((f'{kind} {t}',open_q,px,t));events.append({'key':'EXIT_RUNNER' if booked else 'EXIT_SL','time':t,'price':px});open_q=0;closed=True;break
        best=max(best,h) if s==1 else min(best,lo);mfe=max(mfe,s*(best-entry)/risk)
        if (s==1 and h>sighi) or (s==-1 and lo<siglo):broke=True
        if not booked and qbook>0 and ((s==1 and h>=target) or (s==-1 and lo<=target)):
            legs.append((f'BOOK075 {t}',qbook,target,t));open_q-=qbook;booked=True;events.append({'key':'BOOK_075','time':t,'price':target})
        # Momentum failure exits are evaluated at bar close and apply immediately.
        if not booked and age>=NO_EXTREME_BARS and not broke:
            legs.append((f'FAIL_NO_EXTREME {t}',open_q,c,t));events.append({'key':'EXIT_FAILURE','time':t,'price':c});open_q=0;closed=True;break
        if not booked and age>=PROVE_BARS and mfe<PROVE_R:
            legs.append((f'FAIL_NO_MOMENTUM {t}',open_q,c,t));events.append({'key':'EXIT_FAILURE','time':t,'price':c});open_q=0;closed=True;break
        ema=float(e9[off+j]);against=against+1 if s*(c-ema)<0 else 0
        if booked:
            live_br=(breadth_by_time or {}).get(t)
            if live_br is not None and float(live_br)<.45:
                legs.append((f'BREADTH_FLIP {t}',open_q,c,t));events.append({'key':'EXIT_BREADTH','time':t,'price':c});open_q=0;closed=True;break
            be=entry*(1+COST_LOCK) if s==1 else entry*(1-COST_LOCK);stop=max(stop,be) if s==1 else min(stop,be)
            if mfe>=LOCK_R:
                lock=entry+s*LOCK_TO_R*risk;chand=best-s*TRAIL_R*risk
                if s==1:
                    swing=min(float(bars.low.iloc[max(ei,j-1):j+1].min())*(1-BUFFER),c);stop=max(stop,lock,chand,ema*(1-BUFFER),swing)
                else:
                    swing=max(float(bars.high.iloc[max(ei,j-1):j+1].max())*(1+BUFFER),c);stop=min(stop,lock,chand,ema*(1+BUFFER),swing)
                if not locked:locked=True;events.append({'key':'RUNNER_LOCK','time':t,'price':stop})
            if against>=2:
                legs.append((f'EMA_FAIL {t}',open_q,c,t));events.append({'key':'EXIT_RUNNER','time':t,'price':c});open_q=0;closed=True;break
        if age>=config.max_hold_bars:
            legs.append((f'TIME {t}',open_q,c,t));events.append({'key':'EXIT_TIME','time':t,'price':c});open_q=0;closed=True;break
    if not closed and open_q:
        t=str(bars.t.iloc[-1]);c=float(bars.close.iloc[-1]);legs.append((f'OPEN {t}',open_q,c,t))
    pnl=sum(s*(px-entry)*q for _l,q,px,_t in legs);risk_rs=risk*qty;r_total=pnl/risk_rs if risk_rs else 0
    parts=[]
    for lbl,q,px,t in legs:
        if lbl.startswith('BOOK075'):kind=f'{round(100*q/qty)}% book +{config.book_r:.2f}R'
        elif lbl.startswith('SL'):kind='SL'
        elif lbl.startswith('RUNNER'):kind='runner stop'
        elif lbl.startswith('FAIL'):kind='momentum failure'
        elif lbl.startswith('EMA'):kind='EMA failure'
        elif lbl.startswith('BREADTH'):kind='breadth flip'
        elif lbl.startswith('TIME'):kind='time exit'
        else:kind='OPEN'
        parts.append(f'{kind} {t}')
    return {'symbol':sym,'side':side,'time':etime,'signal':signal,'setup':'M13-A+','entry':round(entry,2),'sl':round(sl,2),'sl_anchor':'signal candle high/low ±0.02%',
      'risk_pts':round(risk,2),'risk_pct':round(risk/entry*100,3),'risk_rs':round(risk_rs,0),'qty':qty,'qty_full':qty_notional,'qty_capped':qty<qty_notional,'capital':round(qty*entry,0),
      'margin_rs':round(qty*entry/LEVERAGE,0),'book_target':round(target,2),'booked':booked,'runner_locked':locked,'trail_armed':booked,'trail_style':'M13 40% EMA9/2-bar runner',
      'legs':legs,'events':events,'exit_text':' · '.join(parts),'leg2_time':legs[-1][3] if legs else None,'pnl':round(pnl,0),'r_total':round(r_total,2),'closed':closed,'features':features or {}}

def fmt_alert(tr,key):
    arrow='🟢' if tr['side']=='BUY' else '🔴';base=f"<b>{tr['symbol']}</b> {arrow} {tr['side']} · {tr['signal']}"
    if key=='ENTRY':return (f"🅼13 🚨 ENTRY · {base}\n{tr['time']} · ₹{tr['entry']} · Qty {tr['qty']} · notional ₹{tr['capital']:,.0f} · margin ~₹{tr['margin_rs']:,.0f}\nSL ₹{tr['sl']} ({tr['sl_anchor']}) · planned risk ₹{tr['risk_rs']:,.0f}\n60% book ₹{tr['book_target']} (+0.75R) · 40% full-move runner")
    if key=='BOOK_075':return f"🅼13 💰 60% BOOKED · {base}\nRunner stop moves to entry+costs · P&L so far protected"
    if key=='RUNNER_LOCK':return f"🅼13 🧲 RUNNER LOCKED · {base}\n+1.5R printed · 40% trails EMA9 / two-bar structure"
    lbl={'EXIT_SL':'SL','EXIT_FAILURE':'MOMENTUM FAILURE','EXIT_RUNNER':'RUNNER EXIT','EXIT_BREADTH':'BREADTH FLIP','EXIT_TIME':'TIME EXIT'}.get(key,key)
    return f"🅼13 ⛔ {lbl} · {base}\n{tr['exit_text']} · gross P&L ₹{tr['pnl']:+,.0f} ({tr['r_total']:+.2f}R)"

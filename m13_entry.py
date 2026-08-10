"""M13 Equity Momentum A+ entry engine.

One setup only: S1 Morning Base + real master variant + opening breadth >=55% in
signal direction + prior-session VIX return >-2%. Anti-chase/reversion features
classify and rank; they never veto an otherwise-valid entry.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping
import numpy as np
import pandas as pd

MODEL_NAME = "M13 Equity Momentum A+"
ENTRY_START, ENTRY_END = "09:45", "12:00"
MAX_SIGNAL_AGE_MIN = 5.0
OPEN_BREADTH_MIN = 0.55
LIVE_BREADTH_OPPOSITE = 0.45
PRIOR_VIX_RETURN_MIN = -2.0
MAX_TRADES_PER_DAY = 3
MAX_CONCURRENT = 2
MAX_TRADES_PER_SIDE = 3
ONE_TRADE_PER_SYMBOL_DAY = True
ONE_TRADE_PER_SECTOR_DAY = True
MIN_A_PLUS_SCORE = 70.0

REAL_CODES = {80, 280} | set(range(101, 113)) | set(range(201, 221))
PREVIEW_CODES = {90, 290}


@dataclass(frozen=True)
class Decision:
    accepted: bool
    score: float
    reason: str
    code: int
    signal: str
    side: str
    time: str
    subtype: str
    features: dict
    def to_dict(self): return asdict(self)


def _finite(v):
    try: return bool(np.isfinite(float(v)))
    except (TypeError, ValueError): return False


def _clip(v, lo, hi): return float(np.clip(float(v), lo, hi))


def opening_breadth(bars_map: Mapping[str, pd.DataFrame], prev: Mapping[str, float], side: str) -> tuple[float|None,int]:
    up=total=0
    for sym,b in bars_map.items():
        pc=prev.get(sym)
        if b is None or b.empty or not _finite(pc) or float(pc)<=0: continue
        up += int(float(b['open'].iloc[0]) >= float(pc)); total += 1
    if total < 180: return None,total
    bull=up/total
    return (bull if side=='BUY' else 1-bull),total


def causal_features(engine_frame: pd.DataFrame, today_prefix: pd.DataFrame, side: str,
                    prev_close: float, master_total_score: float,
                    opening_breadth_dir: float, live_breadth_dir: float|None,
                    sector_breadth_dir: float|None, prior_vix_return: float,
                    video_setups: list[str]) -> dict:
    if engine_frame is None or engine_frame.empty or today_prefix is None or today_prefix.empty: raise ValueError('missing bars')
    if not _finite(prev_close) or float(prev_close)<=0: raise ValueError('fresh previous close unavailable')
    f=engine_frame.sort_index();c=f.close.astype(float);h=f.high.astype(float);lo=f.low.astype(float);v=f.volume.astype(float);pc=c.shift(1)
    tr=pd.concat([h-lo,(h-pc).abs(),(lo-pc).abs()],axis=1).max(axis=1);atr=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean().iloc[-1]
    if not _finite(atr) or atr<=0: raise ValueError('ATR unavailable')
    e9=c.ewm(span=9,adjust=False).mean().iloc[-1];e20=c.ewm(span=20,adjust=False).mean().iloc[-1];e50=c.ewm(span=50,adjust=False).mean().iloc[-1]
    day=today_prefix;close=float(day.close.iloc[-1]);op=float(day.open.iloc[-1]);day_open=float(day.open.iloc[0]);hi=float(day.high.iloc[-1]);low=float(day.low.iloc[-1]);rng=max(hi-low,1e-12);s=1 if side=='BUY' else -1
    typ=(day.high+day.low+day.close)/3;vwap=float((typ*day.volume).sum()/max(1.0,float(day.volume.sum())))
    rel20=float(v.iloc[-1]/v.tail(20).mean()) if len(v)>=10 and v.tail(20).mean()>0 else None
    ts=pd.Timestamp(f.index[-1]);prior=f[(f.index.strftime('%H:%M')==ts.strftime('%H:%M'))&(f.index.date<ts.date())].volume.astype(float).tail(20)
    clock=float(day.volume.iloc[-1]/prior.median()) if len(prior)>=5 and prior.median()>0 else None
    dir_prev=s*(close/float(prev_close)-1)*100;dir_gap=s*(day_open/float(prev_close)-1)*100
    clv=(close-low)/rng if side=='BUY' else (hi-close)/rng
    setups=sorted(set(video_setups or []))
    feats=dict(dir_prev_pct=dir_prev,dir_gap_pct=dir_gap,ema9_20_atr=s*(e9-e20)/atr,ema20_50_atr=s*(e20-e50)/atr,
      close_ema20_atr=s*(close-e20)/atr,close_vwap_atr=s*(close-vwap)/atr,candle_clv=clv,body_atr=s*(close-op)/atr,range_atr=rng/atr,
      relvol20=rel20,clock_relvol=clock,master_total_score=float(master_total_score) if _finite(master_total_score) else 0.0,
      opening_breadth_dir=opening_breadth_dir,live_breadth_dir=live_breadth_dir,sector_breadth_dir=sector_breadth_dir,
      prior_vix_return=float(prior_vix_return),video_setups='+'.join(setups),video_setup_count=len(setups),s1=int('S1' in setups),s2=int('S2' in setups),s3=int('S3' in setups),s4=int('S4' in setups))
    return feats


def classify_subtype(f: Mapping[str,object]) -> str:
    dp=float(f.get('dir_prev_pct') or 0);e20=float(f.get('close_ema20_atr') or 0);vw=float(f.get('close_vwap_atr') or 0)
    if -1.0 <= dp <= .20 and (e20 <= 1.32 or vw <= 1.37): return 'REVERSION-ALIGNMENT'
    if int(f.get('s2') or 0): return 'PULLBACK-REENTRY'
    if dp >= 2.5 or e20 >= 2.5 or vw >= 2.5: return 'MOMENTUM-EXTENDED'
    return 'MOMENTUM-CONTROLLED'


def score_features(f: Mapping[str,object]) -> float:
    # 30 volume, 25 master, 20 profile, 10 video, 10 sector, 5 execution.
    cv=float(f.get('clock_relvol')) if _finite(f.get('clock_relvol')) else 0.0
    vol=30*_clip((cv-1)/3,0,1)
    ms=float(f.get('master_total_score') or 0);master=25*_clip((ms-50)/45,0,1)
    dp=max(0,float(f.get('dir_prev_pct') or 0));profile=20*_clip(dp/2.5,0,1)
    if int(f.get('s3') or 0): video=10
    elif int(f.get('s2') or 0): video=8
    else: video=6
    sb=float(f.get('sector_breadth_dir')) if _finite(f.get('sector_breadth_dir')) else .5;sector=10*_clip((sb-.45)/.40,0,1)
    clv=float(f.get('candle_clv') or 0);body=float(f.get('body_atr') or 0);execution=2.5*_clip((clv-.5)/.4,0,1)+2.5*_clip(body/.8,0,1)
    subtype=classify_subtype(f)
    if subtype in ('MOMENTUM-CONTROLLED','PULLBACK-REENTRY'): profile=min(20,profile+1.5)
    return round(vol+master+profile+video+sector+execution,2)


def decide(code:int,signal:str,side:str,etime:str,f:Mapping[str,object]) -> Decision:
    code=int(code);side=str(side).upper();signal=str(signal);sub=classify_subtype(f)
    def no(r): return Decision(False,score_features(f),r,code,signal,side,etime,sub,dict(f))
    if code in PREVIEW_CODES or code not in REAL_CODES: return no('not a real enabled chart master variant')
    if (side=='BUY') != (code<200): return no('side/code mismatch')
    if etime<ENTRY_START or etime>ENTRY_END: return no(f'outside {ENTRY_START}-{ENTRY_END}')
    if not int(f.get('s1') or 0): return no('S1 Morning Base is mandatory')
    if not _finite(f.get('opening_breadth_dir')) or float(f['opening_breadth_dir'])<OPEN_BREADTH_MIN: return no('opening market breadth below 55% in signal direction')
    if not _finite(f.get('prior_vix_return')) or float(f['prior_vix_return'])<=PRIOR_VIX_RETURN_MIN: return no('prior-session VIX return <= -2%')
    if _finite(f.get('live_breadth_dir')) and float(f['live_breadth_dir'])<LIVE_BREADTH_OPPOSITE: return no('live breadth flipped to opposite regime')
    score=score_features(f)
    if score<MIN_A_PLUS_SCORE: return no(f'A+ score {score:.2f} below {MIN_A_PLUS_SCORE:.2f}')
    return Decision(True,score,'qualified',code,signal,side,etime,sub,dict(f))

#!/usr/bin/env python3
"""Unit tests for options_common (M3/M4). No network: chain/lots/HTTP mocked."""
import sys, types, math
import pandas as pd
sys.path.insert(0, "/home/user/forward-paper-test")
import options_common as O

FAIL = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: FAIL.append(name)

# --- 1) Black-Scholes sanity: put-call parity  C - P = S - K e^{-rT}
S, K, T, sg = 100.0, 95.0, 7/365, 0.30
c = O.bs_price(S, K, T, sg, True); p = O.bs_price(S, K, T, sg, False)
check("A: put-call parity", abs((c - p) - (S - K*math.exp(-O.R_FREE*T))) < 1e-9)
check("B: call delta in (0,1), put = call-1", 0 < O.bs_delta(S,K,T,sg,True) < 1 and abs(O.bs_delta(S,K,T,sg,False)-(O.bs_delta(S,K,T,sg,True)-1))<1e-9)

# --- 2) pick_itm1 on a fake chain
fake_chain = {"oc": {}}
for k in (7400., 7500., 7600., 7700.):
    fake_chain["oc"][f"{k:.6f}"] = {
        "ce": {"last_price": max(0,(7612-k))*0.6+15, "greeks": {"delta": 0.58 if k<7612 else 0.41},
               "implied_volatility": 28.0, "security_id": 1000+int(k)},
        "pe": {"last_price": max(0,(k-7612))*0.6+15, "greeks": {"delta": -0.58 if k>7612 else -0.41},
               "implied_volatility": 28.0, "security_id": 2000+int(k)}}
pk = O.pick_itm1(fake_chain, 7612.0, "BUY")
check("C: BUY -> 1st ITM CE = 7600", pk["strike"]==7600. and pk["opt_type"]=="CE" and pk["delta"]==0.58)
pk = O.pick_itm1(fake_chain, 7612.0, "SELL")
check("D: SELL -> 1st ITM PE = 7700", pk["strike"]==7700. and pk["opt_type"]=="PE" and pk["delta"]==0.58)
check("E: sigma %->dec", abs(pk["sigma"]-0.28) < 1e-9)

# --- 3) enter(): sizing + caps (mock chain/lots)
class _DT:  # fixed now
    def strftime(self, f, *a): return "2026-07-23" if f=="%Y-%m-%d" else "10:10"
O.expiry_list = lambda sym, sid: "2026-07-28"
O.fetch_chain = lambda sym, sid, expiry: fake_chain
O.lot_size = lambda sec: 100
def ubars(rows):  # rows: list of (t,o,h,l,c)
    df = pd.DataFrame(rows, columns=["t","open","high","low","close"])
    df["dt"] = pd.to_datetime("2026-07-23 " + df["t"]).dt.tz_localize("Asia/Kolkata")
    return df
B = ubars([("10:10",7674.,7680.,7672.,7674.5),("10:15",7675.,7690.,7670.,7686.),
           ("10:20",7686.,7700.,7685.,7695.),("10:25",7695.,7710.,7694.,7702.),
           ("10:30",7702.,7701.,7680.,7684.)])
tr = O.enter("EICHERMOT","BUY","10:10",7674.5,"BUY-EX4",B,_DT())
check("F: entry builds CE trade", "error" not in tr and tr["opt_type"]=="CE" and tr["strike"]==7600.)
# UL SL = signal-bar low 7672*(1-0.0002)=7670.47; ul_risk=4.03; R=0.58*4.03=2.34
exp_sl = round(tr["entry"] - 0.58*abs(7674.5-7672*(1-0.0002)), 2)
check("G: prem SL = entry - delta×UL-risk", abs(tr["sl"]-exp_sl) < 0.02)
check("H: TP1 = entry + R (1:1)", abs(tr["tp1"]-(2*tr["entry"]-tr["sl"])) < 0.02)
check("I: lots respect caps",
      tr["lots"] == min(int(1900//((tr["entry"]-tr["sl"])*100)), int(50000//(tr["entry"]*100))))

# 1-lot risk breach -> skip (tiny SL distance => huge delta risk per lot is inverse; force via lot_size)
O.lot_size = lambda sec: 100000
tr2 = O.enter("EICHERMOT","BUY","10:10",7674.5,"BUY-EX4",B,_DT())
check("J: skip when 1 lot breaches ₹2,000", "error" in tr2 and "breaches" in tr2["error"])
O.lot_size = lambda sec: 100

# --- 4) evaluate_opt paths with SYNTH option bars (dict built from BS)
def opt_bars_from(tr_, ul_df, scale=1.0):
    rows=[]
    for _,r in ul_df.iterrows():
        base = tr_["entry"]
        prem = base + (r["close"]-7674.5)*0.58*scale
        rows.append((r["t"], prem, prem+1.5, prem-1.5, prem))
    return ubars(rows)

# path SL straight: underlying drops, premium falls below prem_sl
B2 = ubars([("10:10",7674.,7680.,7672.,7674.5),("10:15",7674.,7680.,7630.,7672.0),
            ("10:20",7632.,7635.,7620.,7625.),("10:25",7625.,7626.,7618.,7620.),("10:30",7620.,7622.,7615.,7618.)])
ob = opt_bars_from(tr, B2)
r = O.evaluate_opt(tr, B2, ob)
check("K: SL exit at prem_sl", r["closed"] and abs(r["legs"][-1][2]-tr["sl"])<1.51 and r["legs"][-1][0].startswith(("SL","trail")))
check("L: SL pnl == -risk_rs", abs(r["pnl"] + tr["risk_rs"]) < 2.0)

# path TP1 then trail: premium rises past tp1, then UL breaks a pivot
def run_up_then_crash():
    ul=[("10:10",7674.,7680.,7672.,7674.5),("10:15",7675.,7692.,7674.,7690.),
        ("10:20",7690.,7706.,7688.,7704.),("10:25",7704.,7714.,7692.,7694.),
        ("10:30",7694.,7696.,7686.,7688.),("10:35",7688.,7690.,7678.,7680.),
        ("10:40",7680.,7683.,7662.,7664.)]
    bu=ubars(ul)
    tr_=O.enter("EICHERMOT","BUY","10:10",7674.5,"BUY-EX4",bu,_DT())
    ob_=opt_bars_from(tr_,bu)
    return tr_,O.evaluate_opt(tr_,bu,ob_)
tr3,r3 = run_up_then_crash()
got1 = any(e["key"]=="TP1" for e in r3["events"])
check("M: TP1 booked then position trails/EOD", got1 and r3["closed"])
qty_out = sum(q for _l,q,_p,_t in r3["legs"])
check("N: qty conservation (all booked)", qty_out == tr3["qty"])

# path EOD square-off: flat premium, no SL, no TP1 -> EOD 15:20
B4 = ubars([("10:10",7674.,7680.,7672.,7674.5)]+[(f"1{h}:0{m}",7674.5,7680.,7672.,7674.8) for h in (0,1) for m in (0,5)][:1])
B4 = ubars([("10:10",7674.,7680.,7672.,7674.5),("10:15",7675.,7680.,7672.,7674.6),
            ("15:20",7674.,7680.,7672.,7674.7)])
tr4 = O.enter("EICHERMOT","BUY","10:10",7674.5,"BUY-EX4",B4,_DT())
r4 = O.evaluate_opt(tr4, B4, opt_bars_from(tr4,B4))
check("O: EOD 15:20 square-off", r4["closed"] and r4["legs"][-1][0].startswith("EOD 15:20"))

# synth fallback (no option bars -> BS marks, labeled)
r5 = O.evaluate_opt(tr, B2, None)
check("P: None option bars -> synth, still closes", r5["closed"] and r5["prem_src"]=="synth")

# 1-lot half-book integrity (qty=lot=100 => q1=50)
check("Q: report-compat fields", all(k in r for k in ("pnl","r_total","exit_text","events")) 
      and r["symbol"]=="EICHERMOT")

print()
print("RESULT:", "ALL CHECKS PASSED" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}")
sys.exit(1 if FAIL else 0)

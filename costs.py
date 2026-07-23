#!/usr/bin/env python3
"""Brokerage + statutory costs for the paper ledgers (subtracted per-trade).

Rates = current July-2026 schedule (post Budget-2026 STT hike, NSE txn slabs):
  EQUITY INTRADAY (M1/M2):  brokerage min(₹20, 0.03%)/order · STT 0.025% sell ·
      txn NSE 0.00307% · stamp 0.003% buy · SEBI ₹10/crore · GST 18% on (brk+txn+sebi)
  OPTIONS BUYING (M3/M4):   brokerage ₹20 flat/order · STT 0.15% sell premium ·
      txn NSE 0.03553% on premium · stamp 0.003% buy · SEBI ₹10/crore · GST 18%
DP charges don't apply (no delivery). IPFT ₹0.01/crore excluded (< 1 paisa).
Each entry/exit leg in the paper ledger counts as one executed order (like real).
"""
EQ = dict(brk_pct=0.0003, brk_cap=20.0, stt_sell=0.00025, txn=0.0000307,
          stamp_buy=0.00003, sebi=0.000001, gst=0.18)
OPT = dict(brk_flat=20.0, stt_sell=0.0015, txn=0.0003553,
           stamp_buy=0.00003, sebi=0.000001, gst=0.18)

# SLIPPAGE MODEL (24-Jul-2026) — closes the paper-vs-live fill gap.
# Paper legs record bar prices; a real MARKET order crosses the spread + impact.
#   stocks : normal fills 0.02% · stop fills (SL/trail, SL-M bursts) 0.05%
#            (large-cap F&O names: ~1-2 ticks + tiny impact for <=Rs50k orders)
#   options: normal fills 0.20% · stop fills 0.40% of premium (ITM near-ATM spreads)
# statutory charges stay exact; slippage is the honest haircut on every fill.
STOCK_SLIP = dict(base=0.0002, stop=0.0005)
OPT_SLIP = dict(base=0.0020, stop=0.0040)


def _slip_rate(lbl, rates):
    s = str(lbl).lower()
    return rates["stop"] if s.startswith(("sl", "trail")) else rates["base"]


def trade_slippage(tr):
    """Rs lost to market-order fills vs recorded bar prices, mirroring _orders()."""
    rates = OPT_SLIP if is_option(tr) else STOCK_SLIP
    slip = float(tr["entry"]) * int(tr.get("qty", 0)) * rates["base"]     # entry is a market order
    legs = tr.get("legs")
    if legs:
        for lbl, q, px, _t in legs:
            if str(lbl).startswith("OPEN"):
                continue
            slip += float(px) * int(q) * _slip_rate(lbl, rates)
    else:                          # legacy v1 shape: exit legs ~ normal fills
        for _side, q, px in _orders(tr)[1:]:
            slip += px * q * rates["base"]
    return slip


def is_option(tr):
    return tr.get("setup") == "OPTIONS-BUY" or bool(tr.get("contract"))


def _orders(tr):
    """Explode a trade dict into executed orders: list of (side, qty, price).
    'OPEN' pseudo-legs are NOT orders (mark-to-market only)."""
    side, qty = tr["side"], int(tr.get("qty", 0))
    entry = float(tr["entry"])
    out = [(side, qty, entry)]
    legs = tr.get("legs")
    if legs:
        for lbl, q, px, _t in legs:
            if str(lbl).startswith("OPEN"):
                continue
            out.append(("SELL" if side == "BUY" else "BUY", int(q), float(px)))
        return out
    # legacy v1 two-leg shape (no legs list)
    flip = "SELL" if side == "BUY" else "BUY"
    q1 = tr.get("leg1_qty")
    if q1 and tr.get("leg1_px") is not None:
        out.append((flip, int(q1), float(tr["leg1_px"])))
        q2 = qty - int(q1)
        if q2 > 0 and tr.get("leg2_px") is not None and tr.get("leg2_why") != "OPEN":
            out.append((flip, q2, float(tr["leg2_px"])))
        return out
    if tr.get("leg2_px") is not None and tr.get("leg2_why") != "OPEN":
        out.append((flip, qty, float(tr["leg2_px"])))
    return out


def trade_costs(tr):
    """Full cost breakdown + net P&L for one paper trade."""
    r = OPT if is_option(tr) else EQ
    brk = stt = txn = stamp = 0.0
    turnover = 0.0
    for side, q, px in _orders(tr):
        val = q * px
        turnover += val
        if "brk_flat" in r:
            brk += r["brk_flat"]
        else:
            brk += min(r["brk_cap"], r["brk_pct"] * val)
        txn += r["txn"] * val
        if side == "SELL":
            stt += r["stt_sell"] * val
        else:
            stamp += r["stamp_buy"] * val
    sebi = r["sebi"] * turnover
    gst = r["gst"] * (brk + txn + sebi)
    total = brk + stt + txn + sebi + stamp + gst
    pnl = float(tr.get("pnl", 0))
    slip = trade_slippage(tr)
    drag = total + slip
    return {"brokerage": round(brk, 2), "stt": round(stt, 2), "txn": round(txn, 2),
            "sebi": round(sebi, 2), "stamp": round(stamp, 2), "gst": round(gst, 2),
            "orders": len(_orders(tr)), "total": round(total, 2),
            "slippage": round(slip, 2), "drag": round(drag, 2),
            "gross": round(pnl, 2), "net": round(pnl - drag, 2)}


NOTE = ("costs INCLUDED per trade: Dhan ₹20 flat (options) / min(₹20,0.03%) (intraday) + "
        "STT 0.15% sell-prem / 0.025% sell-eq + NSE txn 0.03553%/0.00307% + SEBI ₹10/cr + "
        "stamp 0.003% buy + GST 18% (Jul-2026 rates) + SLIPPAGE model: stocks 0.02%/0.05% "
        "normal/SL fills, options 0.20%/0.40% premium · entry @ signal-bar close (fast-VPS "
        "convention; GitHub cycles can see a signal up to 15 min late — residual gap)")

if __name__ == "__main__":
    # golden self-checks (hand-computed)
    t = {"side": "BUY", "qty": 100, "entry": 1000.0, "pnl": 1000.0,
         "legs": [("EOD 15:20", 100, 1010.0, "15:20")]}
    c = trade_costs(t)
    exp = 40 + 25.25 + 6.17 + 0.20 + 3.0 + 0.18 * (40 + 6.17 + 0.20)
    assert abs(c["total"] - exp) < 0.05, (c, exp)
    o = {"side": "BUY", "qty": 100, "entry": 100.0, "pnl": 1000.0, "setup": "OPTIONS-BUY",
         "legs": [("T1 10:15", 50, 110.0, "10:15"), ("EOD 15:20", 50, 110.0, "15:20")]}
    c2 = trade_costs(o)
    exp2 = 60 + 0.0015 * 11000 + 0.0003553 * 21000 + 0.00003 * 10000 + \
           0.000001 * 21000 + 0.18 * (60 + 0.0003553 * 21000 + 0.000001 * 21000)
    assert abs(c2["total"] - exp2) < 0.05, (c2, exp2)
    s = {"side": "SELL", "qty": 100, "entry": 1000.0, "pnl": 1000.0,
         "legs": [("SL 10:10", 100, 990.0, "10:10")]}
    c3 = trade_costs(s)
    exp3 = 40 + 25.0 + 0.0000307 * 199000 + 0.00003 * 99000 + 0.000001 * 199000 + \
           0.18 * (40 + 0.0000307 * 199000 + 0.000001 * 199000)
    assert abs(c3["total"] - exp3) < 0.05, (c3, exp3)
    # slippage goldens (hand-computed): entry always base rate on full qty
    assert abs(c["slippage"] - (100*1000*0.0002 + 100*1010*0.0002)) < 0.05            # 40.20
    assert abs(c2["slippage"] - (100*100*0.0020 + 50*110*0.0020 + 50*110*0.0020)) < 0.05  # 42.00
    assert abs(c3["slippage"] - (100*1000*0.0002 + 100*990*0.0005)) < 0.05            # 69.50
    assert c["net"] < c["gross"] - c["total"] + 0.01          # net includes slip
    print("costs golden checks OK",
          [c["drag"], c2["drag"], c3["drag"]])

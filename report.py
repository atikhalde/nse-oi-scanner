"""End-of-day / on-demand paper-test report (xlsx) + telegram summary text."""
import os
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

HDR_FILL = PatternFill("solid", fgColor="0F172A")
HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
BUY_FILL = PatternFill("solid", fgColor="00783C")
SELL_FILL = PatternFill("solid", fgColor="9B2D2D")
W_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(color="0F172A", bold=True, size=14)
BASE_FONT = Font(size=10)
THIN = Border(*[Side(style="thin", color="D0D7E2")] * 4)
GREEN, RED = Font(color="00783C", bold=True, size=10), Font(color="9B2D2D", bold=True, size=10)

COLS = ["Date", "Symbol", "Side", "Signal", "SpurtRank", "Entry time", "Entry ₹", "Qty",
        "Capital ₹", "SL ₹", "Exit", "Exit time", "Avg exit ₹", "P&L ₹", "P&L %", "R"]


def exit_path(tr):
    if tr["leg1_why"] == "SL":
        return f"SL {tr['leg2_time']}"
    if tr["leg1_why"] and tr["leg1_why"].startswith("TP1"):
        e2 = "EOD 15:20" if tr["leg2_why"] == "EOD15:20" else f"{tr['leg2_why']} {tr['leg2_time']}"
        return f"50% TP1@1:2 {tr['leg1_time']} · 50% {e2}"
    if tr["leg2_why"] == "OPEN":
        return "OPEN"
    return "EOD 15:20" if tr["leg2_why"] == "EOD15:20" else str(tr["leg2_why"])


def build(trades, date_lbl, gate_meta, out_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Paper test"
    ws.sheet_view.showGridLines = False
    ws.cell(row=1, column=1, value=f"PAPER TEST — MASTER SCANNER + TOP-30 OI-SPURT GATE — {date_lbl}").font = TITLE_FONT
    ws.cell(row=2, column=1,
            value=f"OI gate: {gate_meta.get('status')} ({gate_meta.get('source')}) · "
                  f"₹50,000/stock · SL=pivot∓0.02% · 50%@1:2 · trail after 1:3 · sq-off 15:20 · costs NOT included").font = Font(size=9)
    hr = 4
    for j, col in enumerate(COLS, 1):
        c = ws.cell(row=hr, column=j, value=col)
        c.fill, c.font, c.border = HDR_FILL, HDR_FONT, THIN
        ws.column_dimensions[get_column_letter(j)].width = max(11, min(len(col) + 4, 30))
    ws.column_dimensions["K"].width = 32
    r = hr + 1
    tot = 0.0
    for tr in trades:
        sign = 1 if tr["side"] == "BUY" else -1
        avg_ex = (tr["leg1_px"] if tr["leg1_px"] is not None else tr["leg2_px"])
        avg_ex = round(((avg_ex + tr["leg2_px"]) / 2), 2)
        pnl_pct = sign * (avg_ex - tr["entry"]) / tr["entry"] * 100
        tot += tr["pnl"]
        vals = [date_lbl, tr["symbol"], tr["side"], tr["signal"], tr.get("gate_rank", "-"),
                tr["time"], tr["entry"], tr["qty"], tr["capital"], tr["sl"], exit_path(tr),
                tr["leg2_time"], avg_ex, tr["pnl"], round(pnl_pct, 2), tr["r_total"]]
        for j, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=j, value=v)
            c.font, c.border = BASE_FONT, THIN
            if COLS[j - 1] == "Side":
                c.fill = BUY_FILL if v == "BUY" else SELL_FILL
                c.font = W_FONT
            if COLS[j - 1] in ("P&L ₹", "R", "P&L %"):
                c.font = GREEN if (v or 0) > 0 else RED
            if COLS[j - 1] in ("Capital ₹", "P&L ₹"):
                c.number_format = "#,##0"
        r += 1
    c = ws.cell(row=r, column=11, value="TOTAL")
    c.font = Font(bold=True)
    c = ws.cell(row=r, column=14, value=round(tot, 0))
    c.font = GREEN if tot > 0 else RED
    c.number_format = "#,##0"
    wb.save(out_path)
    return out_path


def summary_text(trades, date_lbl, gate_meta):
    wins = [t for t in trades if t["pnl"] > 0]
    tot = sum(t["pnl"] for t in trades)
    rsum = sum(t["r_total"] for t in trades)
    lines = [f"📊 <b>PAPER TEST {date_lbl} — EOD</b>",
             f"Gate: {gate_meta.get('status')} ({gate_meta.get('source')})",
             f"Trades: {len(trades)} · Wins: {len(wins)} ({len(wins)/len(trades)*100:.0f}%)" if trades else "Trades: 0",
             f"P&L: <b>₹{tot:+,.0f}</b> · {rsum:+.2f}R"]
    for t in trades:
        lines.append(f"• {t['symbol']} {t['side']} {t['signal']} ₹{t['pnl']:+,.0f} ({t['r_total']:+.2f}R) {exit_path(t)}")
    return "\n".join(lines)

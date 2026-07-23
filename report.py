"""End-of-day / on-demand paper-test report (xlsx) + telegram summary text.
Supports the setup-aware exits v2 trade format (legs) and gracefully renders
older single-exit trade dicts too."""
import os
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

HDR_FILL = PatternFill("solid", fgColor="0F172A")
HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
BUY_FILL = PatternFill("solid", fgColor="00783C")
SELL_FILL = PatternFill("solid", fgColor="9B2D2D")
SKIP_FILL = PatternFill("solid", fgColor="F5F2E8")
W_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(color="0F172A", bold=True, size=14)
BASE_FONT = Font(size=10)
THIN = Border(*[Side(style="thin", color="D0D7E2")] * 4)
GREEN, RED = Font(color="00783C", bold=True, size=10), Font(color="9B2D2D", bold=True, size=10)
GREY = Font(color="6B7280", size=9)

COLS = ["Date", "Symbol", "Side", "Signal", "Setup", "SpurtRank", "Entry time", "Entry ₹", "Qty",
        "Capital ₹", "SL ₹", "Exit", "Exit time", "Avg exit ₹", "P&L ₹", "P&L %", "R"]

RULES_NOTE = ("setup-aware exits v2 · qty ≤ ₹50k (shrunk when structure risk > ₹1,000 max-loss cap, "
              "₹900 planned) · 50%@1:2 · 30%@1:3 · rest structure/EMA9/ATR trail after +1R · "
              "sq-off 15:20 · costs NOT included")


def exit_path(tr):
    if tr.get("exit_text"):                       # v2 engine: pre-built from legs
        return tr["exit_text"]
    if tr.get("leg1_why") == "SL":
        return f"SL {tr['leg2_time']}"
    if tr.get("leg1_why") and str(tr.get("leg1_why")).startswith("TP1"):
        e2 = "EOD 15:20" if tr.get("leg2_why") == "EOD15:20" else f"{tr.get('leg2_why')} {tr.get('leg2_time')}"
        return f"50% TP1@1:2 {tr['leg1_time']} · 50% {e2}"
    if tr.get("leg2_why") == "OPEN":
        return "OPEN"
    return "EOD 15:20" if tr.get("leg2_why") == "EOD15:20" else str(tr.get("leg2_why"))


def _avg_exit(tr):
    """Weighted average exit across legs (v2) or two-leg fallback (v1)."""
    if tr.get("legs"):
        qs = sum(q for _l, q, _p, _t in tr["legs"])
        return round(sum(p * q for _l, q, p, _t in tr["legs"]) / (qs or 1), 2)
    avg = tr["leg1_px"] if tr.get("leg1_px") is not None else tr["leg2_px"]
    return round((avg + tr["leg2_px"]) / 2, 2)


def build(trades, date_lbl, gate_meta, out_path, skipped=None, rules_note=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "Paper test"
    ws.sheet_view.showGridLines = False
    ws.cell(row=1, column=1, value=f"PAPER TEST — MASTER SCANNER + TOP-30 OI-SPURT GATE — {date_lbl}").font = TITLE_FONT
    note = rules_note or RULES_NOTE
    src = gate_meta.get("source") if isinstance(gate_meta, dict) else None
    ws.cell(row=2, column=1, value=f"OI gate: {gate_meta.get('status')} ({src}) · {note}"
            if isinstance(gate_meta, dict) and "status" in gate_meta else str(gate_meta)).font = Font(size=9)
    hr = 4
    for j, col in enumerate(COLS, 1):
        c = ws.cell(row=hr, column=j, value=col)
        c.fill, c.font, c.border = HDR_FILL, HDR_FONT, THIN
        ws.column_dimensions[get_column_letter(j)].width = max(11, min(len(col) + 4, 30))
    ws.column_dimensions["L"].width = 34
    r = hr + 1
    tot = 0.0
    for tr in trades:
        sign = 1 if tr["side"] == "BUY" else -1
        avg_ex = _avg_exit(tr)
        pnl_pct = sign * (avg_ex - tr["entry"]) / tr["entry"] * 100
        tot += tr["pnl"]
        vals = [date_lbl, tr["symbol"], tr["side"], tr["signal"], tr.get("setup", "-"),
                tr.get("gate_rank", tr.get("spurt_rank", "-")), tr["time"], tr["entry"],
                tr["qty"], tr.get("capital", round(tr["qty"] * tr["entry"], 0)), tr["sl"],
                exit_path(tr), tr.get("leg2_time"), avg_ex, tr["pnl"], round(pnl_pct, 2), tr["r_total"]]
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
    wins = sum(1 for t in trades if t["pnl"] > 0)
    c = ws.cell(row=r, column=11,
                value=f"TOTAL ({len(trades)} trades · {wins} wins · {wins * 100 // max(1, len(trades))}%)")
    c.font = Font(bold=True)
    c = ws.cell(row=r, column=15, value=round(tot, 0))
    c.font = GREEN if tot > 0 else RED
    c.number_format = "#,##0"
    rsum = sum(t.get("r_total", 0) for t in trades)
    c = ws.cell(row=r, column=17, value=round(rsum, 2))
    c.font = GREEN if rsum > 0 else RED

    # --- optional disclosure block: signals that were NOT traded
    if skipped:
        r += 2
        c = ws.cell(row=r, column=1,
                    value=f"SKIPPED — NOT TRADED ({sum(len(v) for v in skipped.values())})")
        c.font = Font(color="0F172A", bold=True, size=10)
        for why, items in skipped.items():
            r += 1
            c = ws.cell(row=r, column=1, value=f"{why} ({len(items)})")
            c.font = Font(bold=True, size=9)
            c.fill = SKIP_FILL
            r += 1
            for j, col in enumerate(["Symbol", "Side", "Signal", "Entry time", "Signal ₹"], 1):
                c = ws.cell(row=r, column=j, value=col)
                c.fill, c.font, c.border = SKIP_FILL, Font(bold=True, size=9), THIN
            r += 1
            for it in items:
                for j, v in enumerate(it, 1):
                    c = ws.cell(row=r, column=j, value=v)
                    c.font, c.border = BASE_FONT, THIN
                    if j == 2:
                        c.fill = BUY_FILL if v == "BUY" else SELL_FILL
                        c.font = W_FONT
                r += 1
        ws.cell(row=r + 1, column=1, value="All rows above are real chart signals; skipped only by the "
                                            "trade-selection/flat rule — signal fidelity itself is untouched.").font = GREY
    wb.save(out_path)
    return out_path


def summary_text(trades, date_lbl, gate_meta):
    wins = [t for t in trades if t["pnl"] > 0]
    tot = sum(t["pnl"] for t in trades)
    rsum = sum(t["r_total"] for t in trades)
    st = gate_meta.get("status") if isinstance(gate_meta, dict) else gate_meta
    src = gate_meta.get("source") if isinstance(gate_meta, dict) else ""
    lines = [f"📊 <b>PAPER TEST {date_lbl} — EOD</b>",
             f"Gate: {st} ({src}) · exits v2 (50/30/20 · ₹1,000 max-loss cap)",
             f"Trades: {len(trades)} · Wins: {len(wins)} ({len(wins)/len(trades)*100:.0f}%)" if trades else "Trades: 0",
             f"P&L: <b>₹{tot:+,.0f}</b> · {rsum:+.2f}R"]
    for t in trades:
        tag = f"{t.get('setup','')[:4]} " if t.get("setup") else ""
        lines.append(f"• {t['symbol']} {t['side']} {tag}{t['signal']} ₹{t['pnl']:+,.0f} ({t['r_total']:+.2f}R) {exit_path(t)}")
    return "\n".join(lines)

#!/usr/bin/env python3
"""Reproduce the 2026-11-25 1m audit analysis from the raw JSON + HTML exports.

No third-party packages are required. Run from anywhere:

    python results/scan1m_2026-11-25/analyze.py

Inputs (user-provided, copied into ./inputs):
    audit-2026-11-25-1m.json   authoritative audit export
    board-2026-11-25-1m.html    authoritative board export
    audit-2026-11-25-1m.pdf     print view, checksum only
    board-2026-11-25-1m.pdf     print view, checksum only

Outputs are deterministic CSV/JSON files next to this script. The report is
README.md; this script writes the evidence tables and metrics used in it.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import statistics
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "inputs"
AUDIT_PATH = INPUT / "audit-2026-11-25-1m.json"
HTML_PATH = INPUT / "board-2026-11-25-1m.html"
INPUT_FILES = [
    AUDIT_PATH,
    HTML_PATH,
    INPUT / "audit-2026-11-25-1m.pdf",
    INPUT / "board-2026-11-25-1m.pdf",
]


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def num(s: str | None):
    if s is None:
        return None
    s = clean(s).replace("+", "").replace(",", "")
    if s in ("", "—", "–", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


class TableParser(HTMLParser):
    """Minimal table extractor that preserves td/th attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict]]] = []
        self.table = None
        self.row = None
        self.cell = None
        self.h2_buf = None
        self.h2s: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = {"tag": tag, "attrs": dict(attrs), "text": ""}
        elif tag == "h2":
            self.h2_buf = ""
        elif tag == "br" and self.cell is not None:
            self.cell["text"] += " "

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data
        if self.h2_buf is not None:
            self.h2_buf += data

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.cell is not None:
            self.cell["text"] = clean(self.cell["text"])
            if self.row is not None:
                self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None
        elif tag == "h2" and self.h2_buf is not None:
            self.h2s.append(clean(self.h2_buf))
            self.h2_buf = None


def cell_text(cell):
    return cell["text"]


def parse_board_html(raw: str):
    p = TableParser()
    p.feed(raw)
    if len(p.tables) < 17:
        raise RuntimeError(f"expected 17 tables, got {len(p.tables)}")

    # Table 0: sector board. data-v = persist value; title carries day/intra.
    table0 = p.tables[0]
    header = [cell_text(c) for c in table0[0]]
    bars = [x for x in header if re.fullmatch(r"\d\d:\d\d", x)]
    sector_rows = []
    for row in table0[1:]:
        sec = cell_text(row[0])
        side_txt = cell_text(row[1])
        rec = {
            "sector": sec,
            "side_final": 1 if side_txt == "long" else -1,
            "persist": {},
            "day": {},
            "intra": {},
            "picked": {},
        }
        for bar, cell in zip(bars, row[2 : 2 + len(bars)]):
            rec["persist"][bar] = num(cell["attrs"].get("data-v"))
            title = cell["attrs"].get("title", "")
            m = re.search(r"day ([+-]?[\d.]+)% · intra ([+-]?[\d.]+)%", title)
            if m:
                rec["day"][bar] = float(m.group(1))
                rec["intra"][bar] = float(m.group(2))
            classes = (cell["attrs"].get("class") or "").split()
            rec["picked"][bar] = "pick" in classes
        tail = [cell_text(c) for c in row[-3:]]
        rec["day_pct"] = num(tail[0])
        rec["intra_pct"] = num(tail[1])
        rec["picks"] = int(float(tail[2]))
        sector_rows.append(rec)

    # Detail heatmaps: one table per sector. Preserve point-in-time rank/pos/mv.
    headings = [h for h in p.h2s if re.match(r"\w+ \((long|short)\)", h)]
    if len(headings) != 15 or len(p.tables[2:]) != 15:
        raise RuntimeError("unexpected heatmap heading/table count")
    details = []
    for htext, tab in zip(headings, p.tables[2:]):
        m = re.match(r"(\S+) \((long|short)\) — (\d+) name\(s\), (\d+) candle\(s\)", htext)
        if not m:
            raise RuntimeError(f"unparsed heatmap heading: {htext}")
        sec = m.group(1)
        side = 1 if m.group(2) == "long" else -1
        hbars = [cell_text(c) for c in tab[0][1:]]
        for row in tab[1:]:
            rec = {
                "name": cell_text(row[0]),
                "sector": sec,
                "side_board": side,
                "bars": hbars,
                "mv": {},
                "rank": {},
                "pos": {},
                "hit": {},
            }
            for bar, cell in zip(hbars, row[1:]):
                rec["mv"][bar] = num(cell["attrs"].get("data-v"))
                title = cell["attrs"].get("title", "")
                rm = re.search(r"rank\s+([\d.]+)\s*·\s*#(\d+)", title)
                if rm:
                    rec["rank"][bar] = float(rm.group(1))
                    rec["pos"][bar] = int(rm.group(2))
                rec["hit"][bar] = "hit" in (cell["attrs"].get("class") or "").split()
            details.append(rec)
    return bars, sector_rows, details


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})


def main():
    missing = [str(p) for p in INPUT_FILES if not p.exists()]
    if missing:
        raise SystemExit("missing raw input files: " + ", ".join(missing))

    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    board_html = HTML_PATH.read_text(encoding="utf-8")
    bars, sector_rows, details = parse_board_html(board_html)
    sector_rows_by_name = {r["sector"]: r for r in sector_rows}
    det_by_key = {(r["name"], r["sector"]): r for r in details}

    # Cross-check the raw HTML against the JSON board summary.
    summary = audit["board"]["summary"]
    assert len(summary) == 190, len(summary)
    assert len(details) == 190, len(details)
    assert len(bars) == 72, len(bars)
    assert sum(sum(r["picked"].values()) for r in sector_rows) == audit["counts"]["judged"]
    html_top5 = {k: sum(r["hit"].values()) for k, r in det_by_key.items()}
    mismatches = [
        (k, c, s["top5"])
        for k, c in html_top5.items()
        for s in [next((x for x in summary if (x["name"], x["sector"]) == k), None)]
        if s is not None and c != s["top5"]
    ]
    assert not mismatches, mismatches[:5]

    # Raw, reproducible long-form tables.
    name_bar_rows = []
    for r in details:
        for b in r["bars"]:
            name_bar_rows.append(
                {
                    "name": r["name"],
                    "sector": r["sector"],
                    "side_board": r["side_board"],
                    "bar": b,
                    "mv": r["mv"].get(b),
                    "rank": r["rank"].get(b),
                    "pos": r["pos"].get(b),
                    "hit": int(r["hit"].get(b, False)),
                }
            )
    write_csv(
        ROOT / "board_name_bars.csv",
        ["name", "sector", "side_board", "bar", "mv", "rank", "pos", "hit"],
        name_bar_rows,
    )

    sector_bar_rows = []
    for t in audit["timeline"]:
        for sec, sv in t["sectors"].items():
            bull, bear = sv["persist_bull"], sv["persist_bear"]
            h = sector_rows_by_name[sec]
            sector_bar_rows.append(
                {
                    "sector": sec,
                    "bar": t["bar"],
                    "market": t["label"],
                    "conf": t["conf"],
                    "pick": t["pick"],
                    "persist_bull": bull,
                    "persist_bear": bear,
                    "dominant_side": 1 if bull >= bear else -1,
                    "dominant_persist": max(bull, bear),
                    "counter_persist": min(bull, bear),
                    "qualified": int(bool(sv["qualified"])),
                    "day_pct": sv["day_pct"],
                    "intra_pct": sv["intra_pct"],
                    "html_persist": h["persist"].get(t["bar"]),
                    "html_picked": int(h["picked"].get(t["bar"], False)),
                }
            )
    write_csv(
        ROOT / "board_sector_bars.csv",
        [
            "sector", "bar", "market", "conf", "pick",
            "persist_bull", "persist_bear", "dominant_side",
            "dominant_persist", "counter_persist", "qualified",
            "day_pct", "intra_pct", "html_persist", "html_picked",
        ],
        sector_bar_rows,
    )

    final_side = {k: v["side"] for k, v in audit["sectors"].items()}
    name_summary_rows = []
    for s in summary:
        fs = final_side[s["sector"]]
        name_summary_rows.append(
            {
                **s,
                "side_final": fs,
                "side_flipped": int(fs != s["side"]),
                "directional_last_pct": fs * s["last_mv"],
                "directional_peak_pct": fs * s["peak_mv"],
                "directional_trough_pct": fs * s["trough_mv"],
            }
        )
    write_csv(
        ROOT / "name_summary.csv",
        [
            "name", "sector", "side", "side_final", "side_flipped",
            "candles", "first_bar", "last_bar", "first_mv", "last_mv",
            "peak_mv", "trough_mv", "directional_last_pct",
            "directional_peak_pct", "directional_trough_pct",
            "best_rank", "best_pos", "top5", "rank_avg", "entered",
            "net_r", "blocked",
        ],
        name_summary_rows,
    )

    # "Big winner" = >=3 percentage points in the sector's final dominant
    # direction by 15:10, de-duplicated by name across overlapping sectors.
    unique = {}
    for r in name_summary_rows:
        if r["name"] not in unique or r["directional_last_pct"] > unique[r["name"]]["directional_last_pct"]:
            unique[r["name"]] = r
    big = sorted(
        [r for r in unique.values() if r["directional_last_pct"] >= 3.0],
        key=lambda r: r["directional_last_pct"],
        reverse=True,
    )
    write_csv(
        ROOT / "big_winners.csv",
        [
            "name", "sector", "side_final", "last_mv", "directional_last_pct",
            "peak_mv", "directional_peak_pct", "best_rank", "rank_avg",
            "top5", "entered", "net_r", "blocked",
        ],
        big,
    )

    # Entry quality: board rank/position on the candle the entry/alert fired.
    timeline_by_bar = {t["bar"]: t for t in audit["timeline"]}
    entry_rows = []
    for t in audit["trades"]:
        bar = t["entry_at"][11:16]
        entry_rows.append((bar, t["name"], t["sector"], t["side"], t["net_r"], t["why"], t["mfe_r"], t["give_back_r"]))
    open_trade = audit["open_trades"][0]
    open_bar = audit["alerts"][open_trade["name"]]["time"]
    entry_rows.append(
        (open_bar, open_trade["name"], open_trade["sector"], open_trade["side"],
         open_trade["net_r"], "still open", open_trade["mfe_r"], None)
    )
    entry_quality = []
    for bar, name, sec, side, net_r, why, mfe_r, give_back_r in entry_rows:
        d = det_by_key[(name, sec)]
        sv = timeline_by_bar[bar]["sectors"][sec]
        bull, bear = sv["persist_bull"], sv["persist_bear"]
        entry_quality.append(
            {
                "bar": bar,
                "name": name,
                "sector": sec,
                "side": side,
                "board_rank": d["rank"].get(bar),
                "board_pos": d["pos"].get(bar),
                "mv": d["mv"].get(bar),
                "market": timeline_by_bar[bar]["label"],
                "pick": timeline_by_bar[bar]["pick"],
                "persist_bull": bull,
                "persist_bear": bear,
                "dominant_persist": max(bull, bear),
                "counter_persist": min(bull, bear),
                "qualified": int(bool(sv["qualified"])),
                "net_r": net_r,
                "why": why,
                "mfe_r": mfe_r,
                "give_back_r": give_back_r,
            }
        )
    write_csv(
        ROOT / "entry_quality.csv",
        [
            "bar", "name", "sector", "side", "board_rank", "board_pos", "mv",
            "market", "pick", "persist_bull", "persist_bear", "dominant_persist",
            "counter_persist", "qualified", "net_r", "why", "mfe_r", "give_back_r",
        ],
        entry_quality,
    )

    # Cost/stop model from the actual ledger.
    risk_pct = statistics.median(
        [abs(t["entry"] - t["stop0"]) / t["entry"] * 100 for t in audit["trades"]]
    )
    cost_r = statistics.median(
        [t["costs_rs"] / t["risk_rs"] for t in audit["trades"]]
    )
    winner_ratios = [
        t["net_r"] / t["mfe_r"]
        for t in audit["trades"]
        if t["mfe_r"] >= 1.0 and t["net_r"] > 0
    ]
    worst_winner_trail_ratio = min(winner_ratios)
    all_ledger_gross_to_mfe = sum(t["gross_r"] for t in audit["trades"]) / sum(
        t["mfe_r"] for t in audit["trades"]
    )

    def topn(sec, bar, n=2):
        rows = []
        for (name, s), d in det_by_key.items():
            if s != sec:
                continue
            rank = d["rank"].get(bar)
            pos = d["pos"].get(bar)
            mv = d["mv"].get(bar)
            if rank is not None and mv is not None:
                rows.append((rank, pos or 999, name, d))
        rows.sort(key=lambda z: (-z[0], z[1], z[2]))
        return rows[:n]

    def to_min(bar):
        h, m = map(int, bar.split(":"))
        return h * 60 + m

    # Proposed second-book screen. Selection uses only the current bar's
    # timeline + board rank; forward path is used only to score the outcome.
    used = set()
    per_sector = Counter()
    last_sector = {}
    counterfactual = []
    for t in audit["timeline"]:
        bar = t["bar"]
        if bar > "14:30":
            continue
        for sec, sv in t["sectors"].items():
            bull, bear = sv["persist_bull"], sv["persist_bear"]
            persist, counter = max(bull, bear), min(bull, bear)
            side = 1 if bull >= bear else -1
            if not (sv["qualified"] and persist >= 90 and counter <= 10):
                continue
            if sec in last_sector and to_min(bar) - last_sector[sec] < 45:
                continue
            for rank, pos, name, d in topn(sec, bar, 2):
                if rank < 55 or pos > 2 or name in used or per_sector[sec] >= 2 or len(counterfactual) >= 6:
                    continue
                i = bars.index(bar)
                mv0 = d["mv"][bar]
                path = [
                    side * (d["mv"][b] - mv0)
                    for b in bars[i:]
                    if d["mv"][b] is not None
                ]
                close_pp = path[-1]
                mfe_pp = max(path)
                mae_pp = min(path)
                stopped = mae_pp <= -risk_pct
                close_r = close_pp / risk_pct
                mfe_r = mfe_pp / risk_pct
                mae_r = mae_pp / risk_pct
                hold_net_r = (-1.0 - cost_r) if stopped else (close_r - cost_r)
                winner_trail_r = (-1.0 - cost_r) if stopped else worst_winner_trail_ratio * mfe_r
                ledger_trail_r = (-1.0 - cost_r) if stopped else (all_ledger_gross_to_mfe * mfe_r - cost_r)
                counterfactual.append(
                    {
                        "bar": bar,
                        "sector": sec,
                        "name": name,
                        "side": side,
                        "board_rank": rank,
                        "board_pos": pos,
                        "dominant_persist": persist,
                        "counter_persist": counter,
                        "pick": t["pick"],
                        "mv0": mv0,
                        "final_mv": d["mv"][bars[-1]],
                        "close_pp": close_pp,
                        "close_r": close_r,
                        "hold_net_r": hold_net_r,
                        "mfe_r": mfe_r,
                        "mae_r": mae_r,
                        "stopped": int(stopped),
                        "winner_trail_proxy_r": winner_trail_r,
                        "all_ledger_trail_proxy_r": ledger_trail_r,
                    }
                )
                used.add(name)
                per_sector[sec] += 1
                last_sector[sec] = to_min(bar)
    write_csv(
        ROOT / "second_book_counterfactual.csv",
        [
            "bar", "sector", "name", "side", "board_rank", "board_pos",
            "dominant_persist", "counter_persist", "pick", "mv0", "final_mv",
            "close_pp", "close_r", "hold_net_r", "mfe_r", "mae_r", "stopped",
            "winner_trail_proxy_r", "all_ledger_trail_proxy_r",
        ],
        counterfactual,
    )

    (ROOT / "rules.json").write_text(json.dumps(audit["rules"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (ROOT / "diagnostics.json").write_text(json.dumps(audit["diagnostics"], indent=2, sort_keys=True) + "\n", encoding="utf-8")

    hashes = {}
    for p in INPUT_FILES:
        hashes[p.name] = {
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }

    rules_counter = Counter(x["rule"] for x in audit["blocked"])
    final_refusal_counter = Counter((x["blocked"] or "none") for x in summary)
    top2 = [r for r in entry_quality if (r["board_pos"] or 99) <= 2]
    pos3 = [r for r in entry_quality if r["board_pos"] == 3]
    pos4plus = [r for r in entry_quality if (r["board_pos"] or 99) >= 4]
    earlier_final = [r for r in summary if r["blocked"] == "earlier"]

    metrics = {
        "input_files": hashes,
        "board_meta": audit["board"]["meta"],
        "audit_counts": audit["counts"],
        "books": audit["books"],
        "costs": audit["costs"],
        "market": audit["market"],
        "scan": audit["scan"],
        "pick_counts": dict(Counter(t["pick"] for t in audit["timeline"])),
        "sector_gate_pick_entries": {
            k: {
                "side": v["side"],
                "day_ret": v["day_ret"],
                "gate_candles": v["gate_candles"],
                "pick_candles": v["pick_candles"],
                "entries": v["entries"],
                "above_70": v["above_70"],
                "above_85": v["above_85"],
            }
            for k, v in audit["sectors"].items()
        },
        "blocked_rule_counts": dict(rules_counter),
        "board_final_refusal_counts": dict(final_refusal_counter),
        "cancel_why": audit["cancel_why"],
        "leadership_scanned_by_sector": dict(
            Counter(x["sector"] for x in audit["leadership"] if x["scanned"])
        ),
        "leadership_rows_by_sector": dict(Counter(x["sector"] for x in audit["leadership"])),
        "big_winners": {
            "threshold_pp": 3.0,
            "unique_names": len(unique),
            "count": len(big),
            "entered": sum(bool(r["entered"]) for r in big),
            "missed": sum(not r["entered"] for r in big),
            "missed_blocked": dict(Counter((r["blocked"] or "none") for r in big if not r["entered"])),
            "sectors": dict(Counter(r["sector"] for r in big)),
            "top10": [
                {k: r[k] for k in ("name", "sector", "last_mv", "directional_last_pct", "entered", "net_r", "blocked", "top5", "best_rank", "rank_avg")}
                for r in big[:10]
            ],
        },
        "earlier_final_refusals": [
            {
                "name": r["name"], "sector": r["sector"], "last_mv": r["last_mv"],
                "directional_last_pct": final_side[r["sector"]] * r["last_mv"],
                "top5": r["top5"], "best_rank": r["best_rank"], "rank_avg": r["rank_avg"],
            }
            for r in earlier_final
        ],
        "entry_quality": {
            "n": len(entry_quality),
            "top2": {"n": len(top2), "net_r": sum(r["net_r"] for r in top2)},
            "pos3": {"n": len(pos3), "net_r": sum(r["net_r"] for r in pos3)},
            "pos4plus": {"n": len(pos4plus), "net_r": sum(r["net_r"] for r in pos4plus)},
            "all_net_r": sum(r["net_r"] for r in entry_quality),
        },
        "exit_engine": {
            "ema9_exits": sum(t["why"] == "EMA9 EXIT" for t in audit["trades"]),
            "sl_hits": sum(t["why"] == "SL HIT" for t in audit["trades"]),
            "mfe_r_sum": sum(t["mfe_r"] for t in audit["trades"]),
            "give_back_r_sum": sum(t["give_back_r"] for t in audit["trades"]),
            "ema9_give_back_r_sum": sum(t["give_back_r"] for t in audit["trades"] if t["why"] == "EMA9 EXIT"),
            "gross_r_sum": sum(t["gross_r"] for t in audit["trades"]),
            "cost_r_sum": sum(t["costs_rs"] / t["risk_rs"] for t in audit["trades"]),
            "net_r_sum": sum(t["net_r"] for t in audit["trades"]),
            "costs_pct_of_gross": audit["books"]["costs_rs"] / audit["books"]["gross_rs"],
        },
        "counterfactual": {
            "risk_pct": risk_pct,
            "cost_r": cost_r,
            "worst_winner_trail_ratio": worst_winner_trail_ratio,
            "all_ledger_gross_to_mfe": all_ledger_gross_to_mfe,
            "n": len(counterfactual),
            "stops": sum(r["stopped"] for r in counterfactual),
            "close_r_sum": sum(r["close_r"] for r in counterfactual),
            "hold_net_r_sum": sum(r["hold_net_r"] for r in counterfactual),
            "mfe_r_sum": sum(r["mfe_r"] for r in counterfactual),
            "winner_trail_proxy_r_sum": sum(r["winner_trail_proxy_r"] for r in counterfactual),
            "all_ledger_trail_proxy_r_sum": sum(r["all_ledger_trail_proxy_r"] for r in counterfactual),
            "trades": counterfactual,
        },
        "checks": {
            "board_name_bar_rows": len(name_bar_rows),
            "missing_mv_cells": sum(1 for r in name_bar_rows if r["mv"] is None),
            "html_pick_cells": sum(sum(r["picked"].values()) for r in sector_rows),
            "top5_marker_mismatches": len(mismatches),
        },
    }
    (ROOT / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "outputs": sorted(p.name for p in ROOT.iterdir() if p.is_file()),
        "big_winners": metrics["big_winners"],
        "entry_quality": metrics["entry_quality"],
        "counterfactual": {k: v for k, v in metrics["counterfactual"].items() if k != "trades"},
        "checks": metrics["checks"],
    }, indent=2))


if __name__ == "__main__":
    main()

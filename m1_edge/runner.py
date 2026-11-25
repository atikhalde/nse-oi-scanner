#!/usr/bin/env python3
"""CLI for paper replay / point-in-time forward testing of the M1E model.

Examples:
  python -m m1_edge.runner --audit-json results/scan1m_2026-11-25/inputs/audit-2026-11-25-1m.json --replay --print-alerts
  python -m m1_edge.runner --board-json board_payload.json --final --print-alerts
  python -m m1_edge.runner --board-jsonl board_payloads.jsonl --replay
  python -m m1_edge.runner --finalize --print-alerts

This CLI never calls the live scanner or Telegram. ``--print-alerts`` uses a
local stdout sender so the distinct M1E cards are visible during replay.
"""
from __future__ import annotations

import argparse
import csv as csvmod
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .model import Config, evaluate_if_enabled, finalize_if_enabled

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent


def _load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _num(x: Any) -> float | None:
    if x is None or str(x).strip() == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _csv_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csvmod.DictReader(f))


def _print_sender(text: str, *, dry_run: bool = False) -> bool:
    print("\n" + text, file=sys.stderr)
    return True


def _fixture_payloads(audit_path: str, session: str) -> list[dict]:
    """Build board.sweep-shaped payloads from the durable 2026-11-25 fixture.

    Live forward testing should pass ``board.sweep``'s payload directly. This
    reconstruction exists only to regression-test the rules against the audit.
    """
    audit = _load_json(audit_path)
    here = Path(audit_path).resolve().parent
    # The durable report keeps raw inputs under inputs/ and parsed CSVs one
    # level up; a standalone audit JSON may sit next to its CSVs.
    candidates = [here, here.parent]
    name_csv = next((p / "board_name_bars.csv" for p in candidates
                     if (p / "board_name_bars.csv").exists()), None)
    sector_csv = next((p / "board_sector_bars.csv" for p in candidates
                       if (p / "board_sector_bars.csv").exists()), None)
    if name_csv is None or sector_csv is None:
        raise SystemExit(f"fixture CSVs missing next to {audit_path}: run analyze.py first")

    name_bars = _csv_rows(name_csv)
    sector_bars = _csv_rows(sector_csv)
    if len(name_bars) != 13680 or len(sector_bars) != 1080:
        raise SystemExit("unexpected fixture row counts; refusing silent replay")

    by_bar: dict[str, dict] = {}
    for row in sector_bars:
        bar, sec = row["bar"], row["sector"]
        by_bar.setdefault(bar, {"b": bar, "session": session, "pick": row.get("pick") or "",
                                "market": row.get("market") or "", "sectors": {}})
        by_bar[bar]["sectors"][sec] = {
            "bull": _num(row.get("persist_bull")),
            "bear": _num(row.get("persist_bear")),
            "side": int(_num(row.get("dominant_side")) or 1),
            "gate": int(_num(row.get("qualified")) or 0),
            "qualified": int(_num(row.get("qualified")) or 0),
            "day": _num(row.get("day_pct")),
            "intra": _num(row.get("intra_pct")),
            "n": [], "rank": [], "rk": [], "mv": [], "s": [], "t": [],
        }
    # HTML board rank/pos/mv are point-in-time; the model gates rank>=55 and pos<=2.
    for row in name_bars:
        bar, sec = row["bar"], row["sector"]
        cell = (by_bar.get(bar) or {}).get("sectors", {}).get(sec)
        if cell is None:
            continue
        cell["n"].append(row["name"])
        cell["rank"].append(_num(row.get("rank")) or -1.0)
        cell["rk"].append(int(_num(row.get("pos")) or len(cell["n"])))
        cell["mv"].append(_num(row.get("mv")))
    payloads = [by_bar[bar] for bar in sorted(by_bar)]
    # Audit-level sanity checks: one 5m board candle per timeline row.
    if len(payloads) != len(audit.get("timeline") or []):
        raise SystemExit("fixture timeline/board length mismatch")
    return payloads


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csvmod.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M1E paper second-book model")
    ap.add_argument("--audit-json", help="Raw 1m audit JSON for fixture replay")
    ap.add_argument("--board-json", help="One board.sweep payload JSON")
    ap.add_argument("--board-jsonl", help="Newline-delimited board.sweep payloads")
    ap.add_argument("--replay", action="store_true",
                    help="Use an isolated replay state and close open paper trades at the last bar")
    ap.add_argument("--final", action="store_true",
                    help="Close open paper trades after the supplied payload(s)")
    ap.add_argument("--finalize", action="store_true",
                    help="Only close open paper trades at EOD (no payloads)")
    ap.add_argument("--print-alerts", action="store_true",
                    help="Print the distinct M1E bot-3 cards locally instead of Telegram")
    ap.add_argument("--session", help="IST session date YYYY-MM-DD")
    ap.add_argument("--config-out", help="Write effective config JSON")
    ap.add_argument("--entries-out", help="Write final entries CSV")
    ap.add_argument("--state-path", help="Override M1E state path")
    ap.add_argument("--journal-dir", help="Override M1E journal directory")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    explicit_state = bool(args.state_path or any(
        k in env for k in ("M1E_STATE_PATH", "M1_EDGE_STATE_PATH")))
    if args.state_path:
        env["M1E_STATE_PATH"] = args.state_path
    if args.journal_dir:
        env["M1E_JOURNAL_DIR"] = args.journal_dir
    if args.replay:
        env["M1E_ENABLED"] = "1"
        env["M1E_MODE"] = "paper"
        # Replay writes a dedicated state/journal by default, never a live book.
        env.setdefault("M1E_STATE_PATH", str(ROOT / "results" / "m1_edge_replay" / "state.json"))
        env.setdefault("M1E_JOURNAL_DIR", str(ROOT / "results" / "m1_edge_replay"))
    cfg = Config.from_env(env)
    if args.audit_json and not args.session:
        session = str(_load_json(args.audit_json).get("session") or datetime.now(IST).date().isoformat())
    else:
        session = args.session or datetime.now(IST).date().isoformat()
    if args.replay and not explicit_state:
        # A replay must be point-in-time from bar zero, not appended to the
        # previous replay. Only the dedicated default replay state is removed.
        Path(cfg.state_path).unlink(missing_ok=True)
    if args.config_out:
        Path(args.config_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.config_out).write_text(json.dumps(cfg.public(), indent=2), encoding="utf-8")

    sender = _print_sender if args.print_alerts else None
    dry_run = True if args.print_alerts else None

    if args.finalize:
        out = finalize_if_enabled(session=session, env=env, send=sender, dry_run=dry_run)
        print(json.dumps(out, indent=2, default=str))
        return 0

    payloads: list[dict] = []
    if args.audit_json:
        payloads = _fixture_payloads(args.audit_json, session)
    elif args.board_json:
        payloads = [_load_json(args.board_json)]
    elif args.board_jsonl:
        payloads = [json.loads(line) for line in
                    Path(args.board_jsonl).read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        ap.error("provide --audit-json, --board-json, --board-jsonl, or --finalize")
    if not payloads:
        ap.error("no payloads to process")

    close_after = bool(args.final or args.replay)
    results = []
    for i, payload in enumerate(payloads):
        result = evaluate_if_enabled(payload, session=session,
                                     final=close_after and i == len(payloads) - 1,
                                     env=env, send=sender, dry_run=dry_run)
        results.append(result)

    state_path = Path(cfg.state_path)
    state = _load_json(state_path) if state_path.exists() else {}
    if args.entries_out:
        _write_csv(Path(args.entries_out), state.get("entries") or [])
    out = {
        "bars": len(payloads),
        "enabled": cfg.enabled,
        "mode": cfg.mode,
        "state_path": str(state_path),
        "config": cfg.public(),
        "summary": state.get("stats") or {},
        "entries": state.get("entries") or [],
        "new_entries": sum(len(r.get("new_entries") or []) for r in results),
        "closed_events": sum(len(r.get("closed") or []) for r in results),
        "alerts_delivered": sum(1 for r in results for a in r.get("alerts") or []
                                if a.get("delivered")),
        "alerts_pending": results[-1].get("alerts_pending", 0),
        "alert_delivery_failed": any(r.get("alert_delivery_failed") for r in results),
        "last_result": results[-1],
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

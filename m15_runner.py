#!/usr/bin/env python3
"""M15 — PERSIST + MASTER live entry model (paper-only).

Port of Persist-check nse_scanner (175b868) into nse-oi-scanner as a
standalone paper model.

Flow:
  1. Market brain (NIFTY Institutional Core + Flow v4) → strongest
     PERSIST +/- sector that complements Nifty direction (BULL/BEAR).
     CHOP/NEUTRAL/VIX-spike → WAIT (no stock scan).
  2. Stock 5m master scanner (MASTER SECTOR BATCH SCANNER) runs ONLY on
     that sector's full batch (no 12-symbol TV cap) — aggressive windows
     + 22-condition EX codes. Only newly-fired entries from the latest
     completed 5m candle are eligible for Telegram.
  3. Risk: entry = candle close, SL = EMA9 ±0.02% (rounded outward to
     0.01), T1/T2/T3 at 1/2/3 R. One alert per symbol per IST session.
     EMA9 trailing exit on first completed close beyond EMA9.

Data feed: the SAME pipeline M1–M6 use (feeds.py via live_runner/fast_feed) —
Yahoo chart JSON primary, Dhan fallback, committed data/history/*.csv for
daily context. nse_scanner/data.py no longer calls yfinance.

Paper: state/state15.json (== state15.json root alias for legacy tooling).
The cycle's feed audit (Yahoo/Dhan/history source mix) is stored as st["feed"].
No broker orders — alerts only + EOD paper report.
Usage: python -u m15_runner.py [--loop 1] [--dry-run] [--test-alert] [--sector IT] [--backfill]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_PRIMARY = ROOT / "state" / "scan_state.json"          # nse_scanner default
STATE15 = ROOT / "state15.json"                               # legacy root alias (M-style)
STATE15_ALT = ROOT / "state" / "state15.json"                 # preferred new location
IST = __import__("zoneinfo").ZoneInfo("Asia/Kolkata") if hasattr(__import__("zoneinfo"), "ZoneInfo") else dt.timezone(dt.timedelta(hours=5, minutes=30))

# Patch nse_scanner.state to use M15 isolated state file BEFORE importing pipeline
import nse_scanner.state as m15_state
# Prefer state/state15.json, fall back to legacy state15.json for migration
if STATE15_ALT.exists() or not STATE_PRIMARY.exists():
    # Use dedicated M15 state so it doesn't collide with standalone persist-check state
    # but keep compatibility: pipeline will read/write via this path
    m15_state.STATE_PATH = STATE15_ALT
else:
    # If an existing scan_state.json was already primed by live nse_scanner, reuse it
    # and keep alias synced
    m15_state.STATE_PATH = STATE_PRIMARY

# Ensure state dir exists
m15_state.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

from nse_scanner.pipeline import run_once, run_until_ready, format_market, format_stock, format_trail_exit
from nse_scanner import telegram as tg
from nse_scanner.session import now_ist
import nse_scanner.data as m15_data


def _fanout_targets() -> list[tuple[str, str]]:
    """Extra Telegram targets for M15 — supports multi-bot mirroring.

    Env:
      TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID          — primary (required)
      TELEGRAM_BOT_TOKEN_2 / TELEGRAM_CHAT_ID_2      — secondary mirror (persist-check default)
      M15_BOT_TOKEN_A / M15_CHAT_ID_A                — M15-specific A
      M15_BOT_TOKEN_B / M15_CHAT_ID_B                — M15-specific B
      M14/M13/M11 fallbacks for A/B (reuse existing vault pairs)
    """
    out: list[tuple[str, str]] = []
    primary = (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip())
    # secondary generic
    tok2 = os.environ.get("TELEGRAM_BOT_TOKEN_2", "").strip()
    chat2 = os.environ.get("TELEGRAM_CHAT_ID_2", "").strip()
    if tok2 and chat2 and (tok2, chat2) != primary and (tok2, chat2) not in out:
        out.append((tok2, chat2))
    for suffix in ("A", "B"):
        tok = (
            os.environ.get(f"M15_BOT_TOKEN_{suffix}", "").strip()
            or os.environ.get(f"M14_BOT_TOKEN_{suffix}", "").strip()
            or os.environ.get(f"M13_BOT_TOKEN_{suffix}", "").strip()
            or os.environ.get(f"M11_BOT_TOKEN_{suffix}", "").strip()
        )
        chat = (
            os.environ.get(f"M15_CHAT_ID_{suffix}", "").strip()
            or os.environ.get(f"M14_CHAT_ID_{suffix}", "").strip()
            or os.environ.get(f"M13_CHAT_ID_{suffix}", "").strip()
            or os.environ.get(f"M11_CHAT_ID_{suffix}", "").strip()
        )
        if tok and chat and (tok, chat) != primary and (tok, chat) not in out:
            out.append((tok, chat))
    return out


def _fanout_send(text: str, dry_run: bool = False) -> int:
    """Send via primary (via nse_scanner.telegram) + fanout to extra targets. Returns successful sends."""
    sent = 0
    # Primary via existing module (handles dry-run internally)
    if tg.send(text, dry_run=dry_run):
        sent += 1
    else:
        # If primary fails, don't count secondary as success-of-primary path
        # but still attempt fanout for visibility
        pass
    # Secondary fanout — direct POST per target (don't use tg.send which re-reads env)
    extra = _fanout_targets()
    if dry_run:
        for tok, chat in extra:
            print(f"[DRY-RUN fanout] -> {chat}: {text[:120]}...")
            sent += 1
        return sent
    for tok, chat in extra:
        try:
            import requests
            url = f"https://api.telegram.org/bot{tok}/sendMessage"
            payload = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            r = requests.post(url, json=payload, timeout=12)
            if r.ok:
                sent += 1
            else:
                print(f"M15 fanout FAIL {chat}: {r.status_code} {r.text[:160]}")
        except Exception as e:
            print(f"M15 fanout EXC {chat}: {type(e).__name__}: {e}")
    return sent


def _load_state(today: str) -> dict:
    try:
        st = m15_state.load()
        # Ensure session reset if new IST day (mimic pipeline reset_if_new_day)
        st = m15_state.reset_if_new_day(st, today)
        return st
    except m15_state.InvalidStateError as e:
        print(f"M15 state corrupt: {e} — failing closed (no alerts).")
        raise
    except Exception as e:
        print(f"M15 state load warning: {e}")
        return {"session": today, "market": {}, "sector": {}, "alerts": {}, "sent": {}, "positions": {}}


def _save_state_alias(st: dict) -> None:
    """Persist to primary path + keep legacy alias synced."""
    m15_state.save(st)
    # Mirror to legacy root file for tooling that expects state15.json at root
    try:
        if m15_state.STATE_PATH != STATE15:
            STATE15.write_text(json.dumps(st, indent=2), encoding="utf-8")
        # Also mirror to state/state15.json if primary is scan_state.json
        if m15_state.STATE_PATH == STATE_PRIMARY and STATE15_ALT != STATE_PRIMARY:
            STATE15_ALT.parent.mkdir(parents=True, exist_ok=True)
            STATE15_ALT.write_text(json.dumps(st, indent=2), encoding="utf-8")
    except Exception:
        pass


def run_cycle(*, dry_run: bool = False, force_sector: str | None = None, backfill: bool = False, wait_for_bar: bool = False) -> dict:
    """One M15 cycle — thin wrapper around nse_scanner pipeline with fanout."""
    if dry_run:
        os.environ["DRY_RUN"] = "1"
    # Patch telegram.send fanout: monkey-patch nse_scanner.pipeline.telegram.send to use fanout
    # Instead of patching globally, we post-process: pipeline already calls tg.send;
    # we interpose by temporarily replacing tg.send with fanout version.
    orig_send = tg.send
    def fanout_send_wrapper(text: str, dry_run: bool = False, parse_mode: str = "HTML") -> bool:
        # Use our fanout that respects dry_run
        n = _fanout_send(text, dry_run=dry_run)
        return n > 0
    # Apply patch
    tg.send = fanout_send_wrapper
    try:
        if wait_for_bar:
            out = run_until_ready(force_sector=force_sector)
        else:
            out = run_once(force_sector=force_sector, backfill=backfill)
    finally:
        tg.send = orig_send
    # Feed audit for early-exit paths too (pipeline already attaches it on a
    # full scan; WAIT/STALE/CLOSED returns get the per-cycle counters here).
    out.setdefault("feed", m15_data.feed_stats())
    # Sync alias after pipeline's internal saves
    try:
        st = m15_state.load()
        _save_state_alias(st)
    except Exception:
        pass
    return out


def test_alert() -> int:
    """Send sample M15 alerts to all configured chats to verify fanout."""
    n1 = _fanout_send("🧪 M15 TEST — PERSIST+MASTER fanout is connected. No position was opened.", dry_run=False)
    print(f"M15 test alert delivered to {n1} target(s)")
    # Also test market/stock formatting with dummy data
    try:
        from nse_scanner.market import MarketDecision
        # Minimal dummy market message
        dummy_md_text = (
            "<b>MARKET BULL</b>  conf 74% STRONG\n"
            "Nifty 24810.0  day +0.42%  intra +0.31%\n"
            "VIX 12.80 ↓ -3.10%   ADX 26   MTF 3/3\n"
            "Breadth 11▲/4▼  TREND UP\n"
            "PERSIST +  IT 78 · PHA 71 · FMCG 64\n"
            "PERSIST −  AUTO 70 · MET 66 · MEDIA 61\n"
            "Live flow  BULL Q (68)\n"
            "\n<b>SECTOR PICK: IT</b>  BULL\n"
            "PERSIST + IT qualified (score 78)\n"
            "Action: <b>SCAN LONG</b>\n"
            "<i>2026-11-09 10:30 IST</i>"
        )
        n2 = _fanout_send(dummy_md_text)
        print(f"M15 dummy market alert to {n2} target(s)")
    except Exception as e:
        print(f"M15 dummy market skip: {e}")
    return n1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="M15 PERSIST+MASTER paper runner")
    ap.add_argument("--loop", type=int, default=1, help="number of cycles")
    ap.add_argument("--dry-run", action="store_true", help="print without Telegram")
    ap.add_argument("--test-alert", action="store_true", help="send test alerts to all chats")
    ap.add_argument("--sector", default=None, help="force sector code (AUTO, IT, BANK, ...)")
    ap.add_argument("--backfill", action="store_true", help="diagnostic last-session scan; requires --dry-run")
    ap.add_argument("--wait-for-bar", action="store_true", help="poll Yahoo until closed 5m bar arrives (max 180s)")
    a = ap.parse_args()

    if a.test_alert:
        test_alert()
        sys.exit(0)
    if a.backfill and not a.dry_run:
        ap.error("--backfill requires --dry-run; old signals must never reach Telegram")

    for i in range(max(1, a.loop)):
        try:
            out = run_cycle(dry_run=a.dry_run, force_sector=a.sector, backfill=a.backfill, wait_for_bar=a.wait_for_bar)
            print(f"M15 cycle {i+1}/{a.loop}: bar={out.get('bar')} market={out.get('market')} pick={out.get('pick')} action={out.get('action')} signals={len(out.get('signals', []))}")
            f = out.get("feed") or {}
            print(f"  FEED yahoo={f.get('yahoo_calls')} dhan={f.get('dhan_calls')} "
                  f"history={f.get('history_calls')} sources={f.get('sources')} fallback={f.get('fallback')}")
            for s in out.get("signals", []):
                print(f"  ALERT {s.get('name')} {s.get('symbol')} {s.get('side')} {s.get('price')} @ {s.get('time')}")
            if out.get("pending"):
                print(f"  PENDING: {out['pending']}")
        except m15_state.InvalidStateError:
            print("M15 abort: corrupt state — refusing to send (fail closed).")
            sys.exit(1)
        except Exception as e:
            print(f"M15 cycle ERROR: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
        if i < a.loop - 1:
            print(f"--- M15 loop: cycle {i+2} of {a.loop} in ~300s ---")
            time.sleep(300)

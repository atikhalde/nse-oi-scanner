"""M1E — opt-in, paper-only, alerting second-book model for the 1m scan.

This module is intentionally standalone: no pandas, no feed calls, and no import
of the scanner's existing state. It can be dropped into the real 1m runtime as
``nse_scanner/edge_second_book.py`` or imported here as ``m1_edge.model``.

The original 1m scan is unchanged unless ``M1E_ENABLED=1`` is explicitly set.
When enabled, the model:

* writes only its own state and JSONL journal;
* sends its own clearly labelled ``[M1E]`` paper cards to the **same 1m bot**
  through an injected ``telegram.send_bot3`` sender;
* never sends an original entry, opens a position, or mutates
  ``state/scan1m_state.json`` / ``state/scan_state.json`` /
  ``state/screen_state.json`` / ``sent`` / ``positions``.

There is no live-order mode. ``M1E_MODE`` must be ``paper`` (the old ``shadow``
value is accepted as a legacy alias, but it is no longer silent).

Rule (from the 2026-11-25 1m audit):
  * sector gate open, dominant persistence >= 90, opposite persistence <= 10;
  * take the sector's top two board-position names with composite rank >= 55;
  * rebase levels to the current board candle (no stale fire levels);
  * at most 2 names/sector, 6/day, 45-minute cooldown, no new entries after 14:30;
  * paper exit at -1R stop, optional +TARGET_R, otherwise EOD;
  * score with the audit's median risk 0.7178% and cost 0.1401R/trade.

Forward testing uses the same 5m board payload the 1m scanner already builds
in ``nse_scanner.board.sweep``; no extra feed calls are needed.
"""
from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA = "m1-edge-second-book-v2"
IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = ROOT / "state" / "edge_second_book_state.json"
DEFAULT_JOURNAL_DIR = ROOT / "reports"

# Exact calibration from results/scan1m_2026-11-25/metrics.json.
AUDIT_MEDIAN_RISK_PCT = 0.7178237971021002
AUDIT_MEDIAN_COST_R = 0.14013902190524707


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _f(value: Any, default: float) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int) -> int:
    return int(_f(value, float(default)))


def _env(env: dict[str, str], suffix: str, default: str | None = None) -> str | None:
    """Accept M1E_* (runtime) and M1_EDGE_* (this repo's CLI alias)."""
    for prefix in ("M1E_", "M1_EDGE_"):
        key = prefix + suffix
        if key in env:
            return env[key]
    return default


def _hhmm(value: str) -> int:
    h, m = str(value).split(":", 1)
    return int(h) * 60 + int(m)


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(value)


def _ist_today() -> str:
    return datetime.now(IST).date().isoformat()


def _esc(value: Any) -> str:
    """Escape dynamic text because telegram.send_bot3 defaults to HTML."""
    return html.escape(str(value if value is not None else ""), quote=False)


def _side_text(side: Any) -> str:
    return "BUY" if int(side or 1) == 1 else "SELL"


def _signed(value: Any, nd: int = 3) -> str:
    try:
        return f"{float(value):+.{nd}f}"
    except (TypeError, ValueError):
        return "+0.000"


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    mode: str = "paper"
    alert: bool = True
    alert_prefix: str = "M1E · PAPER SECOND BOOK"
    alert_exits: bool = True
    alert_eod: bool = True
    min_persist: float = 90.0
    max_counter: float = 10.0
    min_rank: float = 55.0
    top_n: int = 2
    max_per_sector: int = 2
    max_day: int = 6
    cooldown_min: int = 45
    last_entry: str = "14:30"
    risk_pct: float = AUDIT_MEDIAN_RISK_PCT
    cost_r: float = AUDIT_MEDIAN_COST_R
    target_r: float = 0.0
    require_gate: bool = True
    min_strength: float = 0.0
    min_structure: float = 0.0
    state_path: Path = DEFAULT_STATE_PATH
    journal_dir: Path = DEFAULT_JOURNAL_DIR

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        enabled = _truthy(_env(env, "ENABLED", "0"))
        mode = str(_env(env, "MODE", "paper") or "paper").strip().lower()
        # Backwards compatibility with the earlier silent prototype. It is not
        # silent any more: alerts default ON and the mode is normalised.
        if mode == "shadow":
            mode = "paper"
        # Hard safety rail: this model has no live-order path in this iteration.
        if enabled and mode != "paper":
            raise ValueError("M1E_MODE must be 'paper'; no live/order mode is implemented")
        return cls(
            enabled=enabled,
            mode=mode,
            alert=_truthy(_env(env, "ALERT", "1")),
            alert_prefix=str(_env(env, "ALERT_PREFIX", "M1E · PAPER SECOND BOOK")
                             or "M1E · PAPER SECOND BOOK"),
            alert_exits=_truthy(_env(env, "ALERT_EXITS", "1")),
            alert_eod=_truthy(_env(env, "ALERT_EOD", "1")),
            min_persist=_f(_env(env, "MIN_PERSIST", "90"), 90.0),
            max_counter=_f(_env(env, "MAX_COUNTER", "10"), 10.0),
            min_rank=_f(_env(env, "MIN_RANK", "55"), 55.0),
            top_n=_i(_env(env, "TOP_N", "2"), 2),
            max_per_sector=_i(_env(env, "MAX_PER_SECTOR", "2"), 2),
            max_day=_i(_env(env, "MAX_DAY", "6"), 6),
            cooldown_min=_i(_env(env, "COOLDOWN_MIN", "45"), 45),
            last_entry=str(_env(env, "LAST_ENTRY", "14:30") or "14:30"),
            risk_pct=_f(_env(env, "RISK_PCT", str(AUDIT_MEDIAN_RISK_PCT)), AUDIT_MEDIAN_RISK_PCT),
            cost_r=_f(_env(env, "COST_R", str(AUDIT_MEDIAN_COST_R)), AUDIT_MEDIAN_COST_R),
            target_r=_f(_env(env, "TARGET_R", "0"), 0.0),
            require_gate=_truthy(_env(env, "REQUIRE_GATE", "1")),
            min_strength=_f(_env(env, "MIN_STRENGTH", "0"), 0.0),
            min_structure=_f(_env(env, "MIN_STRUCTURE", "0"), 0.0),
            state_path=Path(_env(env, "STATE_PATH", str(DEFAULT_STATE_PATH)) or DEFAULT_STATE_PATH),
            journal_dir=Path(_env(env, "JOURNAL_DIR", str(DEFAULT_JOURNAL_DIR)) or DEFAULT_JOURNAL_DIR),
        )

    def public(self) -> dict:
        d = asdict(self)
        d["state_path"] = str(self.state_path)
        d["journal_dir"] = str(self.journal_dir)
        d["schema"] = SCHEMA
        return d


@dataclass
class Leader:
    name: str
    sector: str
    side: int
    rank: float = -1.0
    pos: int = 999
    # Directional close-to-session-open move, positive in the sector's dynamic
    # side. board.sweep's `mv` is raw close-to-open; from_board_payload converts
    # it as `side * mv`.
    mv: float | None = None
    raw_mv: float | None = None
    price: float | None = None
    ema9: float | None = None
    strength: float | None = None
    structure: float | None = None


@dataclass
class Sector:
    name: str
    side: int
    persist: float
    counter: float
    qualified: bool
    day_pct: float | None = None
    intra_pct: float | None = None
    main_pick: bool = False


@dataclass
class Snapshot:
    session: str
    bar: str
    main_pick: str = ""
    market: str = ""
    sectors: dict[str, Sector] = field(default_factory=dict)
    leaders: list[Leader] = field(default_factory=list)
    final: bool = False
    source: str = "board_payload"


def _num(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _sector_from_cell(name: str, cell: dict, main_pick: str) -> Sector:
    bull = _num(cell.get("bull", cell.get("persist_bull")))
    bear = _num(cell.get("bear", cell.get("persist_bear")))
    persist = _num(cell.get("persist", cell.get("dominant_persist"))) or 0.0
    counter = _num(cell.get("counter_persist", cell.get("counter")))
    if bull is not None and bear is not None:
        side = 1 if bull >= bear else -1
        persist = max(bull, bear)
        counter = min(bull, bear)
    else:
        side = int(_num(cell.get("side", cell.get("dominant_side"))) or 1)
        if counter is None:
            # Missing counter evidence must fail the gate, not silently pass.
            counter = 999.0
    gate = cell.get("gate")
    if gate is None:
        gate = cell.get("qualified", False)
    return Sector(
        name=name,
        side=side,
        persist=float(persist),
        counter=float(counter),
        qualified=_bool(gate),
        day_pct=_num(cell.get("day", cell.get("day_pct"))),
        intra_pct=_num(cell.get("intra", cell.get("intra_pct"))),
        main_pick=(main_pick == name),
    )


def from_board_payload(payload: dict | None, *, session: str | None = None,
                       final: bool = False) -> Snapshot:
    """Convert ``nse_scanner.board.sweep``'s payload into the model snapshot."""
    payload = payload or {}
    bar = str(payload.get("b") or payload.get("bar") or payload.get("scan") or "")
    sess = str(session or payload.get("session") or payload.get("date") or _ist_today())
    main_pick = str(payload.get("pick") or "")
    sectors: dict[str, Sector] = {}
    leaders: list[Leader] = []
    for sec, cell in (payload.get("sectors") or {}).items():
        if not isinstance(cell, dict):
            continue
        sectors[sec] = _sector_from_cell(sec, cell, main_pick)
        names = cell.get("n") or []
        ranks = cell.get("rank") or cell.get("score") or []
        positions = cell.get("rk") or list(range(1, len(names) + 1))
        mvs = cell.get("mv") or []
        prices = cell.get("price") or []
        emas = cell.get("ema9") or []
        strengths = cell.get("s") or []
        structures = cell.get("t") or []
        side = sectors[sec].side
        for idx, raw in enumerate(names):
            mv_raw = _num(mvs[idx] if idx < len(mvs) else None)
            mv_directional = None if mv_raw is None else side * mv_raw
            leaders.append(Leader(
                name=str(raw),
                sector=sec,
                side=side,
                rank=_num(ranks[idx] if idx < len(ranks) else None) or -1.0,
                pos=int(_num(positions[idx] if idx < len(positions) else None) or (idx + 1)),
                mv=mv_directional,
                raw_mv=mv_raw,
                price=_num(prices[idx] if idx < len(prices) else None),
                ema9=_num(emas[idx] if idx < len(emas) else None),
                strength=_num(strengths[idx] if idx < len(strengths) else None),
                structure=_num(structures[idx] if idx < len(structures) else None),
            ))
    return Snapshot(session=sess, bar=bar, main_pick=main_pick,
                    market=str(payload.get("market") or payload.get("label") or ""),
                    sectors=sectors, leaders=leaders, final=final,
                    source="board_payload")


def _fresh_state(session: str, cfg: Config) -> dict:
    return {
        "schema": SCHEMA,
        "session": session,
        "config": cfg.public(),
        "processed_bars": [],
        "entries": [],
        "used_names": [],
        "per_sector": {},
        "last_sector_bar": {},
        "last_bar": "",
        "eod_done": False,
        "alerted": [],
        "pending_alerts": [],
        "stats": {},
    }


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # A paper ledger is an audit, not a reason to stop the live scan.
        return {}


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _append_journal(cfg: Config, session: str, row: dict) -> Path:
    cfg.journal_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.journal_dir / f"m1_edge_{session}.jsonl"
    row = {"ts": datetime.now(IST).isoformat(), "schema": SCHEMA, "session": session, **row}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
    return path


def _entry_key(e: dict) -> tuple[str, str, int]:
    return (str(e.get("sector") or ""), str(e.get("name") or ""), int(e.get("side") or 1))


def _summary(state: dict) -> dict:
    entries = state.get("entries") or []
    closed = [e for e in entries if e.get("status") == "closed"]
    return {
        "entries": len(entries),
        "open": sum(e.get("status") == "open" for e in entries),
        "closed": len(closed),
        "stops": sum(e.get("exit_reason") == "stop" for e in closed),
        "targets": sum(e.get("exit_reason") == "target" for e in closed),
        "wins": sum(float(e.get("net_r") or 0) > 0 for e in closed),
        "net_r_sum": round(sum(float(e.get("net_r") or 0) for e in closed), 6),
        "mfe_r_sum": round(sum(float(e.get("mfe_r") or 0) for e in entries), 6),
        "alerts_sent": len(state.get("alerted") or []),
        "alerts_pending": len(state.get("pending_alerts") or []),
    }


def format_bar_alert(snap: Snapshot, result: dict, cfg: Config) -> str:
    """One distinct M1E card for a bar with new paper entries or exits."""
    entries = result.get("new_entries") or []
    closed = (result.get("closed") or []) if cfg.alert_exits else []
    lines = [
        f"<b>[{_esc(cfg.alert_prefix)}]</b>",
        f"{_esc(snap.session)} · {_esc(snap.bar or 'EOD')} · <b>PAPER</b>",
    ]
    if entries:
        label = "entry" if len(entries) == 1 else "entries"
        lines.append(f"<b>{len(entries)} new second-book {label}</b>")
        for e in entries:
            lines.append(
                f"{_side_text(e.get('side'))} {_esc(e.get('sector'))} {_esc(e.get('name'))} "
                f"#{int(e.get('board_pos') or 0)} rank {float(e.get('board_rank') or 0):.1f} "
                f"· persist {float(e.get('dominant_persist') or 0):.0f}/"
                f"{float(e.get('counter_persist') or 0):.0f} "
                f"· main pick {_esc(e.get('main_pick') or 'WAIT')}"
            )
    if closed:
        label = "close" if len(closed) == 1 else "closes"
        lines.append(f"<b>{len(closed)} paper {label}</b>")
        for c in closed:
            lines.append(
                f"{_esc(str(c.get('why') or '').upper())} {_esc(c.get('name'))} "
                f"at {_esc(c.get('bar'))} · net {_signed(c.get('net_r'))}R"
            )
    lines.append("Paper only — no live order. Original 1m book unchanged.")
    return "\n".join(lines)


def format_eod_alert(session: str, state: dict, cfg: Config) -> str:
    """Distinct end-of-day card for the separate paper book."""
    s = _summary(state)
    return "\n".join([
        f"<b>[{_esc(cfg.alert_prefix)} — EOD]</b>",
        f"{_esc(session)} · <b>PAPER SECOND BOOK</b>",
        f"{s['entries']} entries · {s['wins']} wins · {s['stops']} stops · "
        f"{s['closed']} closed",
        f"net {_signed(s['net_r_sum'])}R · MFE {_signed(s['mfe_r_sum'])}R",
        "Original 1m alerts, entries and state remain unchanged.",
    ])


class SecondBookModel:
    """Stateful paper book. Never imports or mutates the original scanner."""

    def __init__(self, config: Config, sender: Callable[..., bool] | None = None,
                 dry_run: bool | None = None):
        self.cfg = config
        # Injected by the real 1m runtime as telegram.send_bot3. Local replay
        # can omit it (alerts stay pending) or pass a print/log sender.
        self.sender = sender
        self.dry_run = (bool(dry_run) if dry_run is not None
                        else _truthy(os.environ.get("DRY_RUN", "0")))

    def _load(self, session: str) -> dict:
        st = _read_state(self.cfg.state_path)
        if not st or st.get("schema") != SCHEMA or st.get("session") != session:
            st = _fresh_state(session, self.cfg)
        st.setdefault("config", self.cfg.public())
        st.setdefault("processed_bars", [])
        st.setdefault("entries", [])
        st.setdefault("used_names", [])
        st.setdefault("per_sector", {})
        st.setdefault("last_sector_bar", {})
        st.setdefault("last_bar", "")
        st.setdefault("eod_done", False)
        st.setdefault("alerted", [])
        st.setdefault("pending_alerts", [])
        st.setdefault("stats", {})
        return st

    def _commit(self, state: dict, session: str, result: dict) -> dict:
        state["stats"] = _summary(state)
        _write_state(self.cfg.state_path, state)
        result["summary"] = _summary(state)
        result["journal_path"] = str(_append_journal(self.cfg, session, {
            "event": "observe" if not result.get("final") else "final",
            "bar": result.get("bar"),
            "config": self.cfg.public(),
            "new_entries": result.get("new_entries", []),
            "marks": result.get("marks", []),
            "closed": result.get("closed", []),
            "rejections": result.get("rejections", []),
            "alerts": result.get("alerts", []),
            "alerts_queued": result.get("alerts_queued", []),
            "alerts_pending": result.get("alerts_pending", 0),
            "alert_delivery_failed": result.get("alert_delivery_failed", False),
            "summary": result["summary"],
        }))
        return result

    @staticmethod
    def _leader_index(snap: Snapshot) -> dict[tuple[str, str, int], Leader]:
        return {(x.sector, x.name, x.side): x for x in snap.leaders}

    def _mark(self, snap: Snapshot, state: dict, result: dict) -> None:
        idx = self._leader_index(snap)
        for e in state["entries"]:
            if e.get("status") != "open":
                continue
            lead = idx.get(_entry_key(e))
            if lead is None:
                result["rejections"].append({"bar": snap.bar, "name": e["name"], "why": "missing_mark"})
                continue
            risk_pct = float(e.get("risk_pct") or self.cfg.risk_pct)
            r = None
            if lead.price is not None and e.get("entry_price"):
                r = int(e["side"]) * (float(lead.price) / float(e["entry_price"]) - 1.0) * 100.0 / risk_pct
            elif lead.mv is not None and e.get("entry_directional_mv") is not None:
                # mv is already directional: positive means the trade is winning.
                r = (float(lead.mv) - float(e["entry_directional_mv"])) / risk_pct
            if r is None:
                result["rejections"].append({"bar": snap.bar, "name": e["name"], "why": "unpriceable_mark"})
                continue
            e["last_r"] = round(r, 6)
            e["mfe_r"] = round(max(float(e.get("mfe_r") or 0.0), r), 6)
            e["mae_r"] = round(min(float(e.get("mae_r") or 0.0), r), 6)
            e["bars_held"] = int(e.get("bars_held") or 0) + 1
            e["last_bar"] = snap.bar
            result["marks"].append({"bar": snap.bar, "name": e["name"], "r": round(r, 4)})
            if r <= -1.0:
                e["status"] = "closed"
                e["exit_bar"] = snap.bar
                e["exit_r"] = -1.0
                e["exit_reason"] = "stop"
                e["net_r"] = round(-1.0 - float(e.get("cost_r") or self.cfg.cost_r), 6)
                result["closed"].append({"bar": snap.bar, "name": e["name"], "why": "stop", "net_r": e["net_r"]})
            elif self.cfg.target_r > 0 and r >= self.cfg.target_r:
                e["status"] = "closed"
                e["exit_bar"] = snap.bar
                e["exit_r"] = float(self.cfg.target_r)
                e["exit_reason"] = "target"
                e["net_r"] = round(float(self.cfg.target_r) - float(e.get("cost_r") or self.cfg.cost_r), 6)
                result["closed"].append({"bar": snap.bar, "name": e["name"], "why": "target", "net_r": e["net_r"]})

    def _enter(self, snap: Snapshot, state: dict, result: dict) -> None:
        if snap.bar and _hhmm(snap.bar) > _hhmm(self.cfg.last_entry):
            result["rejections"].append({"bar": snap.bar, "why": "after_last_entry"})
            return
        if bool(state.get("eod_done")):
            return
        if len(state["entries"]) >= self.cfg.max_day:
            result["rejections"].append({"bar": snap.bar, "why": "max_day"})
            return
        # Strongest one-way sectors first, deterministic within a sector.
        sector_order = sorted(
            snap.sectors.values(),
            key=lambda s: (-s.persist, s.counter, s.name),
        )
        for sec in sector_order:
            if self.cfg.require_gate and not sec.qualified:
                result["rejections"].append({"bar": snap.bar, "sector": sec.name, "why": "gate_closed"})
                continue
            if sec.persist < self.cfg.min_persist or sec.counter > self.cfg.max_counter:
                result["rejections"].append({"bar": snap.bar, "sector": sec.name,
                                            "why": "persistence", "persist": sec.persist,
                                            "counter": sec.counter})
                continue
            last_bar = state["last_sector_bar"].get(sec.name)
            if last_bar and snap.bar and _hhmm(snap.bar) - _hhmm(last_bar) < self.cfg.cooldown_min:
                result["rejections"].append({"bar": snap.bar, "sector": sec.name, "why": "cooldown"})
                continue
            if int(state["per_sector"].get(sec.name, 0)) >= self.cfg.max_per_sector:
                result["rejections"].append({"bar": snap.bar, "sector": sec.name, "why": "sector_cap"})
                continue
            pool = [x for x in snap.leaders if x.sector == sec.name and x.side == sec.side]
            pool.sort(key=lambda x: (-x.rank, x.pos, x.name))
            for lead in pool:
                if len(state["entries"]) >= self.cfg.max_day:
                    result["rejections"].append({"bar": snap.bar, "name": lead.name, "why": "max_day"})
                    break
                if lead.pos > self.cfg.top_n:
                    result["rejections"].append({"bar": snap.bar, "name": lead.name, "why": "outside_top_n"})
                    continue
                if lead.rank < self.cfg.min_rank:
                    result["rejections"].append({"bar": snap.bar, "name": lead.name, "why": "rank_below_min"})
                    continue
                if self.cfg.min_strength and (lead.strength or 0) < self.cfg.min_strength:
                    result["rejections"].append({"bar": snap.bar, "name": lead.name, "why": "strength_below_min"})
                    continue
                if self.cfg.min_structure and (lead.structure or 0) < self.cfg.min_structure:
                    result["rejections"].append({"bar": snap.bar, "name": lead.name, "why": "structure_below_min"})
                    continue
                if lead.name in state["used_names"]:
                    result["rejections"].append({"bar": snap.bar, "name": lead.name, "why": "already_used"})
                    continue
                if lead.price is None and lead.mv is None:
                    result["rejections"].append({"bar": snap.bar, "name": lead.name, "why": "no_price_or_mv"})
                    continue
                risk_pct = self.cfg.risk_pct
                stop_price = None
                if lead.price is not None and lead.ema9 is not None:
                    candidate = abs(float(lead.price) - float(lead.ema9)) / float(lead.price) * 100.0
                    if candidate > 0:
                        risk_pct = candidate
                        stop_price = float(lead.ema9)
                entry = {
                    "id": f"{snap.session}|{snap.bar}|{sec.name}|{lead.name}",
                    "session": snap.session,
                    "bar": snap.bar,
                    "sector": sec.name,
                    "name": lead.name,
                    "side": sec.side,
                    "board_rank": lead.rank,
                    "board_pos": lead.pos,
                    "dominant_persist": sec.persist,
                    "counter_persist": sec.counter,
                    "main_pick": snap.main_pick,
                    "entry_raw_mv": lead.raw_mv,
                    "entry_directional_mv": lead.mv,
                    "entry_price": lead.price,
                    "stop_price": stop_price,
                    "risk_pct": risk_pct,
                    "cost_r": self.cfg.cost_r,
                    "target_r": self.cfg.target_r,
                    "mode": self.cfg.mode,
                    "reason": "first_high_conviction_rebased_top2",
                    "status": "open",
                    "entry_bar": snap.bar,
                    "last_bar": snap.bar,
                    "bars_held": 0,
                    "mfe_r": 0.0,
                    "mae_r": 0.0,
                    "last_r": 0.0,
                }
                state["entries"].append(entry)
                state["used_names"].append(lead.name)
                state["per_sector"][sec.name] = int(state["per_sector"].get(sec.name, 0)) + 1
                state["last_sector_bar"][sec.name] = snap.bar
                result["new_entries"].append({k: entry[k] for k in (
                    "bar", "sector", "name", "side", "board_rank", "board_pos",
                    "dominant_persist", "counter_persist", "main_pick", "entry_raw_mv",
                    "entry_directional_mv", "entry_price", "risk_pct", "reason")})
                if int(state["per_sector"][sec.name]) >= self.cfg.max_per_sector:
                    break

    def _close_eod(self, snap_or_session: Snapshot | str, state: dict, result: dict) -> None:
        bar = snap_or_session.bar if isinstance(snap_or_session, Snapshot) else ""
        for e in state["entries"]:
            if e.get("status") != "open":
                continue
            e["status"] = "closed"
            e["exit_bar"] = bar or e.get("last_bar") or ""
            e["exit_r"] = float(e.get("last_r") or 0.0)
            e["exit_reason"] = "eod"
            e["net_r"] = round(e["exit_r"] - float(e.get("cost_r") or self.cfg.cost_r), 6)
            result["closed"].append({"bar": e["exit_bar"], "name": e["name"], "why": "eod", "net_r": e["net_r"]})
        state["eod_done"] = True

    def _queue_alerts(self, snap: Snapshot, state: dict, result: dict) -> None:
        state.setdefault("pending_alerts", [])
        state.setdefault("alerted", [])
        result["alerts_queued"] = []
        if not self.cfg.alert:
            return
        existing = set(state["alerted"]) | {p.get("id") for p in state["pending_alerts"]}
        entries = result.get("new_entries") or []
        closed = (result.get("closed") or []) if self.cfg.alert_exits else []
        # On the final bar the EOD card is the one clean summary; do not also
        # send a per-bar close card.
        if (entries or closed) and not (snap.final and self.cfg.alert_eod):
            bar_id = f"bar|{snap.session}|{snap.bar}"
            if bar_id not in existing:
                item = {"id": bar_id, "kind": "bar", "bar": snap.bar,
                        "text": format_bar_alert(snap, result, self.cfg)}
                state["pending_alerts"].append(item)
                result["alerts_queued"].append(bar_id)
                existing.add(bar_id)
        if snap.final and self.cfg.alert_eod:
            eod_id = f"eod|{snap.session}"
            if eod_id not in existing:
                item = {"id": eod_id, "kind": "eod", "bar": snap.bar,
                        "text": format_eod_alert(snap.session, state, self.cfg)}
                state["pending_alerts"].append(item)
                result["alerts_queued"].append(eod_id)

    def _deliver_alerts(self, state: dict, result: dict) -> None:
        pending = state.setdefault("pending_alerts", [])
        result["alerts"] = []
        result["alerts_disabled"] = not self.cfg.alert
        result["alert_delivery_failed"] = False
        result["alerts_pending"] = len(pending)
        if not self.cfg.alert or not pending:
            return
        if self.sender is None:
            result["alert_delivery_failed"] = True
            result["alerts"].append({"id": "sender-missing", "delivered": False,
                                     "reason": "no injected bot-3 sender"})
            return
        delivered: list[str] = []
        for item in list(pending):
            ok = False
            try:
                ok = bool(self.sender(item["text"], dry_run=self.dry_run))
            except TypeError as exc:
                if "unexpected keyword argument" in str(exc):
                    ok = bool(self.sender(item["text"]))
                else:
                    result.setdefault("alert_errors", []).append(
                        f"{item.get('id')}: {type(exc).__name__}: {exc}")
            except Exception as exc:                 # noqa: BLE001 — never cost the scan
                result.setdefault("alert_errors", []).append(
                    f"{item.get('id')}: {type(exc).__name__}: {exc}")
            result["alerts"].append({"id": item.get("id"), "kind": item.get("kind"),
                                     "bar": item.get("bar"), "delivered": ok})
            if ok:
                delivered.append(item["id"])
            else:
                result["alert_delivery_failed"] = True
        if delivered:
            state.setdefault("alerted", []).extend(delivered)
            del state["alerted"][:-200]
            state["pending_alerts"] = [p for p in pending if p.get("id") not in delivered]
        result["alerts_pending"] = len(state["pending_alerts"])

    def process(self, snap: Snapshot) -> dict:
        state = self._load(snap.session)
        result: dict[str, Any] = {
            "enabled": True,
            "mode": self.cfg.mode,
            "session": snap.session,
            "bar": snap.bar,
            "final": snap.final,
            "new_entries": [],
            "marks": [],
            "closed": [],
            "rejections": [],
            "alerts": [],
            "alerts_queued": [],
            "alerts_pending": 0,
            "alerts_disabled": not self.cfg.alert,
            "alert_delivery_failed": False,
            "state_path": str(self.cfg.state_path),
        }
        if state.get("last_bar") and snap.bar and _hhmm(snap.bar) < _hhmm(state["last_bar"]):
            result["rejections"].append({"bar": snap.bar, "why": "out_of_order"})
            self._deliver_alerts(state, result)
            return self._commit(state, snap.session, result)
        duplicate = bool(snap.bar and (
            snap.bar in state["processed_bars"] or snap.bar == state.get("last_bar")
        ))
        if duplicate:
            # Duplicate board payloads happen on handover/retry. Do not mark,
            # enter, or re-alert — but do retry any undelivered alert cards.
            result["rejections"].append({"bar": snap.bar, "why": "duplicate_bar"})
            if snap.final and not state.get("eod_done"):
                self._close_eod(snap, state, result)
                self._queue_alerts(snap, state, result)
            self._deliver_alerts(state, result)
            return self._commit(state, snap.session, result)
        self._mark(snap, state, result)
        if not snap.final and not state.get("eod_done"):
            self._enter(snap, state, result)
        if snap.final and not state.get("eod_done"):
            self._close_eod(snap, state, result)
        if snap.bar:
            state["last_bar"] = snap.bar
            if snap.bar not in state["processed_bars"]:
                state["processed_bars"].append(snap.bar)
                del state["processed_bars"][:-130]
        self._queue_alerts(snap, state, result)
        self._deliver_alerts(state, result)
        return self._commit(state, snap.session, result)

    def finalize(self, session: str | None = None) -> dict:
        session = str(session or _ist_today())
        state = self._load(session)
        snap = Snapshot(session=session, bar=state.get("last_bar") or "", final=True)
        result: dict[str, Any] = {
            "enabled": True,
            "mode": self.cfg.mode,
            "session": session,
            "bar": snap.bar,
            "final": True,
            "new_entries": [],
            "marks": [],
            "closed": [],
            "rejections": [],
            "alerts": [],
            "alerts_queued": [],
            "alerts_pending": 0,
            "alerts_disabled": not self.cfg.alert,
            "alert_delivery_failed": False,
            "state_path": str(self.cfg.state_path),
        }
        if state.get("eod_done"):
            result["rejections"].append({"why": "already_finalized"})
        else:
            self._close_eod(snap, state, result)
        self._queue_alerts(snap, state, result)
        self._deliver_alerts(state, result)
        return self._commit(state, session, result)


def evaluate_if_enabled(payload: dict | None, *, session: str | None = None,
                        final: bool = False, env: dict[str, str] | None = None,
                        send: Callable[..., bool] | None = None,
                        dry_run: bool | None = None) -> dict:
    """One-call hook for the 1m scan. No-op when ``M1E_ENABLED`` is unset."""
    cfg = Config.from_env(env)
    if not cfg.enabled:
        return {"enabled": False, "mode": cfg.mode, "reason": "M1E_ENABLED=0",
                "session": str(session or _ist_today()),
                "bar": str((payload or {}).get("b") or (payload or {}).get("bar") or ""),
                "final": final,
                "new_entries": [], "marks": [], "closed": [], "rejections": [],
                "alerts": [], "alerts_queued": [], "alerts_pending": 0,
                "alerts_disabled": True, "alert_delivery_failed": False}
    snap = from_board_payload(payload, session=session, final=final)
    return SecondBookModel(cfg, sender=send, dry_run=dry_run).process(snap)


def finalize_if_enabled(*, session: str | None = None,
                        env: dict[str, str] | None = None,
                        send: Callable[..., bool] | None = None,
                        dry_run: bool | None = None) -> dict:
    cfg = Config.from_env(env)
    if not cfg.enabled:
        return {"enabled": False, "mode": cfg.mode, "reason": "M1E_ENABLED=0",
                "session": str(session or _ist_today()), "new_entries": [],
                "marks": [], "closed": [], "rejections": [],
                "alerts": [], "alerts_queued": [], "alerts_pending": 0,
                "alerts_disabled": True, "alert_delivery_failed": False}
    return SecondBookModel(cfg, sender=send, dry_run=dry_run).finalize(session)


# Aliases used by the Persist-check 1m scanner patch.
maybe_observe = evaluate_if_enabled
maybe_finalize = finalize_if_enabled

__all__ = [
    "SCHEMA", "AUDIT_MEDIAN_RISK_PCT", "AUDIT_MEDIAN_COST_R", "Config",
    "Leader", "Sector", "Snapshot", "SecondBookModel", "from_board_payload",
    "format_bar_alert", "format_eod_alert", "evaluate_if_enabled",
    "finalize_if_enabled", "maybe_observe", "maybe_finalize",
]

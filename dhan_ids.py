"""Dhan instrument master -> securityId map for the names fno_universe.csv lacks.

`fno_universe.csv` carries an id for the 210 F&O names only, so the 196
extra/mapped scan names have no Dhan id: the bootstrap cannot fall back to Dhan
candles for them and their chart link can only use the `NSE:<SYMBOL>` form.
Dhan's public instrument master has every NSE equity, so one best-effort refresh
per weekday fixes both.

The refresh self-validates: a parsed map is only accepted when it agrees with
the ids we already trust in `fno_universe.csv` (TCS 11536, RELIANCE 2885, ...).
A wrong column guess or a changed file layout therefore writes nothing instead
of poisoning the chart links and the candle fallback.
"""
from __future__ import annotations

import csv
import io
import pathlib

import requests

MAP_FILE = "dhan_security_ids.csv"
MASTER_URLS = (
    "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
    "https://images.dhan.co/api-data/api-scrip-master.csv",
)
_MIN_AGREEMENT = 0.90          # of the ids we can already check against
_MIN_AGREED = 100              # and at least this many of them


def _pick(fieldnames, *names):
    """Column lookup: exact (case-insensitive) first, then substring."""
    low = {(f or "").strip().lower(): f for f in fieldnames or []}
    for name in names:
        if name in low:
            return low[name]
    for f in fieldnames or []:
        fl = (f or "").strip().lower()
        for name in names:
            if name in fl:
                return f
    return None


def parse_master(text):
    """{NSE trading symbol: securityId} from a Dhan instrument-master CSV."""
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    id_col = _pick(fields, "sem_smst_security_id", "security_id", "securityid")
    sym_col = _pick(fields, "sem_trading_symbol", "trading_symbol", "symbol")
    custom_col = _pick(fields, "sem_custom_symbol", "custom_symbol")
    exch_col = _pick(fields, "sem_exm_exch_id", "exchange", "exch_id")
    seg_col = _pick(fields, "sem_segment", "segment")
    inst_col = _pick(fields, "sem_instrument_name", "instrument_name", "instrument")
    if not id_col or not (sym_col or custom_col):
        return {}
    out = {}
    for row in reader:
        if exch_col and (row.get(exch_col) or "").strip().upper() != "NSE":
            continue
        if seg_col and (row.get(seg_col) or "").strip().upper() not in {"E", "EQ", "EQUITY", "NSE_EQ"}:
            continue
        if inst_col and (row.get(inst_col) or "").strip().upper() != "EQUITY":
            continue
        sym = (row.get(sym_col) or row.get(custom_col) or "").strip().upper()
        sid = (row.get(id_col) or "").strip()
        if not sym or not sid.isdigit():
            continue
        out[sym] = int(sid)
    return out


def validate(parsed, reference):
    """(ok, agreed, checked) — a map is trusted only when it matches known ids."""
    common = set(parsed) & set(reference)
    agreed = sum(1 for s in common if int(parsed[s]) == int(reference[s]))
    checked = len(common)
    ok = checked >= _MIN_AGREED and agreed / checked >= _MIN_AGREEMENT
    return ok, agreed, checked


def fetch_master(urls=MASTER_URLS, timeout=90):
    """First master that downloads and parses to something plausible."""
    last = None
    for url in urls:
        try:
            r = requests.get(url, timeout=timeout)
            if not r.ok:
                last = f"HTTP {r.status_code} {url}"
                continue
            parsed = parse_master(r.text)
            if parsed:
                return parsed, url
            last = f"no rows parsed from {url}"
        except Exception as e:                     # network/SSL/timeout
            last = f"{type(e).__name__}: {e} ({url})"
    raise RuntimeError(last or "no master URL configured")


def save(parsed, symbols, path=MAP_FILE):
    """Write the wanted symbols only, sorted; returns how many were written."""
    wanted = [(s, parsed[s]) for s in dict.fromkeys(symbols) if s in parsed]
    fp = pathlib.Path(path)
    fp.write_text("symbol,securityId\n" + "".join(f"{s},{i}\n" for s, i in sorted(wanted)),
                  encoding="utf-8")
    return len(wanted)


def load(path=MAP_FILE):
    """{symbol: securityId} from a previously written map; {} when absent/invalid."""
    fp = pathlib.Path(path)
    if not fp.exists():
        return {}
    out = {}
    try:
        for row in csv.DictReader(fp.open()):
            sym = (row.get("symbol") or "").strip().upper()
            sid = (row.get("securityId") or "").strip()
            if sym and sid.isdigit():
                out[sym] = int(sid)
    except Exception:
        return {}
    return out

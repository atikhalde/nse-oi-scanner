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


# NSE cash segments that carry a chartable security id. "E"/"EQ" are the normal
# rolling-settlement series; "BE" is trade-to-trade and "BZ" the illiquid series,
# which a stock can move into (or be listed only in) for long stretches.
_SEG_PRIORITY = {"E": 0, "EQ": 0, "EQUITY": 0, "NSE_EQ": 0, "BE": 1, "BZ": 2, "BZ_BE": 2}


def parse_master(text, watch=()):
    """{NSE trading symbol: securityId} from a Dhan instrument-master CSV.

    When `watch` is given, also returns a diagnostic map {symbol: "EXCH/SEG/INST"}
    for those symbols wherever they appear in the file, so a name the filter drops
    can be told apart from one the master simply does not list (the actionable
    difference between "relax the filter" and "the ticker is gone"). When a
    symbol is listed in several series the ordinary equity series wins over
    BE/BZ; otherwise the first row wins.
    """
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    id_col = _pick(fields, "sem_smst_security_id", "security_id", "securityid")
    sym_col = _pick(fields, "sem_trading_symbol", "trading_symbol", "symbol")
    custom_col = _pick(fields, "sem_custom_symbol", "custom_symbol")
    exch_col = _pick(fields, "sem_exm_exch_id", "exchange", "exch_id")
    seg_col = _pick(fields, "sem_segment", "segment")
    inst_col = _pick(fields, "sem_instrument_name", "instrument_name", "instrument")
    if not id_col or not (sym_col or custom_col):
        return ({}, {}) if watch else {}
    watch = {str(w).strip().upper() for w in watch}
    seen, best = {}, {}
    for row in reader:
        sym = (row.get(sym_col) or row.get(custom_col) or "").strip().upper()
        if not sym:
            continue
        exch = ((row.get(exch_col) or "").strip().upper() if exch_col else "")
        seg = ((row.get(seg_col) or "").strip().upper() if seg_col else "")
        inst = ((row.get(inst_col) or "").strip().upper() if inst_col else "")
        if watch and sym in watch and sym not in seen:
            seen[sym] = "/".join([exch or "?", seg or "?", inst or "?"])
        if exch_col and exch != "NSE":
            continue
        # A blank or unexpected instrument/segment is not an equity row. Keep the
        # filter strict here: the 2026-11-27 18:47Z run showed a blank-instrument
        # MOTHERSON row shadowing the real one (id 25510 instead of 4204), which
        # dropped the master's agreement with fno_universe.csv to 208/209.
        if inst_col and inst != "EQUITY":
            continue
        if seg_col and seg not in _SEG_PRIORITY:
            continue
        sid = (row.get(id_col) or "").strip()
        if not sid.isdigit():
            continue
        rank = _SEG_PRIORITY.get(seg, 9)
        if sym not in best or rank < best[sym][0]:
            best[sym] = (rank, int(sid))
    out = {sym: sid for sym, (_, sid) in best.items()}
    return (out, seen) if watch else out


def validate(parsed, reference):
    """(ok, agreed, checked) — a map is trusted only when it matches known ids."""
    common = set(parsed) & set(reference)
    agreed = sum(1 for s in common if int(parsed[s]) == int(reference[s]))
    checked = len(common)
    ok = checked >= _MIN_AGREED and agreed / checked >= _MIN_AGREEMENT
    return ok, agreed, checked


def disagreements(parsed, reference):
    """{symbol: "reference=X master=Y"} where the master and a trusted map differ."""
    out = {}
    for sym in sorted(set(parsed) & set(reference)):
        if int(parsed[sym]) != int(reference[sym]):
            out[sym] = f"{int(reference[sym])}->{int(parsed[sym])}"
    return out


def fetch_text(urls=MASTER_URLS, timeout=90):
    """First master CSV that downloads as text, with the URL it came from."""
    last = None
    for url in urls:
        try:
            r = requests.get(url, timeout=timeout)
            if not r.ok:
                last = f"HTTP {r.status_code} {url}"
                continue
            if r.text and r.text.strip():
                return r.text, url
            last = f"empty body from {url}"
        except Exception as e:                     # network/SSL/timeout
            last = f"{type(e).__name__}: {e} ({url})"
    raise RuntimeError(last or "no master URL configured")


def fetch_master(urls=MASTER_URLS, timeout=90, watch=()):
    """First master that downloads and parses to something plausible."""
    last = None
    for url in urls:
        try:
            text, got = fetch_text([url], timeout=timeout)
            parsed = parse_master(text, watch=watch) if watch else parse_master(text)
            if parsed:
                return parsed, got
            last = f"no rows parsed from {url}"
        except Exception as e:                     # network/SSL/timeout
            last = f"{type(e).__name__}: {e} ({url})"
    raise RuntimeError(last or "no master URL configured")


def resolve_from_masters(symbols, urls=MASTER_URLS, timeout=90):
    """Second chance for `symbols` the first master did not carry.

    Dhan publishes a detailed master and a compact one; a name can be missing
    from the detailed file (the 2026-11-27 run left ATHERENERGY, GSPL,
    GUJGASLTD, JBCHEPHARM, HEG and ICICIPRULI unresolved) while the compact file
    still lists it. Every URL is tried, in order, for the symbols still missing.

    Returns (ids, seen, tried) where `seen` is the same "EXCH/SEG/INST" map for
    whatever stayed missing and `tried` lists the URLs actually fetched.
    """
    ids, seen, tried = {}, {}, []
    pending = [str(s).strip().upper() for s in symbols]
    for url in urls:
        if not pending:
            break
        try:
            text, got = fetch_text([url], timeout=timeout)
        except Exception:
            tried.append(url)
            continue
        tried.append(got)
        got_ids, got_seen = parse_master(text, watch=pending)
        for sym in pending:
            if sym in got_ids:
                ids[sym] = got_ids[sym]
        for sym, where in got_seen.items():
            seen.setdefault(sym, where)
        pending = [s for s in pending if s not in ids]
    return ids, seen, tried


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
        with fp.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            sym = (row.get("symbol") or "").strip().upper()
            sid = (row.get("securityId") or "").strip()
            if sym and sid.isdigit():
                out[sym] = int(sid)
    except Exception:
        return {}
    return out

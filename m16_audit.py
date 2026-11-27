"""Detailed M16 PDF review from observed state, never invented trade outcomes."""
from __future__ import annotations
from collections import Counter
from html import escape
from pathlib import Path
from pprint import pformat
import textwrap

import pandas as pd


def sections(st):
    """Plain-text sections shared by PDF, TXT and offline assertions."""
    setups = st.get('setups', {})
    events = st.get('events', [])
    bars = st.get('audit_bars', {})
    dormant = set(st.get('inactive_symbols', []))
    provider_counts = dict(Counter('usable' if x.get('ok') else 'unusable'
                                   for sym, x in st.get('feed_health', {}).items() if sym not in dormant))
    dormant_note = f"; dormant excluded: {sorted(dormant)}" if dormant else ""
    outbox = st.get('outbox', {})
    sent = [v for v in outbox.values() if v.get('status') == 'sent' or 'sent' in (v.get('deliveries') or {}).values()]
    duplicates = [e for e in events if e.get('duplicate_of')]
    modes = dict(Counter(e.get('sector_mode') or st.get('sector_mode') or 'unknown' for e in events)) or {
        (st.get('sector_mode') or 'unknown'): 0}
    if set(modes) == {'unknown'}:
        policy = ("Sector mode: not recorded in this state (written before the advisory switch); "
                  "the events below were produced by the fail-closed gate: score > 70 and top-3 rank required")
    else:
        policy = (f"Sector mode: {', '.join(f'{k} ({v} events)' for k, v in modes.items())} | "
                  + ("the board is CONTEXT on every alert, never a send filter"
                     if 'advisory' in modes else "fail-closed gate: score > 70 and top-3 rank required"))
    coverage = [f"Date: {st.get('date', '?')} (IST) | Mode: {st.get('mode', 'feed-polled observations; historical events are not live alerts')}",
                policy,
                f"Original tracked signals: {len(setups)}; continuation observations: {len(events)}",
                f"Setup phases: {dict(Counter(s.get('phase', '?') for s in setups.values()))}",
                f"Persistence-only sector board required: {st.get('sector_gate_required', False)}; decisions: {dict(Counter(e.get('sector_gate', {}).get('status', 'legacy') for e in events))}; snapshots: {len(st.get('sector_snapshots', []))}",
                f"Event types: {dict(Counter(e.get('kind', '?') for e in events))}",
                f"Recorded completed candles: {sum(len(b) for b in bars.values())} across {len(bars)} symbols.",
                f"Alerts delivered: {len(sent)}; duplicate-suppressed (same candle already delivered): {len(duplicates)}; "
                f"not delivered: {len(outbox) - len(sent) - len(duplicates)}; events with no outbox item: {len(events) - len(outbox)}",
                f"Promoted after a board wait (live flag): {sum(bool(e.get('live')) for e in events)}; historical only: {sum(not bool(e.get('live')) and not e.get('duplicate_of') for e in events)}",
                f"Delayed re-entry alerts (BOS found late, priced at discovery): {sum(bool(e.get('late')) for e in events)}"
                + (f" | delays {dict(Counter(round(e['late']['delay_minutes'], 1) for e in events if e.get('late')))}" if any(e.get('late') for e in events) else ""),
                f"Monitoring last checked: {st.get('monitoring_health', {}).get('at', st.get('health_notice', {}).get('checked_at', '?'))}; affected symbols: {len(st.get('monitoring_health', {}).get('affected', st.get('health_notice', {}).get('affected', [])))}",
                f"Provider results (latest response, not a latency guarantee): {provider_counts}{dormant_note}; full per-symbol details in appendix",
                "Late backfill cannot reconstruct missed intrabar crossings; wall-clock-expired setups may remain expired, so historical matches are not a complete opportunity count.",
                f"Diagnostic records omitted by retention cap: {st.get('diagnostics_omitted', 0)}",
                "Coverage is OBSERVED data only. Discovery covers the universe before 11:00; seeded stocks are then watched through market close.",
                "Entry confirmation cutoff remains strictly before 12:30. Later prices are research observations, NOT new entries or trades.",
                "M16 is alerts-only: no executed fills, transaction costs, SL exits or realized P&L are claimed.",
                "Five-minute OHLC cannot reconstruct historical intrabar crossing times or the order of wicks.",
                "Missing candles, unobserved sessions and legacy-state details are not fabricated."]
    yield '1. Executive summary and coverage', coverage
    yield '2. Rules and interpretation', [
        'All final chart labels/EX variants seed tracking; previews 90/290 excluded. Seed confirmed before 11:00; BOS confirmation before 12:30.',
        'Pre-pullback rally: >=3 completed candles, >=2 directional advances, >=0.3% progress, efficiency >=0.65 and both EMA slopes directional.',
        'From seed through confirmation, BUY closes >= both EMA9/10; SELL closes <= both. Wick touches allowed. Missing continuity invalidates.',
        'Fib 0.236-0.382 band or EMA9/10 wick touch; freeze prior extreme as BOS. >50% retracement invalidates. Default sector mode is ADVISORY: every fresh signal is delivered and the M6 persistence board appears as context (score, 15-minute slope, rank, board breadth). Setting M16_SECTOR_MODE=gate restores the fail-closed board (score >70 and top-3 rank, waiting for publication).',
        'Duplicate policy: one alert per symbol/side/candle. When a running alert was already delivered for that candle, its close confirmation is recorded as duplicate_suppressed instead of sent again (M16_SUPPRESS_DUPLICATES=0 keeps both). A CLOSE_FAILED risk update is never suppressed.',
        'RUNNING_BREAK is provisional. CLOSE_CONFIRMED and CLOSE_FAILED describe the SAME candle. CLOSE_ONLY means no intrabar crossing was observed.',
        'DIRECT_BOS_CONFIRMED: the original final indicator label itself occurs on a qualifying BOS close. Prior price-pattern evidence, not a fabricated earlier indicator signal; no second pullback required.',
        'DELAYED re-entry: when a close-confirmed BOS is first discovered 30s-15min late and price is still within 0.75% of the frozen BOS, the alert is sent priced at the discovery candle and labelled DELAYED. Provisional RUNNING_BREAK alerts are never delayed-issued.',
        'Delayed feed, computation, Telegram and GitHub scheduling can add latency. Poll cadence is not a zero-delay market-data guarantee.',
        'Complete recorded values are in the appendix, including per-bot delivery identifiers (hashes, never credentials).']
    signals = []
    for s in st.get('signal_audit', []):
        signals.append(f"{s.get('bar')} {s.get('symbol')} code={s.get('code')} {s.get('signal')} | {s.get('decision')} | {s.get('reason', '')} | observed={s.get('observed', '?')} historical={s.get('historical', '?')}")
    yield '3. Indicator signal audit / selection decisions', signals or ['No signal-decision journal available for this state. See tracked seeds and legacy coverage limits.']
    reasons = Counter(s.get('invalid_reason', s.get('waiting_reason', s.get('phase', '?')))
                      for s in setups.values() if s.get('phase') != 'confirmed')
    yield '4. Blocked, invalid, waiting and expired reasons', [f'{n} x {why}' for why,n in reasons.most_common()] or ['No tracked setups.']
    detail = []
    for key, s in sorted(setups.items()):
        detail.extend([f"SETUP {key}",
                       f"{s.get('symbol')} {s.get('side')} {s.get('signal')} | seed {s.get('signal_bar')} price {s.get('signal_price')}",
                       f"Phase {s.get('phase')}; reason {s.get('invalid_reason', s.get('waiting_reason', '-'))}",
                       f"Anchor {s.get('anchor')}; extreme {s.get('peak')} at {s.get('peak_bar')}; BOS {s.get('bos')}",
                       f"Pullback {s.get('pullback')} at {s.get('pullback_bar')}; low/high {s.get('pullback_low')}/{s.get('pullback_high')}",
                       'Pre-pullback rally evidence: '+pformat(s.get('rally_evidence', {})),
                       'Last checked closed candle: '+str(s.get('last_checked_bar')), ''])
    yield '5. Every tracked setup - rally, pullback, BOS and final state', detail or ['None.']
    yield '6. Phase-change timeline', [pformat(t, width=110) for t in st.get('transitions', [])] or ['No transition journal available.']
    yield '7. Running / closing alert observations', [pformat(e, width=110) for e in events] or ['No continuation observations.']
    paths = []
    # Only candles AFTER the event candle: a same-bar high/low could precede
    # the crossing, so it cannot be used as a post-entry excursion.
    for e in events:
        symbol = e['symbol']
        later = [b for t,b in sorted(bars.get(symbol, {}).items()) if pd.Timestamp(t) > pd.Timestamp(e['bar'])]
        if not later or not e.get('price'):
            paths.append(f"{e['id']}: no later recorded candles; no path inferred.")
            continue
        ref = float(e['price'])
        high, low, last = max(b['high'] for b in later), min(b['low'] for b in later), later[-1]['close']
        if e['side'] == 'BUY':
            favorable, adverse, change = (high/ref-1)*100, (low/ref-1)*100, (last/ref-1)*100
        else:
            favorable, adverse, change = (1-low/ref)*100, (1-high/ref)*100, (1-last/ref)*100
        paths.append(f"{symbol} {e['kind']} at {e['bar']} ref {ref:.4f} | later candles {len(later)} | "
                     f"directional best {favorable:+.3f}%, worst {adverse:+.3f}%, last {change:+.3f}% | last recorded {later[-1]['dt']}")
    yield '8. Later price behavior - observations, NOT trade P&L', [
        'Exclude the event candle. No assumed entry execution, stop, target, fees or guaranteed tradable prices. Values can remain negative/positive throughout; they are raw extrema, not clipped MFE/MAE.'] + (paths or ['No events to evaluate.'])
    yield '9. Bot delivery and stale-send audit', alert_ledger(st)
    yield '10. Feed, engine and retention diagnostics', [pformat(d, width=110) for d in st.get('diagnostics', [])] or ['No diagnostics recorded; this alone does not prove uninterrupted data coverage.']
    coverage = []
    for symbol, values in sorted(bars.items()):
        keys = sorted(values)
        if not keys:
            continue
        seeded = any(s.get('symbol') == symbol for s in setups.values())
        end = '15:25' if seeded else '10:50'
        expected = pd.date_range(st['date']+' 09:15', st['date']+' '+end, freq='5min', tz='Asia/Kolkata')
        actual = {pd.Timestamp(k) for k in keys}
        missing = [t.strftime('%H:%M') for t in expected if t not in actual]
        coverage.append(f"{symbol}: {len(keys)} candles | {keys[0]} through {keys[-1]} | {'seeded full-day' if seeded else 'discovery-only'} | absent expected bars: {', '.join(missing) or 'none'}")
    yield '11. Per-symbol candle coverage', coverage or ['No candle journal available. Legacy states cannot supply missing history.']
    yield '12. COMPLETE SAVED AUDIT - all recorded fields', pformat(st, width=110, sort_dicts=False).splitlines()


def alert_ledger(st):
    """One readable line per alert: delivered, duplicate-suppressed or not sent.

    The full raw outbox dict is still in the appendix; this is the summary a
    reader needs to answer "what did the bot actually send today?".
    """
    lines, outbox = [], st.get('outbox', {})
    events = {e.get('id'): e for e in st.get('events', [])}
    for key, item in outbox.items():
        e = item.get('event') or events.get(key) or {}
        delivered = item.get('status') == 'sent' or 'sent' in (item.get('deliveries') or {}).values()
        if delivered:
            state = 'DELIVERED'
        elif item.get('status') == 'duplicate_suppressed':
            first = events.get(item.get('suppresses')) or {}
            state = f"DUPLICATE of {str(first.get('bar', '?'))[11:16]} alert (same candle)"
        elif item.get('status') == 'sector_rejected':
            state = 'NOT SENT (gate mode: sector)'
        elif item.get('status') == 'expired_unsent':
            state = 'NOT SENT (expired before delivery)'
        else:
            state = f"NOT SENT ({item.get('status', '?')})"
        gate = e.get('sector_gate_at_send') or e.get('sector_gate') or {}
        ctx = ''
        if gate.get('sector'):
            ctx = f" | {gate['sector']} {float(gate.get('score') or 0):.1f}"
            if gate.get('slope15') is not None:
                ctx += f" ({float(gate['slope15']):+.1f}/15m)"
            if gate.get('known_rank'):
                ctx += f" rank {gate['known_rank']}"
            if gate.get('status') == 'unknown':
                ctx += ' unverified'
        ext = None
        if e.get('bos') and e.get('price'):
            sign = 1 if e.get('side') == 'BUY' else -1
            try:
                ext = sign * (float(e['price']) - float(e['bos'])) / float(e['bos']) * 100.
            except (TypeError, ValueError, ZeroDivisionError):
                ext = None
        ext_s = f" | ext {ext:+.2f}%" if ext is not None else ''
        ema_s = f" | EMA undercut {float(e['ema_undercut']):.2f}%" if e.get('ema_undercut') else ''
        late_s = f" | late {e['late']['delay_minutes']:.1f} min" if e.get('late') else ''
        mirror = f"{sum(1 for v in (item.get('deliveries') or {}).values() if v == 'sent')} destination(s)"
        lines.append(f"{str(e.get('observed', ''))[11:19]} {e.get('symbol', '?')} {e.get('side', '?')} "
                     f"{e.get('kind', '?')} @ {e.get('price')} | {state}{ctx}{ext_s}{ema_s}{late_s}"
                     + (f" | {mirror}" if delivered else ''))
    suppressed = [e for e in st.get('events', []) if e.get('duplicate_of') and e.get('id') not in outbox]
    for e in suppressed:
        lines.append(f"{str(e.get('observed', ''))[11:19]} {e.get('symbol')} {e.get('side')} {e.get('kind')} "
                     f"@ {e.get('price')} | DUPLICATE not sent (kept {e['duplicate_of'].split(':')[2:4]})")
    counts = Counter('delivered' if (v.get('status') == 'sent' or 'sent' in (v.get('deliveries') or {}).values())
                     else ('duplicate' if v.get('status') == 'duplicate_suppressed' else 'not sent')
                     for v in outbox.values())
    lines.insert(0, f"Outbox summary: {dict(counts)}")
    lines.extend(['TXT delivery acknowledgements: '+pformat(st.get('eod_deliveries', {})),
                  'PDF delivery acknowledgements (snapshot before sending): '+pformat(st.get('eod_pdf_deliveries', {})),
                  'Sending after a crash is uncertain, not proof of delivery. Reports cannot contain acknowledgements received after their own creation.'])
    return lines


def build_pdf(st, path):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Preformatted, Spacer, PageBreak

    font = 'Helvetica'
    for candidate in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf'):
        if Path(candidate).exists():
            font = 'M16Audit'
            if font not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font, candidate))
            break
    printable_w = A4[0] - 72
    normal = ParagraphStyle('audit', fontName=font, fontSize=8, leading=11, spaceAfter=3, wordWrap='CJK')
    heading = ParagraphStyle('heading', parent=normal, fontSize=13, leading=17, spaceAfter=10, textColor=colors.HexColor('#164e63'))
    title = ParagraphStyle('title', parent=heading, fontSize=18, leading=24, alignment=TA_CENTER)
    appendix_pre = ParagraphStyle('appendix_pre', fontName=font, fontSize=6.5, leading=8, spaceAfter=0)
    story = [Paragraph('M16 FULL-DAY CONTINUATION AUDIT', title),
             Paragraph(escape(str(st.get('date', '?'))) + ' - IST', normal), Spacer(1, 10)]
    for i, (name, lines) in enumerate(sections(st)):
        if i:
            story.append(PageBreak())
        story.append(Paragraph(escape(name), heading))
        if i == 11:
            # Render the full saved-state dump in bounded Preformatted blocks so
            # 150k+ appendix lines build in ~5s instead of ~105s with zero truncation.
            wrapped = []
            for line in lines:
                for physical in str(line).splitlines() or ['']:
                    for part in textwrap.wrap(physical, width=110, replace_whitespace=False, drop_whitespace=False) or [' ']:
                        text = ''.join(c if ord(c) >= 32 or c == '\t' else ' ' for c in part).expandtabs(4)
                        if font == 'Helvetica':
                            text = text.encode('cp1252', errors='replace').decode('cp1252')
                        if len(text) <= 80 or pdfmetrics.stringWidth(text, font, 6.5) <= printable_w:
                            wrapped.append(text)
                        else:
                            buf = ''
                            for ch in text:
                                if buf and pdfmetrics.stringWidth(buf + ch, font, 6.5) > printable_w:
                                    wrapped.append(buf)
                                    buf = ch
                                else:
                                    buf += ch
                            wrapped.append(buf or ' ')
            for k in range(0, len(wrapped), 55):
                story.append(Preformatted('\n'.join(wrapped[k:k + 55]), appendix_pre))
            continue
        for line in lines:
            # Bounded flowables prevent LayoutError from extremely long keys,
            # diagnostics or a large nested saved-state string. No truncation.
            for physical in str(line).splitlines() or ['']:
                for part in textwrap.wrap(physical, width=115, replace_whitespace=False, drop_whitespace=False) or [' ']:
                    text = ''.join(c if ord(c) >= 32 or c == '\t' else ' ' for c in part)
                    if font == 'Helvetica':
                        text = text.encode('cp1252', errors='replace').decode('cp1252')
                    story.append(Paragraph(escape(text).replace('  ', '&nbsp; '), normal))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    def footer(canvas, doc):
        canvas.setFont(font, 7)
        canvas.drawString(36, 20, f"M16 {st.get('date', '?')} | Observed data only; not executed trades")
        canvas.drawRightString(A4[0]-36, 20, f'Page {doc.page}')
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36,
                      title=f"M16 full-day audit {st.get('date', '')}", author='M16').build(story, onFirstPage=footer, onLaterPages=footer)
    return path

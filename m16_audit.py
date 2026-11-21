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
    coverage = [f"Date: {st.get('date', '?')} (IST) | Mode: {st.get('mode', 'live observations')}",
                f"Original tracked signals: {len(setups)}; continuation observations: {len(events)}",
                f"Setup phases: {dict(Counter(s.get('phase', '?') for s in setups.values()))}",
                f"Event types: {dict(Counter(e.get('kind', '?') for e in events))}",
                f"Recorded completed candles: {sum(len(b) for b in bars.values())} across {len(bars)} symbols.",
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
        'Fib 0.236-0.382 band or EMA9/10 wick touch; freeze prior extreme as BOS. >50% retracement invalidates. No sector/persistent/top-five filters.',
        'RUNNING_BREAK is provisional. CLOSE_CONFIRMED and CLOSE_FAILED describe the SAME candle. CLOSE_ONLY means no intrabar crossing was observed.',
        'DIRECT_BOS_CONFIRMED: the original final indicator label itself occurs on a qualifying BOS close. Prior price-pattern evidence, not a fabricated earlier indicator signal; no second pullback required.',
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
    delivery = [f"{key}: {pformat(value, width=110)}" for key,value in st.get('outbox', {}).items()]
    delivery.extend(['TXT delivery acknowledgements: '+pformat(st.get('eod_deliveries', {})),
                     'PDF delivery acknowledgements (snapshot before sending): '+pformat(st.get('eod_pdf_deliveries', {})),
                     'Sending after a crash is uncertain, not proof of delivery. Reports cannot contain acknowledgements received after their own creation.'])
    yield '9. Bot delivery and stale-send audit', delivery
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


def build_pdf(st, path):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak

    font = 'Helvetica'
    for candidate in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf'):
        if Path(candidate).exists():
            font = 'M16Audit'
            if font not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font, candidate))
            break
    normal = ParagraphStyle('audit', fontName=font, fontSize=8, leading=11, spaceAfter=3, wordWrap='CJK')
    heading = ParagraphStyle('heading', parent=normal, fontSize=13, leading=17, spaceAfter=10, textColor=colors.HexColor('#164e63'))
    title = ParagraphStyle('title', parent=heading, fontSize=18, leading=24, alignment=TA_CENTER)
    small = ParagraphStyle('appendix', parent=normal, fontSize=7, leading=9)
    story = [Paragraph('M16 FULL-DAY CONTINUATION AUDIT', title),
             Paragraph(escape(str(st.get('date', '?'))) + ' - IST', normal), Spacer(1, 10)]
    for i, (name, lines) in enumerate(sections(st)):
        if i:
            story.append(PageBreak())
        story.append(Paragraph(escape(name), heading))
        for line in lines:
            # Bounded flowables prevent LayoutError from extremely long keys,
            # diagnostics or a large nested saved-state string. No truncation.
            for physical in str(line).splitlines() or ['']:
                for part in textwrap.wrap(physical, width=115, replace_whitespace=False, drop_whitespace=False) or [' ']:
                    text = ''.join(c if ord(c) >= 32 or c == '\t' else ' ' for c in part)
                    if font == 'Helvetica':
                        text = text.encode('cp1252', errors='replace').decode('cp1252')
                    story.append(Paragraph(escape(text).replace('  ', '&nbsp; '), small if i == 11 else normal))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    def footer(canvas, doc):
        canvas.setFont(font, 7)
        canvas.drawString(36, 20, f"M16 {st.get('date', '?')} | Observed data only; not executed trades")
        canvas.drawRightString(A4[0]-36, 20, f'Page {doc.page}')
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36,
                      title=f"M16 full-day audit {st.get('date', '')}", author='M16').build(story, onFirstPage=footer, onLaterPages=footer)
    return path

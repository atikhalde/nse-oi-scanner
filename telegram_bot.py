"""Telegram notifier. Credentials from env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.

Failure-visibility rules (08-Nov alert-blackout fix):
  * every rejected/failed send prints a greppable ``TG_FAIL`` line including the
    HTTP status and Telegram's own ``description`` — no more silent drops;
  * a 400 "can't parse entities" reply triggers ONE automatic retry with all
    stray &, <, > escaped (raw ampersands in "P&L", "F&O movers" and symbols
    like M&M / GVT&D otherwise get the whole message rejected by Telegram's
    HTML parser).
"""
import html as _html
import os
import re
import time
from urllib.parse import quote

import requests

API = "https://api.telegram.org"

# Chart that opens when the stock name in an alert is tapped. It stays inside
# Dhan: charts.dhan.co is Dhan's own charts home (the TradingCharts engine
# served by DEXT, signed in with the Dhan account), so no separate
# tradingcharts.co/Google login is involved.
# The instrument key is the one Dhan's own "Launch This Stock" links carry:
#   ?symbol=NSEE<security-id>:<SYMBOL>      e.g. NSEE11536:TCS
# and the platform's published instrument string (the one chartscript's
# request.security takes) is `NSE:<SYMBOL>`, used when a name has no known
# security id (the non-F&O extras). `{instrument}` is URL-quoted for you;
# `{symbol}` is accepted as the same placeholder for older overrides.
# M16_CHART_URL_TEMPLATE overrides the whole template without a code change.
CHART_URL_TEMPLATE = "https://charts.dhan.co/?symbol={instrument}"


def chart_instrument(symbol, security_id=None):
    """TradingCharts instrument key: NSEE<id>:SYM when the id is known, else NSE:SYM."""
    sid = "" if security_id is None else str(security_id).strip()
    if sid in {"", "nan", "None", "-1"}:
        return f"NSE:{symbol}"
    return f"NSEE{sid}:{symbol}"


def chart_url(symbol, security_id=None, template=None):
    """Deep link to the stock's chart; the instrument is quoted, never interpolated raw."""
    tmpl = template or os.environ.get("M16_CHART_URL_TEMPLATE") or CHART_URL_TEMPLATE
    inst = quote(chart_instrument(symbol, security_id), safe=":")
    return tmpl.replace("{instrument}", inst).replace("{symbol}", inst)


def chart_link(symbol, security_id=None):
    """The alert's stock name: bold, and a tap opens its chart (Telegram HTML)."""
    return (f'<b><a href="{_html.escape(chart_url(symbol, security_id), quote=True)}">'
            f'{esc(symbol)}</a></b>')


# Dhan publishes one Android app entry point: its own Adjust link, which hands
# a `dhan://…` route to the app and falls back to the web when the app is absent
# (this is the "Use App" button on dhan.co). Telegram will not open a raw
# `dhan://` anchor — it only follows http(s) — so the route always travels as
# the https Adjust link. There is no published per-stock route, so the default
# is `dhan://Login`; the symbol keeps the exact chart link.
# `{deeplink}` and `{fallback}` are URL-quoted for you. M16_APP_URL_TEMPLATE
# overrides the whole template, and M16_APP_LINK=0 removes the line.
DHAN_APP_URL_TEMPLATE = ("https://dhan.go.link/?adj_t=tunb1sj&adj_deeplink={deeplink}"
                         "&adj_fallback={fallback}&adj_campaign=m16-alert")


def app_link_enabled():
    return os.environ.get("M16_APP_LINK", "1").strip().lower() not in {"0", "false", "no"}


def app_deeplink(symbol=None, security_id=None, deeplink=None):
    """Route handed to the Dhan app; M16_APP_DEEPLINK overrides it without a code change.

    Probed on 27-Nov-2026: the app honours `dhan://Login` only — twelve chart
    routes (Chart/stocks/scrip/terminal/Search, symbol and securityId forms) all
    land on the app home, and no Dhan host publishes an App Link, so an https
    chart link always opens the browser. If Dhan ships a route later, set
    M16_APP_DEEPLINK (placeholders `{symbol}`, `{instrument}`, `{security_id}`).
    """
    route = deeplink or os.environ.get("M16_APP_DEEPLINK") or "dhan://Login"
    return (str(route)
            .replace("{symbol}", str(symbol or ""))
            .replace("{instrument}", chart_instrument(symbol, security_id) if symbol else "")
            .replace("{security_id}", "" if security_id in (None, "") else str(security_id)))


def app_url(symbol=None, security_id=None, deeplink=None):
    """https link that hands the route to the Dhan Android app (web chart as fallback)."""
    tmpl = os.environ.get("M16_APP_URL_TEMPLATE") or DHAN_APP_URL_TEMPLATE
    fallback = chart_url(symbol, security_id) if symbol else "https://web.dhan.co/"
    return (tmpl.replace("{deeplink}", quote(app_deeplink(symbol, security_id, deeplink), safe=""))
                .replace("{fallback}", quote(fallback, safe="")))


# Candidate in-app routes for opening a stock chart. Dhan publishes only
# `dhan://Login`; these are the plausible shapes, probed on the phone through
# `m16_runner.py --probe-app-links` so the real one can be adopted.
def app_link_candidates(symbol="AXISBANK", security_id=5900):
    # Raw (un-quoted) routes: they are quoted exactly once when the https app
    # link is built, so `NSE:AXISBANK` reaches the app as `NSE%3A`.
    sym = str(symbol)
    inst = chart_instrument(symbol, security_id)
    path = [
        ("Chart?symbol=SYM", f"dhan://Chart?symbol={sym}"),
        ("chart?symbol=SYM", f"dhan://chart?symbol={sym}"),
        ("Chart?symbol=NSE:SYM", f"dhan://Chart?symbol=NSE:{sym}"),
        ("Chart?symbol=NSEE<id>:SYM", f"dhan://Chart?symbol={inst}"),
        ("Chart?securityId=<id>", f"dhan://Chart?securityId={quote(str(security_id), safe='')}"),
        ("Chart?securityId&exchangeSegment", f"dhan://Chart?securityId={quote(str(security_id), safe='')}"
                                            "&exchangeSegment=NSE_EQ"),
        ("Chart/SYM", f"dhan://Chart/{sym}"),
        ("stocks/SYM", f"dhan://stocks/{sym}"),
        ("scrip/SYM", f"dhan://scrip/{sym}"),
        ("terminal?symbol=NSEE<id>:SYM", f"dhan://terminal?symbol={inst}"),
        ("Watchlist", "dhan://Watchlist"),
        ("Search?query=SYM", f"dhan://Search?query={sym}"),
    ]
    return [(label.replace("SYM", str(symbol)).replace("<id>", str(security_id)), url)
            for label, url in path]


def configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def esc(s):
    """Escape &, <, > for Telegram HTML parse mode."""
    return _html.escape(str(s), quote=False)


# Entities this module already emits. They are parked while the text is escaped
# and restored afterwards, so healing a correctly escaped message is a no-op:
# without this, "M&amp;M" became "M&amp;amp;M" on the 400 retry and showed the
# entity literally in the chat.
_HEAL_ENTITIES = (("&amp;", "\x00amp\x00"), ("&lt;", "\x00lt\x00"), ("&gt;", "\x00gt\x00"))


def _heal_html(text):
    """Escape every stray &, <, > but keep the tags this codebase emits.

    `<b>`/`</b>`, the chart/mirror anchors `<a href="…">`/`</a>`, and entities
    that were already correct. Idempotent: healing an escaped message leaves it
    byte-identical, so the retry can neither double-escape a label nor break a
    URL's `&amp;`.
    """
    healed = str(text)
    for entity, token in _HEAL_ENTITIES:
        healed = healed.replace(entity, token)
    healed = esc(healed)
    healed = healed.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    healed = healed.replace("&lt;/a&gt;", "</a>")
    healed = re.sub(r'&lt;a href="(.*?)"&gt;', r'<a href="\1">', healed, flags=re.S)
    for entity, token in _HEAL_ENTITIES:
        healed = healed.replace(token, entity)
    return healed


def _err(r):
    """Telegram's human-readable error description, best effort."""
    try:
        return r.json().get("description") or r.text[:200]
    except Exception:
        return r.text[:200]


def _is_parse_reject(r):
    return r.status_code == 400 and "parse entities" in r.text.lower()


def _post(url, timeout=20, **kw):
    """POST with one 429-flood-control retry (Telegram asks us to wait retry_after)."""
    try:
        r = requests.post(url, timeout=timeout, **kw)
        if r.status_code == 429:
            try:
                wait = min(float(r.json().get("parameters", {}).get("retry_after", 5)), 25.0)
            except Exception:
                wait = 5.0
            print(f"TG 429 flood control — waiting {wait}s then retry")
            time.sleep(wait)
            r = requests.post(url, timeout=timeout, **kw)
        return r
    except Exception as e:
        print("TG error:", e)
        return None


def send_message_to(tok, chat, text, silent=False):
    """Send one message to an explicit (token, chat) pair. True on success.

    On a 400 parse-entities reject, retries once with escaped text so a raw
    '&' (P&L, F&O, M&M, GVT&D) or '<' can no longer kill the whole alert.
    """
    url = f"{API}/bot{tok}/sendMessage"
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True, "disable_notification": bool(silent)}
    r = _post(url, json=payload)
    if r is None:
        print(f"TG_FAIL sendMessage chat={chat}: network/exception (see TG error above)")
        return False
    if _is_parse_reject(r):
        print(f"TG 400 parse reject chat={chat} ({_err(r)}) — retrying with escaped text")
        payload["text"] = _heal_html(text)
        r = _post(url, json=payload)
        if r is None:
            print(f"TG_FAIL sendMessage chat={chat}: network/exception on escaped retry")
            return False
    if not r.ok:
        print(f"TG_FAIL sendMessage chat={chat}: HTTP {r.status_code} — {_err(r)}")
        return False
    return True


def send_message(text, silent=False):
    """HTML-formatted message to the main chat. Returns True on success."""
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("TG_FAIL: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — message NOT sent")
        print(f"[TG-DRY] {text}")
        return False
    return send_message_to(tok, chat, text, silent=silent)


def send_document_to(tok, chat, path, caption=""):
    """Send one document to an explicit (token, chat) pair. True on success."""
    url = f"{API}/bot{tok}/sendDocument"
    try:
        with open(path, "rb") as f:
            r = _post(url, data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
                      files={"document": f}, timeout=60)
    except Exception as e:
        print(f"TG_FAIL sendDocument chat={chat}: {type(e).__name__}: {e}")
        return False
    if r is None:
        print(f"TG_FAIL sendDocument chat={chat}: network/exception (see TG error above)")
        return False
    if _is_parse_reject(r):
        print(f"TG 400 parse reject on caption chat={chat} ({_err(r)}) — retrying escaped")
        try:
            with open(path, "rb") as f:   # re-open: previous file object was consumed
                r = _post(url, data={"chat_id": chat, "caption": _heal_html(caption),
                                     "parse_mode": "HTML"}, files={"document": f}, timeout=60)
        except Exception as e:
            print(f"TG_FAIL sendDocument chat={chat}: {type(e).__name__}: {e}")
            return False
        if r is None:
            print(f"TG_FAIL sendDocument chat={chat}: network/exception on escaped retry")
            return False
    if not r.ok:
        print(f"TG_FAIL sendDocument chat={chat}: HTTP {r.status_code} — {_err(r)}")
        return False
    return True


def send_document(path, caption=""):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("TG_FAIL: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — document NOT sent")
        print(f"[TG-DRY] document {path} ({caption})")
        return False
    return send_document_to(tok, chat, path, caption)


def test():
    ok = send_message("✅ <b>Paper-test bot connected.</b>\nMaster Scanner + TOP-30 OI-spurt gate alerts will arrive here.")
    if not ok:
        print("TG_FAIL self-test — check: token from @BotFather (not regenerated since), "
              "chat_id from https://api.telegram.org/bot<TOKEN>/getUpdates, "
              "and that the bot was /start-ed and is not blocked.")
    return ok

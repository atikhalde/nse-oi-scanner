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
import time
import requests

API = "https://api.telegram.org"


def configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def esc(s):
    """Escape &, <, > for Telegram HTML parse mode."""
    return _html.escape(str(s), quote=False)


def _heal_html(text):
    """Escape every stray &, <, > but keep the only tags this codebase emits: <b></b>."""
    return esc(text).replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")


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

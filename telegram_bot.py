"""Telegram notifier. Credentials from env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID."""
import os
import time
import requests

API = "https://api.telegram.org"


def configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


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


def send_message(text, silent=False):
    """HTML-formatted message. Returns True on success, False (and prints) otherwise."""
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print(f"[TG-DRY] {text}")
        return False
    r = _post(f"{API}/bot{tok}/sendMessage",
              json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": True, "disable_notification": silent})
    if r is None:
        return False
    if not r.ok:
        print("TG sendMessage failed:", r.status_code, r.text[:200])
    return r.ok


def send_document(path, caption=""):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print(f"[TG-DRY] document {path} ({caption})")
        return False
    try:
        with open(path, "rb") as f:
            r = _post(f"{API}/bot{tok}/sendDocument",
                      data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
                      files={"document": f}, timeout=60)
        if r is None:
            return False
        if not r.ok:
            print("TG sendDocument failed:", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        print("TG doc error:", e)
        return False


def test():
    return send_message("✅ <b>Paper-test bot connected.</b>\nMaster Scanner + TOP-30 OI-spurt gate alerts will arrive here.")

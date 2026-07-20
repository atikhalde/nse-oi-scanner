"""Telegram notifier. Credentials from env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID."""
import os
import requests

API = "https://api.telegram.org"


def configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send_message(text, silent=False):
    """HTML-formatted message. Returns True on success, False (and prints) otherwise."""
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print(f"[TG-DRY] {text}")
        return False
    try:
        r = requests.post(f"{API}/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True, "disable_notification": silent},
                          timeout=20)
        if not r.ok:
            print("TG sendMessage failed:", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        print("TG error:", e)
        return False


def send_document(path, caption=""):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print(f"[TG-DRY] document {path} ({caption})")
        return False
    try:
        with open(path, "rb") as f:
            r = requests.post(f"{API}/bot{tok}/sendDocument",
                              data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
                              files={"document": f}, timeout=60)
        if not r.ok:
            print("TG sendDocument failed:", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        print("TG doc error:", e)
        return False


def test():
    return send_message("✅ <b>Paper-test bot connected.</b>\nMaster Scanner + TOP-30 OI-spurt gate alerts will arrive here.")

"""M13 three-target Telegram fanout with strict at-most-once reservation helpers.

This module has no trading logic. m13_runner.py must reserve deterministic keys in its
state before calling send_message/send_document.
"""
from __future__ import annotations

import os
from typing import Callable

import telegram_bot as tg

M13_INCLUDE_MAIN = True


def targets() -> list[tuple[str, str]]:
    """M13 A/B pairs preferred; matching complete M11 pairs are fallback."""
    out = []
    main_pair = (os.environ.get("TELEGRAM_BOT_TOKEN"),
                 os.environ.get("TELEGRAM_CHAT_ID"))
    for suffix in ("A", "B"):
        tok = os.environ.get(f"M13_BOT_TOKEN_{suffix}")
        chat = os.environ.get(f"M13_CHAT_ID_{suffix}")
        if not (tok and chat):
            tok = os.environ.get(f"M11_BOT_TOKEN_{suffix}")
            chat = os.environ.get(f"M11_CHAT_ID_{suffix}")
        if tok and chat and (tok, chat) != main_pair and (tok, chat) not in out:
            out.append((tok, chat))
    return out


def send_message(text: str, silent: bool = False) -> int:
    extra = targets(); to_main = M13_INCLUDE_MAIN or not extra
    if to_main:
        tg.send_message(text, silent=silent)
    for tok, chat in extra:
        try:
            tg._post(f"{tg.API}/bot{tok}/sendMessage",
                     data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": True,
                           "disable_notification": bool(silent)}, timeout=20)
        except Exception as exc:
            print(f"M13 extra target {chat}: message failed {type(exc).__name__}")
    return (1 if to_main else 0) + len(extra)


def send_document(path: str, caption: str = "") -> int:
    extra = targets(); to_main = M13_INCLUDE_MAIN or not extra
    if to_main:
        tg.send_document(path, caption=caption)
    for tok, chat in extra:
        try:
            with open(path, "rb") as f:
                tg._post(f"{tg.API}/bot{tok}/sendDocument",
                         data={"chat_id": chat, "caption": caption,
                               "parse_mode": "HTML"},
                         files={"document": f}, timeout=60)
        except Exception as exc:
            print(f"M13 extra target {chat}: document failed {type(exc).__name__}")
    return (1 if to_main else 0) + len(extra)


def reserve_once(st: dict, key: str, save_state: Callable[[dict], None]) -> bool:
    """Reserve and persist before network send. False means never send/retry."""
    alerts = st.setdefault("alerts", [])
    if key in alerts:
        return False
    alerts.append(key)
    save_state(st)
    return True


def reserve_batch(st: dict, keys: list[str], save_state: Callable[[dict], None]) -> set[str]:
    alerts = st.setdefault("alerts", [])
    new = {k for k in keys if k not in alerts}
    if new:
        alerts.extend(k for k in keys if k in new)
        save_state(st)
    return new


def test_alert() -> int:
    n = send_message("🧪 🅼13 TEST — three-target fanout is connected. "
                     "No trading state or order was created.")
    print(f"M13 test alert dispatched to {n} target(s): "
          f"main={'yes' if (M13_INCLUDE_MAIN or not targets()) else 'no'} "
          f"extras={len(targets())}")
    return n


if __name__ == "__main__":
    test_alert()

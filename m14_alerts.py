"""M14 three-target Telegram fanout with strict at-most-once reservation helpers."""
from __future__ import annotations

import os
from typing import Callable

import telegram_bot as tg

M14_INCLUDE_MAIN = True


def targets() -> list[tuple[str, str]]:
    """M14 A/B pairs preferred; matching complete M13 or M11 pairs are fallback."""
    out = []
    main_pair = (os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))
    for suffix in ("A", "B"):
        tok = os.environ.get(f"M14_BOT_TOKEN_{suffix}") or os.environ.get(f"M13_BOT_TOKEN_{suffix}") or os.environ.get(f"M11_BOT_TOKEN_{suffix}")
        chat = os.environ.get(f"M14_CHAT_ID_{suffix}") or os.environ.get(f"M13_CHAT_ID_{suffix}") or os.environ.get(f"M11_CHAT_ID_{suffix}")
        if tok and chat and (tok, chat) != main_pair and (tok, chat) not in out:
            out.append((tok, chat))
    return out


def send_message(text: str, silent: bool = False) -> int:
    """Fan out to main + extra targets; returns count of SUCCESSFUL sends."""
    extra = targets()
    to_main = M14_INCLUDE_MAIN or not extra
    sent = 0
    if to_main and tg.send_message(text, silent=silent):
        sent += 1
    for tok, chat in extra:
        try:
            if tg.send_message_to(tok, chat, text, silent=silent):
                sent += 1
        except Exception as exc:
            print(f"M14 TG_FAIL extra target {chat}: {type(exc).__name__}: {exc}")
    return sent


def send_document(path: str, caption: str = "") -> int:
    """Fan out a document to main + extra targets; returns SUCCESSFUL sends."""
    extra = targets()
    to_main = M14_INCLUDE_MAIN or not extra
    sent = 0
    if to_main and tg.send_document(path, caption=caption):
        sent += 1
    for tok, chat in extra:
        try:
            if tg.send_document_to(tok, chat, path, caption=caption):
                sent += 1
        except Exception as exc:
            print(f"M14 TG_FAIL extra target {chat}: document failed {type(exc).__name__}")
    return sent


def reserve_once(st: dict, key: str, save_state: Callable[[dict], None]) -> bool:
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
    n = send_message("🧪 🅼14 TEST — three-target fanout is connected. "
                     "No trading state or order was created.")
    print(f"M14 test alert delivered to {n} target(s)")
    return n


if __name__ == "__main__":
    test_alert()

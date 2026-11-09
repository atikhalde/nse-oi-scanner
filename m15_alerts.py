"""M15 three-target Telegram fanout helpers (mirrors M12/M13/M14 pattern)."""
from __future__ import annotations
import os
from typing import Callable

# Reuse m15_runner fanout logic for consistency
from m15_runner import _fanout_targets, _fanout_send  # type: ignore
import telegram_bot as tg  # legacy helper for document fanout if needed

def targets() -> list[tuple[str, str]]:
    return _fanout_targets()

def send_message(text: str, silent: bool = False) -> int:
    # For M15, silent flag is ignored (nse_scanner uses HTML); map to fanout
    return _fanout_send(text, dry_run=False)

def send_document(path: str, caption: str = "") -> int:
    """Fanout document to main + extra targets via legacy telegram_bot helpers."""
    extra = _fanout_targets()
    main_pair = (os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))
    sent = 0
    try:
        if tg.send_document(path, caption=caption):
            sent += 1
    except Exception as e:
        print(f"M15 doc FAIL main: {e}")
    for tok, chat in extra:
        try:
            if tg.send_document_to(tok, chat, path, caption=caption):
                sent += 1
        except Exception as e:
            print(f"M15 doc FAIL {chat}: {e}")
    return sent

def test_alert() -> int:
    n = _fanout_send("🧪 M15 TEST — fanout helper is connected. No position opened.")
    print(f"M15 test helper delivered to {n} target(s)")
    return n

if __name__ == "__main__":
    test_alert()

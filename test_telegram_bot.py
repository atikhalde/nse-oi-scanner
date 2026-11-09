#!/usr/bin/env python3
"""telegram_bot: 400 'can't parse entities' must trigger one escaped retry.

Regression guard for the alert blackout where raw '&' in "P&L" / "F&O" /
symbols M&M / GVT&D made Telegram reject whole HTML messages.
"""
import os
import types

import telegram_bot as tg


def ok(name, cond):
    assert cond, name
    print("PASS", name)


class FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


def main():
    calls = []
    old_post = tg.requests.post
    saved = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    try:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID"] = "chat"

        # 1) parse-reject then success -> True, second attempt escaped, <b> kept
        def fake_post(url, timeout=20, **kw):
            calls.append(kw["json"]["text"])
            if len(calls) == 1:
                return FakeResp(400, {"description": "Bad Request: can't parse entities"},
                                "can't parse entities")
            return FakeResp(200, {"ok": True})
        tg.requests.post = fake_post
        ok("parse-reject heals and succeeds",
           tg.send_message("<b>M&M</b> P&L ₹+100") is True)
        ok("exactly one retry", len(calls) == 2)
        ok("ampersand escaped", "P&amp;L" in calls[1] and "M&amp;M" in calls[1])
        ok("bold tags preserved", "<b>M&amp;M</b>" in calls[1])

        # 2) non-parse failure -> False, single attempt, loud TG_FAIL line
        calls.clear()
        def fake_401(url, timeout=20, **kw):
            calls.append(kw["json"]["text"])
            return FakeResp(401, {"description": "Unauthorized"}, "Unauthorized")
        tg.requests.post = fake_401
        ok("bad token fails honestly", tg.send_message("x") is False and len(calls) == 1)

        # 3) network exception -> False
        calls.clear()
        def boom(url, timeout=20, **kw):
            raise OSError("connection reset")
        tg.requests.post = boom
        ok("network error fails honestly", tg.send_message("x") is False)

        # 4) no credentials -> False and does not call requests
        calls.clear()
        os.environ.pop("TELEGRAM_BOT_TOKEN")
        tg.requests.post = lambda url, timeout=20, **kw: calls.append(url)
        ok("missing secrets never hit network", tg.send_message("x") is False and not calls)

        # 5) _heal_html unit checks
        ok("heal escapes angle brackets", tg._heal_html("a < b > c") == "a &lt; b &gt; c")
        ok("heal keeps bold only",
           tg._heal_html("<b>X</b> F&O") == "<b>X</b> F&amp;O")
    finally:
        tg.requests.post = old_post
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ALL TELEGRAM_BOT TESTS PASSED")


if __name__ == "__main__":
    main()

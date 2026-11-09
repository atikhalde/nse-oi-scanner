import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import os

import pandas as pd

ROOT = Path(__file__).resolve().parent

def test_imports():
    import nse_scanner.data as d
    import nse_scanner.market as m
    import nse_scanner.stock as s
    import nse_scanner.ta as ta
    import nse_scanner.telegram as tg
    import nse_scanner.pipeline as p
    assert hasattr(d, "load_universe")
    assert hasattr(m, "run_market")
    assert hasattr(s, "scan_stock")
    assert hasattr(ta, "ema")
    assert hasattr(tg, "send")
    assert hasattr(p, "run_once")

def test_universe_loads():
    import nse_scanner.data as d
    uni = d.load_universe()
    assert "nifty" in uni and "sectors" in uni
    assert uni["nifty"] == "^NSEI"
    assert "IT" in uni["sectors"]
    assert len(uni["sectors"]["IT"]["stocks"]) >= 5

def test_state_isolated():
    import nse_scanner.state as st
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "state15.json"
        st.STATE_PATH = p
        assert st.load() == {}
        st.save({"session": "2026-11-09", "market": {}, "sector": {}, "sent": {}, "positions": {}, "alerts": {}})
        loaded = st.load()
        assert loaded["session"] == "2026-11-09"
        # corrupt should raise InvalidStateError not silently reset
        p.write_text('{"session": "2026-11-09", "sent": ', encoding="utf-8")
        try:
            st.load()
            assert False, "expected InvalidStateError"
        except st.InvalidStateError:
            pass
        # restore
        st.STATE_PATH = ROOT / "state" / "state15.json"

def test_m15_runner_import_and_fanout():
    import m15_runner
    assert hasattr(m15_runner, "run_cycle")
    assert hasattr(m15_runner, "_fanout_targets")
    # env fanout empty should return list
    with patch.dict(os.environ, {}, clear=False):
        # clear M15 keys
        env = {k: "" for k in list(os.environ.keys()) if k.startswith("M15_") or k.startswith("M14_")}
        with patch.dict(os.environ, env, clear=False):
            targets = m15_runner._fanout_targets()
            assert isinstance(targets, list)

def test_m15_state_alias_paths():
    from pathlib import Path
    import m15_runner
    # Ensure alias sync creates files without error (dry-run doesn't need Telegram)
    assert m15_runner.STATE15 is not None
    assert m15_runner.STATE15_ALT is not None

def test_ta_ema_basic():
    import nse_scanner.ta as ta
    import numpy as np
    arr = np.array([10.0]*20 + [20.0]*10)
    e = ta.ema(arr, 9)
    assert len(e) == len(arr)
    assert e[-1] > 10

def test_telegram_secondary_not_required():
    import nse_scanner.telegram as tg
    tok, chat = tg.config_from_env()
    # Should not crash even if missing
    assert isinstance(tok, str)




def test_live_cycle_fanout_and_restore():
    import m15_runner as runner
    original = runner.tg.send
    env = {"TELEGRAM_BOT_TOKEN": "primary", "TELEGRAM_CHAT_ID": "chat1",
           "TELEGRAM_BOT_TOKEN_2": "secondary", "TELEGRAM_CHAT_ID_2": "chat2",
           "M15_BOT_TOKEN_A": "secondary", "M15_CHAT_ID_A": "chat2",
           "M15_BOT_TOKEN_B": "third", "M15_CHAT_ID_B": "chat3"}
    def pipeline(**kwargs):
        assert runner.tg.send("market alert", parse_mode="HTML") is True
        return {"signals": []}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(runner, "run_once", side_effect=pipeline), \
         patch.object(runner.tg, "_send_to", return_value=True) as send, \
         patch.object(runner.m15_state, "load", return_value={}):
        runner.run_cycle()
        assert send.call_count == 3
        assert [call.args[:2] for call in send.call_args_list] == [
            ("primary", "chat1"), ("secondary", "chat2"), ("third", "chat3")]
        assert runner.tg.send is original


def test_fanout_failures_and_fallback_pairs():
    import m15_runner as runner
    env = {"M15_BOT_TOKEN_A": "incomplete", "M14_BOT_TOKEN_A": "other",
           "M14_CHAT_ID_A": "other-chat"}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(runner.tg, "_send_to", return_value=False) as send:
        assert runner._fanout_targets() == [("other", "other-chat")]
        assert runner._fanout_send("test") == 0
        assert send.call_count == 1
    with patch.dict(os.environ, {}, clear=True):
        assert runner._fanout_send("test") == 0


def test_cycle_restores_dry_run_and_sender_on_failure():
    import m15_runner as runner
    original = runner.tg.send
    with patch.dict(os.environ, {}, clear=True), \
         patch.object(runner, "run_once", side_effect=RuntimeError("feed failed")), \
         patch.object(runner, "_save_state_alias") as save:
        try:
            runner.run_cycle(dry_run=True)
            assert False, "cycle should propagate failures"
        except RuntimeError:
            pass
        assert "DRY_RUN" not in os.environ
        assert runner.tg.send is original
        save.assert_not_called()


def test_dry_run_does_not_write_state():
    import m15_runner as runner
    def pipeline(**kwargs):
        assert os.environ["DRY_RUN"] == "1"
        assert runner.tg.send("test", dry_run=True)
        return {"signals": []}
    with patch.dict(os.environ, {}, clear=True), \
         patch.object(runner, "run_once", side_effect=pipeline), \
         patch.object(runner, "_save_state_alias") as save:
        runner.run_cycle(dry_run=True)
        save.assert_not_called()
        assert "DRY_RUN" not in os.environ


def test_actual_pipeline_market_alert_and_state():
    import datetime as dt
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    import m15_runner as runner
    import nse_scanner.pipeline as pipeline
    now = dt.datetime(2026, 11, 9, 10, 2, tzinfo=ZoneInfo("Asia/Kolkata"))
    bar = now.replace(hour=9, minute=55, second=0)
    frame = pd.DataFrame({"Close": [100.0]}, index=pd.DatetimeIndex([bar]))
    md = SimpleNamespace(dir_label="NEUTRAL", dir_conf=20, dir_state=0,
                         pick_sector="WAIT", pick_side=0, pick_reason="no sector",
                         action="WAIT", bar_time="2026-11-09 09:55", regime="CHOP")
    env = {"TELEGRAM_BOT_TOKEN": "primary", "TELEGRAM_CHAT_ID": "chat"}
    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, env, clear=True), \
         patch.object(runner.m15_state, "STATE_PATH", Path(td) / "state.json"), \
         patch.object(runner, "STATE15", Path(td) / "alias.json"), \
         patch.object(pipeline, "now_ist", return_value=now), \
         patch.object(pipeline.data, "load_universe", return_value={"nifty": "N", "vix": "V", "sectors": {}}), \
         patch.object(pipeline.data, "fetch_symbol", return_value={"m5": frame, "d1": frame}), \
         patch.object(pipeline.data, "fetch_many", return_value={}), \
         patch.object(pipeline, "run_market", return_value=md), \
         patch.object(pipeline, "format_market", return_value="market alert"), \
         patch.object(runner.tg, "_send_to", return_value=True) as send:
        out = runner.run_cycle()
        assert out["market"] == "NEUTRAL" and out["action"] == "WAIT"
        assert send.call_count == 1
        assert runner.m15_state.load()["market"]["label"] == "NEUTRAL"
        runner.run_cycle()
        assert send.call_count == 1, "same market alert must not repeat"


def test_all_delivery_failures_are_visible():
    import m15_runner as runner
    env = {"TELEGRAM_BOT_TOKEN": "primary", "TELEGRAM_CHAT_ID": "chat"}
    def pipeline(**kwargs):
        runner.tg.send("market alert")
    with patch.dict(os.environ, env, clear=True), \
         patch.object(runner, "run_once", side_effect=pipeline), \
         patch.object(runner.tg, "_send_to", return_value=False):
        try:
            runner.run_cycle()
            assert False, "total delivery failure should fail the cycle"
        except runner.TelegramDeliveryError:
            pass


def test_cli_reports_missing_targets():
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not (k.startswith("TELEGRAM_") or
           k.startswith(("M15_", "M14_", "M13_", "M11_")) or k == "DRY_RUN")}
    for args in (["--test-alert"], ["--loop", "1"]):
        result = subprocess.run([sys.executable, str(ROOT / "m15_runner.py"), *args],
                                env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode != 0, (args, result.stdout, result.stderr)


if __name__ == "__main__":
    test_imports()
    test_universe_loads()
    test_state_isolated()
    test_m15_runner_import_and_fanout()
    test_m15_state_alias_paths()
    test_ta_ema_basic()
    test_telegram_secondary_not_required()
    test_live_cycle_fanout_and_restore()
    test_fanout_failures_and_fallback_pairs()
    test_cycle_restores_dry_run_and_sender_on_failure()
    test_dry_run_does_not_write_state()
    test_actual_pipeline_market_alert_and_state()
    test_all_delivery_failures_are_visible()
    test_cli_reports_missing_targets()
    print("test_m15_runner OK")

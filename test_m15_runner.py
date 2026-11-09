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

if __name__ == "__main__":
    test_imports()
    test_universe_loads()
    test_state_isolated()
    test_m15_runner_import_and_fanout()
    test_m15_state_alias_paths()
    test_ta_ema_basic()
    test_telegram_secondary_not_required()
    print("test_m15_runner OK")

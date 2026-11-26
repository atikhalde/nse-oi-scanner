"""Real file-lock/cross-process cooldown tests; zero external requests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import feeds
import m16_transport as T


class TransportTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'budget.json'
        env=patch.dict(os.environ, {'M16_YAHOO_COORDINATE':'1','M16_YAHOO_BUDGET_FILE':str(self.path)})
        env.start();self.addCleanup(env.stop)

    def test_sector_and_stock_install_is_idempotent(self):
        with patch.object(feeds,'fetch_bars_yahoo',return_value='frame') as raw:
            T.install();wrapped=feeds.fetch_bars_yahoo
            T.install();self.assertIs(wrapped, feeds.fetch_bars_yahoo)
            with patch.object(T,'acquire',return_value=0):
                self.assertEqual(wrapped('TEST'),'frame')
            raw.assert_called_once()

    def test_429_blocks_another_process_and_honors_retry_after(self):
        T.trip(120)
        data=json.loads(self.path.read_text())
        self.assertGreaterEqual(data['cooldown_until']-data['last_429'],120)
        p=subprocess.run([sys.executable,'-c','import m16_transport as T; assert T.acquire()>0; print("blocked")'],capture_output=True,text=True,check=True)
        self.assertIn('blocked',p.stdout)

    def test_first_429_stops_next_call_without_network(self):
        def limited(*args,**kwargs):
            kwargs['on_error'](dict(http_status=429,retry_after=90,reason='rate_limited',host='query1'))
            return None
        with patch.object(feeds,'fetch_bars_yahoo',side_effect=limited) as raw:
            T.install();callback=[]
            feeds.fetch_bars_yahoo('FIRST',on_error=callback.append)
            feeds.fetch_bars_yahoo('SECOND',on_error=callback.append)
            self.assertEqual(raw.call_count,1)
            self.assertEqual(callback[-1]['host'],'shared-m16-budget')

    def test_repeated_429_increases_backoff(self):
        T.trip(0);one=json.loads(self.path.read_text())
        T.trip(0);two=json.loads(self.path.read_text())
        self.assertGreater(two['cooldown_until'],one['cooldown_until'])

    def test_pacing_reserves_across_callers(self):
        with patch.object(T.time,'time',return_value=100):
            self.assertEqual(T.acquire(),0)
        data=json.loads(self.path.read_text())
        self.assertGreater(data['next'],100)

    def test_m6_default_does_not_install_wrapper(self):
        with patch.dict(os.environ,{'M16_YAHOO_COORDINATE':'0'}), patch.object(feeds,'fetch_bars_yahoo') as raw:
            T.install()
            self.assertIs(feeds.fetch_bars_yahoo,raw)

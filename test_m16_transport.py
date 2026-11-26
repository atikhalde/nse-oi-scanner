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


def worker_start_method():
    # Resolve the child's own default context, not a pickled bound method
    # carrying the parent's DefaultContext object.
    import multiprocessing
    return multiprocessing.get_start_method()


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

    def test_explicit_unlock_works_while_forked_child_keeps_fd_open(self):
        # Reproduce the exact inherited-open-file hazard in an isolated
        # subprocess, so a regression cannot hang the test runner itself.
        script = r'''
import fcntl, os, time
import m16_transport as T
pid = None
ready_r, ready_w = os.pipe()
release_r, release_w = os.pipe()
def fork_inside_lock(data):
    global pid
    pid = os.fork()
    if pid == 0:
        os.close(ready_r); os.close(release_w)
        os.write(ready_w,b'1')
        os.read(release_r,1)
        os._exit(0)
    os.close(ready_w); os.close(release_r)
    os.read(ready_r,1)
    data['test'] = True
try:
    T.update(fork_inside_lock)
    # Child is alive with inherited fd. Parent must still acquire the lock.
    with T.state_path().open('a+') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        fcntl.flock(handle,fcntl.LOCK_UN)
    print('unlocked with inherited child alive')
finally:
    if pid:
        os.write(release_w,b'1')
        os.waitpid(pid,0)
'''
        p=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('unlocked with inherited child alive',p.stdout)

    def test_lock_wait_is_bounded_and_status_does_not_freeze_heartbeat(self):
        import fcntl
        with self.path.open('a+') as locked:
            fcntl.flock(locked,fcntl.LOCK_EX)
            with patch.object(T.time,'monotonic',side_effect=[0,3]), patch.object(T.time,'sleep'):
                with self.assertRaisesRegex(TimeoutError,'budget lock'):
                    T.update(lambda data:None)
            with patch.object(T.time,'monotonic',side_effect=[0,3]), patch.object(T.time,'sleep'):
                self.assertIn('error',T.status())
            fcntl.flock(locked,fcntl.LOCK_UN)

    def test_callback_exception_still_releases_lock(self):
        def broken(data):raise ValueError('test failure')
        with self.assertRaises(ValueError):T.update(broken)
        self.assertEqual(T.update(lambda data:'available'),'available')

    def test_real_spawn_worker_can_use_budget_after_parent_lock_closes(self):
        import fcntl
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=1,mp_context=multiprocessing.get_context('spawn')) as pool:
            with self.path.open('a+') as locked:
                fcntl.flock(locked,fcntl.LOCK_EX)
                method=pool.submit(worker_start_method).result(timeout=15)
                self.assertEqual(method,'spawn')
                # Deliberately close without explicit unlock: spawn workers
                # must never inherit this parent's open-file description.
            self.assertEqual(pool.submit(T.acquire).result(timeout=5),0.)

    def test_empty_backoff_is_per_symbol_shared_with_process_and_recovers(self):
        def empty(*args, **kwargs):
            for host in ('query1', 'query2'):
                kwargs['on_error'](dict(host=host, reason='empty_response', http_status=200))
                kwargs['log'](f'yahoo MISSING: empty response on {host}; trying next host')
            return None
        messages, errors = [], []
        with patch.object(feeds, 'fetch_bars_yahoo', side_effect=empty) as raw, patch.object(
                T, 'acquire', return_value=0), patch.object(T.time, 'time', return_value=100):
            T.install()
            self.assertIsNone(feeds.fetch_bars_yahoo('MISSING', '1d', log=messages.append))
            self.assertIsNone(feeds.fetch_bars_yahoo('MISSING', '5d', log=messages.append))
            self.assertEqual(raw.call_count, 2)  # first fallback range remains allowed
            feeds.fetch_bars_yahoo('MISSING', log=messages.append, on_error=errors.append)
            self.assertEqual(raw.call_count, 2)
            self.assertEqual(errors[-1]['reason'], 'empty response backoff')
            self.assertIsNone(errors[-1]['http_status'])  # not a fake HTTP 429
            self.assertEqual(T.status()['empty_backoffs'], {'MISSING': 30.0})
            feeds.fetch_bars_yahoo('HEALTHY', log=messages.append)
            self.assertEqual(raw.call_count, 3)
        self.assertEqual(len([m for m in messages if 'MISSING' in m]), 2)
        self.assertTrue(all('trying next host' not in m for m in messages))
        # Real second process reads the same circuit without a Yahoo request.
        p = subprocess.run([sys.executable, '-c',
            "import m16_transport as T; from unittest.mock import patch; "
            "clock=patch.object(T.time,'time',return_value=110); clock.start(); "
            "assert T.status()['empty_backoffs']['MISSING']==20; print('shared')"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        with patch.object(feeds, 'fetch_bars_yahoo', return_value='recovered') as raw, patch.object(
                T, 'acquire', return_value=0), patch.object(T.time, 'time', return_value=131):
            T.install()
            self.assertEqual(feeds.fetch_bars_yahoo('MISSING', log=messages.append), 'recovered')
            self.assertNotIn('MISSING', T.status()['empty_backoffs'])
        self.assertIn('data recovered', messages[-1])

    def test_empty_backoff_grows_to_five_minutes_and_history_is_not_blocked(self):
        def empty(*args, **kwargs):
            kwargs['on_error'](dict(host='query2', reason='empty_response', http_status=200))
            return None
        with patch.object(feeds, 'fetch_bars_yahoo', side_effect=empty) as raw, patch.object(
                T, 'acquire', return_value=0), patch.object(T.time, 'time', return_value=1000), patch('builtins.print'):
            T.install()
            for i in range(9):
                T.update(lambda d: d.get('empty_symbols', {}).get('TEST', {}).update(until=0))
                feeds.fetch_bars_yahoo('TEST')
            rec = json.loads(self.path.read_text())['empty_symbols']['TEST']
            self.assertEqual(rec['until']-1000, 300)
            before = raw.call_count
            feeds.fetch_bars_yahoo('TEST', '60d')
            self.assertEqual(raw.call_count, before+1)

    def test_shared_default_logging_unchanged_and_m16_coalesces_real_empty_response(self):
        from unittest.mock import MagicMock
        response = MagicMock(status_code=200)
        response.json.return_value = {'chart': {'result': [{'timestamp': []}]}}
        session = MagicMock()
        session.get.return_value = response
        original = feeds.fetch_bars_yahoo
        with patch.object(feeds, '_yahoo_session', return_value=(session, None)), patch('builtins.print') as log:
            original('TEST')
            self.assertEqual(log.call_count, 2)
        with patch.object(feeds, 'fetch_bars_yahoo', original), patch.object(
                feeds, '_yahoo_session', return_value=(session, None)), patch.object(
                T, 'acquire', return_value=0), patch('builtins.print') as log:
            T.install()
            feeds.fetch_bars_yahoo('TEST')
            feeds.fetch_bars_yahoo('TEST', '5d')
            feeds.fetch_bars_yahoo('TEST')
            self.assertEqual(log.call_count, 2)
            self.assertIn('retrying in 30s', log.call_args.args[0])

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

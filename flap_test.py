"""Regression tests for the Update 16 flapping-crash fix (2026-11-19).

Live symptom on https://the-quad-free.onrender.com, 2026-11-19
22:57-23:12 AEST: the homepage loads once after a wait/reload,
then the very next request (/register, then even / again) returns
Render 502 'service is currently unavailable', then after a few
minutes a reload wakes it again and the cycle repeats. Three
/register attempts all 502d. Earlier that evening: one
wal_insert_begin failed at 22:48, and one FATAL at 22:53 'app
did not become healthy within 60s; not starting nginx' followed
by a restart and clean boot at 22:53:35.

Root cause, proven in dev against a local sqld primary with
libsql-experimental 0.0.55 (the exact live driver): concurrent
write transactions on the embedded replica serialize
catastrophically. 10 threads x 5 connect + BEGIN IMMEDIATE +
INSERT + SELECT + commit took 220.2s, only 11 of 50 succeeded,
39 failed with 'Remote SQlite failure: 2:0:Transaction
timed-out' (5s/15s), one op taking 115s — while the identical
read-only workload took 0.1s with 0 errors, and the identical
workload against plain sqlite3 took 0.1s. The replica forwards
writes to the primary, the primary allows one write transaction
at a time with a ~5s budget, and queued write transactions burn
that budget while waiting, then fail, and the app retries them,
re-queueing. Every page view is a write transaction (rate_limit
BEGIN IMMEDIATE + page_views INSERT + session touch UPDATEs), so
even a couple of simultaneous visitors wedge the site: requests
hang past nginx's 60s read timeout and Render answers 502,
while /healthz (read-only, fast) stays green, so start.sh and
Render both think the app is healthy. The queue eventually
drains, one request gets through, the next batch re-wedges it.

Fix (db_backend.py, Update 16): a single-writer lock in the
connection wrapper — at most one write transaction is ever in
flight against the replica. Reads never take the lock. A waiter
gives up after 10s with an error containing 'locked', so
rate_limit() treats it as transient and falls back to memory
instead of hanging. start.sh's health gate is also raised from
60s to 120s: a cold start has to pull the replica across the
Pacific (Oregon -> Sydney) and live proved 60s can kill a
healthy-but-slow start.

What this proves (dev only, temp DBs wiped after each run):
  * _is_write_sql classifies write vs read statements.
  * A write attempted while another thread holds the lock fails
    fast with a 'locked' error, and succeeds once the lock is
    released (the lock cannot wedge writers forever).
  * A failed write (UNIQUE violation) releases the lock via
    rollback, so the next write succeeds promptly.
  * Against a real sync primary (when one is reachable at
    QUAD_SQLD_URL, default http://127.0.0.1:8080): 8 threads x 5
    concurrent BEGIN IMMEDIATE write transactions all succeed
    and finish in well under 60s (pre-fix: 220s, 11/50 ok).
  * Reads stay concurrent and fast against the same primary.

The remote tests are skipped (not failed) when no sqld primary
is reachable — they need a sync server, and this VM has none by
default. Run one locally (sqld --http-listen-addr
127.0.0.1:8080) to exercise them; the lock unit tests always run.

Driver setup: same pattern as wal502_test.py — libsql is installed
into /tmp/quad-turso-venv on first run and the file re-runs itself
under that venv.

Usage:  cd ~/workspace/network-merge/dev && python3 flap_test.py
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent

# ---------------------------------------------------------- driver setup
try:
    import libsql_experimental  # noqa: F401
except ImportError:
    if os.environ.get('QUAD_TURSO_VENV_DONE') != '1':
        venv = Path('/tmp/quad-turso-venv')
        if not (venv / 'bin' / 'python').exists():
            subprocess.run([sys.executable, '-m', 'venv', str(venv)],
                           check=True)
            subprocess.run([str(venv / 'bin' / 'pip'), 'install', '-q',
                            'libsql-experimental==0.0.55'], check=True)
        env = dict(os.environ)
        env['QUAD_TURSO_VENV_DONE'] = '1'
        env['PYTHONPATH'] = str(ROOT)
        sys.exit(subprocess.run(
            [str(venv / 'bin' / 'python'), str(Path(__file__).resolve())],
            env=env).returncode)
    raise

sys.path.insert(0, str(ROOT))
import db_backend

CHECKS = []
SKIPPED = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))
    if not cond:
        print('FAIL:', name)


def skip(name):
    SKIPPED.append(name)
    print('SKIP:', name)


def report():
    failed = [n for n, ok in CHECKS if not ok]
    print('flap_test: %d passed, %d failed, %d skipped'
          % (len(CHECKS) - len(failed), len(failed), len(SKIPPED)))
    for name, ok in CHECKS:
        if not ok:
            print('  FAILED:', name)
    return 1 if failed else 0


def local_conn(path):
    """A libSQL connection against a local file (no sync URL) —
    same wrapper and same single-writer lock as the live path,
    just without a remote primary."""
    return db_backend.connect_turso(path)


def remote_available(url):
    """True when a sync primary answers at url (short probe)."""
    import libsql_experimental as libsql
    probe = os.path.join(tempfile.mkdtemp(), 'probe.db')
    try:
        c = libsql.connect(probe, sync_url=url, auth_token='')
        c.execute('SELECT 1').fetchone()
        c.close()
        return True
    except Exception:
        return False


def main():
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / 'flap.db'

    # --- 1. write/read classification (no server needed) ----------
    for sql in ('INSERT INTO t VALUES (1)', 'insert into t values (1)',
                'UPDATE t SET x=1', 'DELETE FROM t', 'REPLACE INTO t VALUES (1)',
                'BEGIN IMMEDIATE', 'BEGIN', 'CREATE TABLE t(x)',
                'DROP TABLE t', 'ALTER TABLE t ADD COLUMN y',
                '  INSERT INTO t VALUES (1)'):
        check('write recognised: %s' % sql.strip()[:28],
              db_backend._is_write_sql(sql))
    for sql in ('SELECT 1', 'SELECT * FROM t WHERE x=1',
                'PRAGMA quick_check', 'PRAGMA foreign_keys=ON',
                'PRAGMA wal_checkpoint(TRUNCATE)'):
        check('read recognised: %s' % sql,
              not db_backend._is_write_sql(sql))

    # --- 2. lock timeout: waiter fails fast, then succeeds --------
    with local_conn(str(db_path)) as c:
        c.execute('CREATE TABLE IF NOT EXISTS t(x)')
    orig_timeout = db_backend._WRITE_LOCK_TIMEOUT_SECONDS
    db_backend._WRITE_LOCK_TIMEOUT_SECONDS = 2
    try:
        db_backend._WRITE_LOCK.acquire()
        try:
            err = []
            t0 = time.time()

            def try_write():
                try:
                    with local_conn(str(db_path)) as c2:
                        c2.execute('INSERT INTO t VALUES (1)')
                except Exception as exc:
                    err.append(exc)

            th = threading.Thread(target=try_write)
            th.start()
            th.join(timeout=10)
            dt = time.time() - t0
            check('write while lock held fails fast (<5s)', dt < 5)
            check('write while lock held raises a locked error',
                  err and 'locked' in str(err[0]).lower())
            check('write while lock held: thread finished', not th.is_alive())
        finally:
            db_backend._WRITE_LOCK.release()
        t0 = time.time()
        with local_conn(str(db_path)) as c:
            c.execute('INSERT INTO t VALUES (2)')
        check('write succeeds promptly after lock released',
              time.time() - t0 < 5)
    finally:
        db_backend._WRITE_LOCK_TIMEOUT_SECONDS = orig_timeout

    # --- 3. failed write releases the lock ------------------------
    with local_conn(str(db_path)) as c:
        c.execute('CREATE TABLE IF NOT EXISTS u(x UNIQUE)')
        c.execute("INSERT INTO u VALUES ('a')")
    try:
        with local_conn(str(db_path)) as c:
            c.execute("INSERT INTO u VALUES ('a')")
        check('duplicate insert raises', False)
    except Exception:
        check('duplicate insert raises', True)
    t0 = time.time()
    with local_conn(str(db_path)) as c:
        c.execute("INSERT INTO u VALUES ('b')")
    check('write after a failed write succeeds promptly',
          time.time() - t0 < 5)

    # --- 4/5. remote-primary tests (skipped without sqld) ---------
    sync_url = os.environ.get('QUAD_SQLD_URL', 'http://127.0.0.1:8080')
    # Local sqld builds (<= 0.24.32) reject PRAGMA busy_timeout /
    # synchronous / wal_checkpoint on a replica with 'unsupported
    # statement'; Turso cloud accepts them (live serves pages, and
    # connect_turso would raise on every connect otherwise). The
    # lock logic under test does not depend on those pragmas, so
    # tolerate them here — this patch is test-only, the shipped
    # _apply_pragmas is untouched.
    _real_pragmas = db_backend._apply_pragmas

    def _tolerant_pragmas(conn):
        for _sql in ('PRAGMA foreign_keys=ON',
                     'PRAGMA busy_timeout=30000',
                     'PRAGMA synchronous=NORMAL'):
            try:
                conn.execute(_sql)
            except Exception as exc:
                if 'unsupported statement' not in str(exc):
                    raise
    db_backend._apply_pragmas = _tolerant_pragmas
    try:
        return _remote_tests(tmp, sync_url)
    finally:
        db_backend._apply_pragmas = _real_pragmas


def _remote_tests(tmp, sync_url):
    if not remote_available(sync_url):
        skip('concurrent remote writes need sqld at %s' % sync_url)
        skip('concurrent remote reads need sqld at %s' % sync_url)
        return report()

    remote_path = tmp / 'remote.db'
    with db_backend.connect_turso(str(remote_path), sync_url=sync_url,
                                  auth_token='') as c:
        c.execute('CREATE TABLE IF NOT EXISTS t(x)')

    results = []

    def writer(n):
        for i in range(5):
            try:
                with db_backend.connect_turso(
                        str(remote_path), sync_url=sync_url,
                        auth_token='') as c:
                    c.execute('BEGIN IMMEDIATE')
                    c.execute('INSERT INTO t VALUES (?)', (n * 100 + i,))
                    c.execute('SELECT COUNT(*) FROM t').fetchone()
                results.append('ok')
            except Exception as exc:
                results.append('ERR %s: %s' % (type(exc).__name__, exc))

    t0 = time.time()
    ths = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    elapsed = time.time() - t0
    print('concurrent remote writes: %d/40 ok in %.1fs'
          % (results.count('ok'), elapsed))
    for r in results:
        if r != 'ok':
            print('  ', r)
    check('concurrent remote writes: all 40 succeed',
          results.count('ok') == 40)
    check('concurrent remote writes: finish in under 60s',
          elapsed < 60)

    read_results = []

    def reader():
        for _ in range(5):
            try:
                with db_backend.connect_turso(
                        str(remote_path), sync_url=sync_url,
                        auth_token='') as c:
                    c.execute('SELECT COUNT(*) FROM t').fetchone()
                read_results.append('ok')
            except Exception as exc:
                read_results.append('ERR %s' % exc)

    t0 = time.time()
    ths = [threading.Thread(target=reader) for _ in range(10)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    check('concurrent remote reads: all 50 succeed',
          read_results.count('ok') == 50)
    check('concurrent remote reads: finish in under 10s',
          time.time() - t0 < 10)

    return report()


if __name__ == '__main__':
    sys.exit(main())

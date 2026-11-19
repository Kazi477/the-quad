"""Regression tests for the live 502 of 2026-11-19 (Update 13).

What happened live: GET / returned 502 every time, while /healthz
returned ok. Every homepage request crashed in rate_limit()
(app.py line 490 in the live build): BEGIN IMMEDIATE + INSERT INTO
rate_limits raised sqlite3.Error: wal_insert_begin failed on the
Turso embedded replica, whose WAL was left broken after an unclean
Render spin-down. The exception escaped do_GET, the handler closed
the connection without a response, and Render/nginx answered 502.
/healthz only does SELECT 1, so it stayed green.

What this proves (dev only, temp DBs wiped after each run):
  * rate_limit() retries a transient WAL/lock failure, then falls
    back to in-memory counters instead of raising — and the
    in-memory fallback still enforces the limit.
  * A homepage GET still returns 200 when EVERY database write
    fails with wal_insert_begin (the exact live failure, injected
    by a wrapper around connect()), while /healthz stays 200.
  * db_backend heals a broken replica WAL on open: stale -wal/-shm
    sidecars are removed and the replica reopens, and the local
    file is NEVER deleted when there is no cloud database to
    rebuild it from (local/dev mode safety).
  * A real garbage -wal sidecar next to a healthy replica file
    does not stop the database opening or writing.

Driver setup: libsql-experimental is not installed system-wide.
If it is missing, this file creates /tmp/quad-turso-venv, installs
it there, and re-runs itself under that venv (once) — the same
pattern as render_free_test.py.

Usage:  cd ~/workspace/network-merge/dev && python3 wal502_test.py
"""

import http.client
import os
import re
import shutil
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

CHECKS = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))
    if not cond:
        print('FAIL:', name)


def report():
    failed = [n for n, ok in CHECKS if not ok]
    print('wal502_test: %d passed, %d failed'
          % (len(CHECKS) - len(failed), len(failed)))
    for name, ok in CHECKS:
        if not ok:
            print('  FAILED:', name)
    return 1 if failed else 0


# ---------------------------------------------------------- app import
def load_app(data):
    """Import the app module against a temp DATA_DIR (plain sqlite
    mode — no Turso env vars), and initialise the schema."""
    env = {'PORT': '18301', 'PRODUCTION': '0', 'TRUST_PROXY': '0',
           'DATA_DIR': str(data), 'APP_SECRET': 'wal502-test-secret',
           'ADMIN_EMAILS': 'mitchellwhite018@gmail.com',
           'PBKDF2_ITERATIONS': '10000'}
    for key, value in env.items():
        os.environ[key] = value
    os.environ.pop('TURSO_DATABASE_URL', None)
    os.environ.pop('TURSO_AUTH_TOKEN', None)
    if 'the_network_production' in sys.modules:
        del sys.modules['the_network_production']
    import the_network_production as app
    app.init_db()
    return app


class _FailingConn:
    """Wraps a real connection and makes every write fail with the
    exact live error, while reads pass through untouched. Used to
    reproduce the broken-replica WAL without a real Turso outage."""

    _WRITE_PREFIXES = ('INSERT', 'UPDATE', 'DELETE', 'BEGIN', 'REPLACE')

    def __init__(self, real, fail_times=None):
        self._real = real
        self._fail_times = fail_times  # None = fail forever

    def _maybe_fail(self, sql):
        text = str(sql).strip().upper()
        if text.startswith(self._WRITE_PREFIXES):
            if self._fail_times is None:
                raise sqlite3.Error('wal_insert_begin failed')
            if self._fail_times > 0:
                self._fail_times -= 1
                raise sqlite3.Error('wal_insert_begin failed')

    def execute(self, sql, params=()):
        self._maybe_fail(sql)
        return self._real.execute(sql, params)

    def executemany(self, sql, seq):
        self._maybe_fail(sql)
        return self._real.executemany(sql, seq)

    def executescript(self, sql):
        self._maybe_fail(sql)
        return self._real.executescript(sql)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        return self._real.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


def _failing_connect_factory(app, real_connect, fail_times=None):
    # The counter is shared across connections: each rate_limit
    # retry opens a fresh connection, so a per-connection counter
    # would fail forever instead of failing transiently.
    shared = {'left': fail_times}

    def failing_connect():
        return _FailingConnShared(real_connect(), shared)
    return failing_connect


class _FailingConnShared(_FailingConn):
    """Like _FailingConn, but the remaining failure count is shared
    across every connection the factory hands out."""

    def __init__(self, real, shared):
        super().__init__(real, fail_times=None)
        self._shared = shared

    def _maybe_fail(self, sql):
        text = str(sql).strip().upper()
        if text.startswith(self._WRITE_PREFIXES):
            if self._shared['left'] is None:
                raise sqlite3.Error('wal_insert_begin failed')
            if self._shared['left'] > 0:
                self._shared['left'] -= 1
                raise sqlite3.Error('wal_insert_begin failed')


# ---------------------------------------------------------- rate_limit
def rate_limit_tests():
    data = Path(tempfile.mkdtemp(prefix='quad-wal502-rate-'))
    try:
        app = load_app(data)
        app._RATE_LIMIT_MEMORY.clear()
        # Healthy DB first: the counter is stored in the DB.
        for _ in range(3):
            check('rate_limit: healthy DB allows the request',
                  app.rate_limit('healthy-check', 'client-a', 20, 60))
        with app.connect() as c:
            row = c.execute(
                'SELECT request_count FROM rate_limits '
                "WHERE action='healthy-check'").fetchone()
        check('rate_limit: healthy DB stores the counter (3)',
              row is not None and row['request_count'] == 3)

        # Now every write fails with the live error. rate_limit must
        # not raise, and must still enforce the limit from memory.
        real_connect = app.connect
        app.connect = _failing_connect_factory(app, real_connect)
        try:
            allowed = [app.rate_limit('view-log', 'client-b', 20, 60)
                       for _ in range(25)]
            check('rate_limit: wal failure never raises (25 calls)',
                  all(a is True for a in allowed[:20]))
            check('rate_limit: in-memory fallback still enforces '
                  'the limit (21st call denied)',
                  allowed[20] is False and all(a is False for a in allowed[20:]))
        finally:
            app.connect = real_connect
        # DB works again afterwards: a fresh action writes to the DB.
        check('rate_limit: DB usable again after the failure clears',
              app.rate_limit('after-failure', 'client-c', 20, 60))
        with app.connect() as c:
            row = c.execute(
                'SELECT request_count FROM rate_limits '
                "WHERE action='after-failure'").fetchone()
        check('rate_limit: counter back in the DB after recovery',
              row is not None and row['request_count'] == 1)

        # Transient failure: the first two writes fail, the third
        # succeeds — the retry must land the write in the DB.
        app.connect = _failing_connect_factory(app, real_connect,
                                               fail_times=2)
        try:
            ok = app.rate_limit('flaky-write', 'client-d', 20, 60)
        finally:
            app.connect = real_connect
        check('rate_limit: retry succeeds after transient wal failure',
              ok is True)
        with app.connect() as c:
            row = c.execute(
                'SELECT request_count FROM rate_limits '
                "WHERE action='flaky-write'").fetchone()
        check('rate_limit: retried write is in the DB',
              row is not None and row['request_count'] == 1)
    finally:
        shutil.rmtree(data, ignore_errors=True)


# ---------------------------------------------------------- HTTP end to end
def homepage_tests():
    data = Path(tempfile.mkdtemp(prefix='quad-wal502-http-'))
    try:
        app = load_app(data)
        app._RATE_LIMIT_MEMORY.clear()
        real_connect = app.connect
        # Patch connect AFTER init_db: from here on, every write in
        # the whole app fails with the live wal_insert_begin error,
        # exactly as on the broken live replica.
        app.connect = _failing_connect_factory(app, real_connect)
        server = app.NetworkHTTPServer(('127.0.0.1', 18302), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def get(path):
                conn = http.client.HTTPConnection('127.0.0.1', 18302,
                                                  timeout=5)
                conn.request('GET', path)
                resp = conn.getresponse()
                body = resp.read().decode('utf-8', 'replace')
                conn.close()
                return resp.status, body

            st, body = get('/healthz')
            check('http: /healthz is 200 while writes fail', st == 200)
            st, body = get('/')
            check('http: GET / is 200 while every write fails '
                  '(was 502 live)', st == 200)
            check('http: homepage renders The Campus while writes fail',
                  'Started in a dorm room' in body)
            st, body = get('/app.css')
            check('http: static /app.css is 200 while writes fail',
                  st == 200)
        finally:
            server.shutdown()
            server.server_close()
            app.connect = real_connect
    finally:
        shutil.rmtree(data, ignore_errors=True)


# ---------------------------------------------------------- db_backend WAL
def wal_recovery_tests():
    import db_backend
    data = Path(tempfile.mkdtemp(prefix='quad-wal502-db-'))
    try:
        db_path = data / 't.db'
        conn = db_backend.connect_turso(db_path, sync_url=None,
                                        auth_token='')
        conn.executescript('CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);')
        with conn as c:
            c.execute("INSERT INTO t(v) VALUES ('keep-me')")
        conn.close()

        # A garbage -wal/-shm next to a healthy file must not stop
        # the replica opening or writing (Render spin-down can leave
        # junk sidecars behind).
        (data / 't.db-wal').write_bytes(b'\x00\x01garbage-wal-bytes')
        (data / 't.db-shm').write_bytes(b'\x00\x01garbage-shm-bytes')
        db_backend._WAL_PROBE_DONE = False
        conn = db_backend.connect_turso(db_path, sync_url=None,
                                        auth_token='')
        with conn as c:
            c.execute("INSERT INTO t(v) VALUES ('after-garbage-wal')")
            rows = c.execute('SELECT v FROM t ORDER BY id').fetchall()
        check('wal: garbage -wal/-shm sidecars do not break open/write',
              [r['v'] for r in rows] == ['keep-me', 'after-garbage-wal'])
        conn.close()

        # The recovery path itself: force the WAL self-check to fail
        # once with the live error, then prove the replica is
        # reopened (stale sidecars removed) with its data intact.
        (data / 't.db-wal').write_bytes(b'stale-wal')
        (data / 't.db-shm').write_bytes(b'stale-shm')
        real_check = db_backend._wal_self_check
        calls = {'n': 0}

        def failing_check_once(c):
            calls['n'] += 1
            if calls['n'] == 1:
                raise sqlite3.Error('wal_insert_begin failed')
            return real_check(c)

        db_backend._wal_self_check = failing_check_once
        db_backend._WAL_PROBE_DONE = False
        try:
            conn = db_backend.connect_turso(db_path, sync_url=None,
                                            auth_token='')
        finally:
            db_backend._wal_self_check = real_check
        with conn as c:
            c.execute("INSERT INTO t(v) VALUES ('after-recovery')")
            rows = c.execute('SELECT v FROM t ORDER BY id').fetchall()
        check('wal: recovery reopens the replica and data survives',
              [r['v'] for r in rows] == ['keep-me', 'after-garbage-wal',
                                          'after-recovery'])
        check('wal: stale sidecars were removed by recovery',
              not (data / 't.db-wal').exists()
              and not (data / 't.db-shm').exists())
        conn.close()

        # Safety: if the WAL cannot be healed and there is NO cloud
        # database to rebuild from, the local file must NOT be
        # deleted — in local mode it is the only copy of the data.
        def failing_check_always(c):
            raise sqlite3.Error('wal_insert_begin failed')

        db_backend._wal_self_check = failing_check_always
        db_backend._WAL_PROBE_DONE = False
        try:
            db_backend.connect_turso(db_path, sync_url=None, auth_token='')
            healed = True
        except sqlite3.Error:
            healed = False
        finally:
            db_backend._wal_self_check = real_check
        check('wal: unhealable replica fails loudly in local mode',
              healed is False)
        check('wal: local file is NOT deleted when there is no '
              'cloud copy to rebuild from',
              db_path.exists())
        conn = db_backend.connect_turso(db_path, sync_url=None,
                                        auth_token='')
        rows = conn.execute('SELECT v FROM t ORDER BY id').fetchall()
        check('wal: data still intact after refused rebuild',
              [r['v'] for r in rows] == ['keep-me', 'after-garbage-wal',
                                          'after-recovery'])
        conn.close()
    finally:
        shutil.rmtree(data, ignore_errors=True)


if __name__ == '__main__':
    rate_limit_tests()
    homepage_tests()
    wal_recovery_tests()
    sys.exit(report())

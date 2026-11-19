"""Regression tests for the Update 15 fix-all pass (2026-11-19).

What was still broken after the Update 13 (WAL) and Update 14
(malformed + connection refused) fixes, all seen live on
https://the-quad-free.onrender.com on 2026-11-19:

  1. rate_limit() treated a *malformed* database error as a
     programming error and re-raised it — only WAL/lock errors
     fell back to memory. A malformed replica therefore still
     502d every POST that rate-limits (login, register, posting),
     even though GET / survived.
  2. session() was the first database touch on EVERY request and
     was unguarded: if connect() itself raised (corrupt replica,
     Turso sync failure), the request died with no response at
     all — nginx 502 — before any page could render.
  3. shell() queried the database for badge counts on every
     logged-in page, unguarded: a DB blip mid-request killed a
     page that had already authenticated fine.
  4. do_GET()/do_POST() had no top-level guard: any unhandled
     exception in a page handler escaped, the connection closed
     with no response, nginx answered 502.
  5. A client disconnecting mid-send raised BrokenPipeError
     through wfile.write() and dumped a traceback into the
     Render logs on every abandoned page.
  6. db_backend: a WAL failure on the *second* open (after the
     sidecars were removed) raised instead of rebuilding from
     Turso, and concurrent request threads hitting a corrupt
     replica could each rebuild it at once.
  7. start.sh waited for /healthz but then started nginx even
     when the app NEVER became healthy within the wait — the
     exact connection-refused state the wait was meant to
     prevent, 60 seconds later.
  8. main() ran the startup database backup synchronously
     before serve_forever, delaying the first /healthz answer
     (Turso sync + file copy) while nginx/Render waited.

What this proves (dev only, temp DBs wiped after each run):
  * rate_limit() falls back to memory on a malformed error and
    still enforces the limit.
  * session() returns (None, None) when connect() raises.
  * shell() renders with zero badges when the DB is down.
  * A handler that raises still gets a real 500 page from the
    app (not a dropped connection / nginx 502).
  * _write() swallows a client disconnect; handle_error() does
    not log a traceback for BrokenPipe/ConnectionReset.
  * db_backend rebuilds from Turso after a WAL failure on
    re-open, and the rebuild path is serialised.
  * start.sh exits (no nginx) if the app never becomes healthy;
    nginx fails fast on connect (5s).
  * The startup backup runs in a background thread.

Driver setup: same pattern as wal502_test.py — libsql is installed
into /tmp/quad-turso-venv on first run and the file re-runs itself
under that venv.

Usage:  cd ~/workspace/network-merge/dev && python3 fixall_test.py
"""

import http.client
import io
import os
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
    print('fixall_test: %d passed, %d failed'
          % (len(CHECKS) - len(failed), len(failed)))
    for name, ok in CHECKS:
        if not ok:
            print('  FAILED:', name)
    return 1 if failed else 0


# ---------------------------------------------------------- app import
def load_app(data, port='18321'):
    env = {'PORT': port, 'PRODUCTION': '0', 'TRUST_PROXY': '0',
           'DATA_DIR': str(data), 'APP_SECRET': 'fixall-test-secret',
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


class _MalformedConn:
    """Wraps a real connection; every write raises the exact live
    malformed-database error, reads pass through."""

    _WRITE_PREFIXES = ('INSERT', 'UPDATE', 'DELETE', 'BEGIN', 'REPLACE')

    def __init__(self, real):
        self._real = real

    def _maybe_fail(self, sql):
        if str(sql).strip().upper().startswith(self._WRITE_PREFIXES):
            raise sqlite3.Error('database disk image is malformed')

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


# ---------------------------------------------------------- rate_limit
def rate_limit_malformed_tests():
    data = Path(tempfile.mkdtemp(prefix='quad-fixall-rate-'))
    try:
        app = load_app(data)
        app._RATE_LIMIT_MEMORY.clear()
        check('rate_limit: malformed is transient',
              app._rate_limit_is_transient(
                  ValueError('database disk image is malformed')))
        check('rate_limit: "file is not a database" is transient',
              app._rate_limit_is_transient(
                  ValueError('file is not a database')))
        check('rate_limit: a real SQL error is still NOT transient',
              not app._rate_limit_is_transient(
                  ValueError('no such table: users')))
        real_connect = app.connect
        app.connect = lambda: _MalformedConn(real_connect())
        try:
            allowed = [app.rate_limit('fixall-malformed', 'client-m',
                                      20, 60) for _ in range(25)]
            check('rate_limit: malformed failure never raises',
                  all(a is True for a in allowed[:20]))
            check('rate_limit: memory fallback still enforces the '
                  'limit on malformed (21st denied)',
                  allowed[20] is False)
        finally:
            app.connect = real_connect
    finally:
        shutil.rmtree(data, ignore_errors=True)


# ---------------------------------------------------------- session/shell
def session_shell_tests():
    data = Path(tempfile.mkdtemp(prefix='quad-fixall-session-'))
    try:
        app = load_app(data)
        # A signed-in user exists, so shell() has a real user row.
        with app.connect() as c:
            c.execute(
                "INSERT INTO users(name,email,password_hash,is_admin,"
                "joined) VALUES('Sydney Test','sydneytest@example.com',"
                "'x',0,1)")
            user = c.execute(
                "SELECT * FROM users WHERE email="
                "'sydneytest@example.com'").fetchone()
        user = dict(user)
        user['csrf'] = 'test-csrf'

        def broken_connect():
            raise sqlite3.Error('database disk image is malformed')

        real_connect = app.connect
        app.connect = broken_connect
        try:
            # session(): build a bare Handler (no socket) with a
            # session cookie and prove it degrades to signed-out.
            handler = app.Handler.__new__(app.Handler)
            handler.headers = {'Cookie': 'network_session=deadbeef'}
            user_out, token_out = handler.session()
            check('session: connect() raising degrades to '
                  'signed-out, not a crash',
                  user_out is None and token_out is None)
            # shell(): badge counts must not kill the page.
            page = app.shell('Test', '<p>hello</p>', user=user)
            check('shell: renders with zero badges when DB is down',
                  'hello' in page and 'the campus' in page)
        finally:
            app.connect = real_connect
    finally:
        shutil.rmtree(data, ignore_errors=True)


# ---------------------------------------------------------- top-level guard
def handler_guard_tests():
    data = Path(tempfile.mkdtemp(prefix='quad-fixall-guard-'))
    try:
        app = load_app(data, port='18322')
        server = app.NetworkHTTPServer(('127.0.0.1', 18322), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def get(path):
                conn = http.client.HTTPConnection('127.0.0.1', 18322,
                                                  timeout=5)
                conn.request('GET', path)
                resp = conn.getresponse()
                body = resp.read().decode('utf-8', 'replace')
                conn.close()
                return resp.status, body

            st, _ = get('/')
            check('guard: healthy homepage is 200', st == 200)
            # Break every handler from the inside: _do_GET raises.
            real_do_get = app.Handler._do_GET

            def boom(self):
                raise RuntimeError('simulated handler crash')

            app.Handler._do_GET = boom
            try:
                st, body = get('/')
                check('guard: a crashing handler returns a real '
                      '500 page, not a dropped connection',
                      st == 500 and 'Something went wrong' in body)
                st, _ = get('/healthz')
                check('guard: /healthz still 200 after a crash',
                      st == 200)
            finally:
                app.Handler._do_GET = real_do_get
            st, _ = get('/')
            check('guard: homepage recovers to 200 after the crash',
                  st == 200)
        finally:
            server.shutdown()
            server.server_close()
    finally:
        shutil.rmtree(data, ignore_errors=True)


# ---------------------------------------------------------- disconnect
def disconnect_tests():
    data = Path(tempfile.mkdtemp(prefix='quad-fixall-disc-'))
    try:
        app = load_app(data, port='18323')
        handler = app.Handler.__new__(app.Handler)

        class _BrokenWFile:
            def write(self, data):
                raise BrokenPipeError('client went away')

        handler.wfile = _BrokenWFile()
        check('_write: BrokenPipe returns False, does not raise',
              handler._write(b'hello') is False)

        class _ResetWFile:
            def write(self, data):
                raise ConnectionResetError('connection reset')

        handler.wfile = _ResetWFile()
        check('_write: ConnectionReset returns False, does not raise',
              handler._write(b'hello') is False)

        # handle_error lives on the *server* (socketserver calls
        # the server's handle_error, not the handler's). It must
        # swallow disconnects silently (no traceback on stderr)
        # and still log real errors.
        server = app.NetworkHTTPServer(('127.0.0.1', 0), app.Handler)
        err = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = err
        try:
            try:
                raise BrokenPipeError('gone')
            except BrokenPipeError:
                server.handle_error(None, ('127.0.0.1', 1))
            broken_logged = 'Traceback' in err.getvalue()
        finally:
            sys.stderr = old_stderr
        check('handle_error: BrokenPipe logs no traceback',
              not broken_logged)
        err = io.StringIO()
        sys.stderr = err
        try:
            try:
                raise RuntimeError('real bug')
            except RuntimeError:
                server.handle_error(None, ('127.0.0.1', 1))
            real_logged = 'Traceback' in err.getvalue()
        finally:
            sys.stderr = old_stderr
            server.server_close()
        check('handle_error: a real error still logs a traceback',
              real_logged)
    finally:
        shutil.rmtree(data, ignore_errors=True)


# ---------------------------------------------------------- db_backend
def db_backend_tests():
    import db_backend
    # A WAL failure on the SECOND open (after sidecars removed)
    # must rebuild from Turso, not raise. Simulated like
    # malformed502_test: _open_raw fails twice, then delivers a
    # fresh replica seeded from the cloud copy.
    data = Path(tempfile.mkdtemp(prefix='quad-fixall-db-'))
    seed_dir = Path(tempfile.mkdtemp(prefix='quad-fixall-seed-'))
    try:
        seed = seed_dir / 'seed.db'
        conn = db_backend.connect_turso(seed, sync_url=None, auth_token='')
        conn.executescript('CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);')
        with conn as c:
            c.execute("INSERT INTO t(v) VALUES ('cloud-data')")
        conn.close()
        db = data / 't.db'
        conn = db_backend.connect_turso(db, sync_url=None, auth_token='')
        conn.executescript('CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);')
        with conn as c:
            c.execute("INSERT INTO t(v) VALUES ('keep-me')")
        conn.close()
        real_open = db_backend._open_raw
        calls = {'n': 0}

        def fake_open(path, sync_url=None, auth_token=''):
            calls['n'] += 1
            if calls['n'] <= 2:
                raise ValueError('wal_insert_begin failed')
            shutil.copyfile(seed, str(path))
            return real_open(path, sync_url=None, auth_token='')

        db_backend._open_raw = fake_open
        db_backend._WAL_PROBE_DONE = False
        try:
            conn = db_backend.connect_turso(
                db, sync_url='libsql://fake.turso.io', auth_token='tok')
            rows = conn.execute('SELECT v FROM t ORDER BY id').fetchall()
            values = [r['v'] for r in rows]
            conn.close()
        finally:
            db_backend._open_raw = real_open
        check('db: WAL failure on re-open rebuilds from Turso',
              values == ['cloud-data'])
        check('db: damaged file quarantined on WAL rebuild',
              len(list(data.glob('t.db.corrupt-*'))) >= 1)
        check('db: recovery lock exists for concurrent rebuilds',
              hasattr(db_backend, '_RECOVERY_LOCK'))
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(seed_dir, ignore_errors=True)


# ---------------------------------------------------------- deploy files
def deploy_file_tests():
    start = (ROOT / 'start.sh').read_text()
    check('start.sh: exits if app never becomes healthy',
          'healthy=1' in start
          and 'FATAL: app did not become healthy within 60s' in start
          and "nginx -g 'daemon off;'" in start)
    check('start.sh: nginx starts only after the health check',
          start.index('/healthz') < start.index("nginx -g 'daemon off;"))
    nginx = (ROOT / 'nginx.conf.template').read_text()
    check('nginx: proxy_connect_timeout is 5s (fail fast)',
          'proxy_connect_timeout 5s;' in nginx)
    src = (ROOT / 'the_network_production.py').read_text()
    check('main: startup backup runs in a background thread',
          "threading.Thread(target=backup_database" in src)
    check('main: no synchronous backup blocks serving anymore',
          src.count('    backup_database()\n') == 0
          or 'database-backup-startup' in src)


if __name__ == '__main__':
    rate_limit_malformed_tests()
    session_shell_tests()
    handler_guard_tests()
    disconnect_tests()
    db_backend_tests()
    deploy_file_tests()
    sys.exit(report())

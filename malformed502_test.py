"""Regression tests for the second live failure of 2026-11-19
(Update 14): connection refused on 127.0.0.1:8081 + malformed
SQLite database.

What happened live, after the WAL fix (ace2f6a) was redeployed:
the homepage loaded once and a dummy registration showed success,
then 502s came back. Mitchell spotted this in the Render logs:

    2026/11/19 12:14:47 [error] 10#10: *1 connect() failed
    (111: Connection refused) while connecting to upstream,
    client: 10.238.24.253, server: _, request: "GET /healthz
    HTTP/1.1", upstream: "http://127.0.0.1:8081/healthz",
    host: "the-quad-free.onrender.com"

and the app logs reported a malformed SQLite database. The dummy
account creation is UNCONFIRMED — it may or may not have landed.

Root cause, proved in dev: a local replica file whose header/pages
are torn by an unclean Render spin-down comes back as
"file is not a database" / "database disk image is malformed".
The ace2f6a WAL recovery only treated WAL/lock errors as
recoverable, so a corrupt file was NOT healed — and main() ran
init_db() + PRAGMA quick_check BEFORE binding its port, so the
process died before it ever listened on 8081. nginx was up,
nothing was listening, every request (including /healthz) got
connection refused, and the app could not even answer 503.

What this proves (dev only, temp DBs wiped after each run):
  * 'malformed' / 'file is not a database' are treated as
    recoverable corruption, exactly like the WAL errors.
  * A corrupt replica in local mode (no cloud copy) still fails
    loudly and the file is NOT deleted or quarantined away.
  * A corrupt replica WITH a Turso cloud copy is quarantined
    (as <file>.corrupt-<timestamp>, not deleted) and rebuilt
    from the cloud, with the cloud data present afterwards.
  * The startup self-check now runs PRAGMA quick_check: a
    non-'ok' result raises as corruption and triggers recovery.
  * ensure_database_healthy() rebuilds a damaged replica and
    the app then starts normally; without a cloud copy it
    raises loudly instead of serving on a broken DB.
  * A subprocess started on a corrupt plain-SQLite DATA_DIR
    exits non-zero, logs FATAL, and never serves /healthz —
    i.e. it fails loudly instead of hanging or 502ing forever.
  * A subprocess on a healthy DB binds first and serves
    /healthz 200 (the main() restructure did not break startup).
  * start.sh waits for the app to answer /healthz before
    starting nginx, so a startup race can no longer produce
    the connection-refused log line Mitchell saw.

Driver setup: same pattern as wal502_test.py — libsql is installed
into /tmp/quad-turso-venv on first run and the file re-runs itself
under that venv.

Usage:  cd ~/workspace/network-merge/dev && python3 malformed502_test.py
"""

import http.client
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
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
import db_backend  # noqa: E402

CHECKS = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))
    if not cond:
        print('FAIL:', name)


def report():
    failed = [n for n, ok in CHECKS if not ok]
    print('malformed502_test: %d passed, %d failed'
          % (len(CHECKS) - len(failed), len(failed)))
    for name, ok in CHECKS:
        if not ok:
            print('  FAILED:', name)
    return 1 if failed else 0


def _healthy_libsql_file(path):
    conn = db_backend.connect_turso(path, sync_url=None, auth_token='')
    conn.executescript('CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);')
    with conn as c:
        c.execute("INSERT INTO t(v) VALUES ('keep-me')")
    conn.close()


def _corrupt_header(path):
    b = bytearray(Path(path).read_bytes())
    b[0:100] = b'X' * 100
    Path(path).write_bytes(bytes(b))


# ---------------------------------------------------------- markers
def marker_tests():
    check('corrupt: "database disk image is malformed" is corrupt',
          db_backend._is_corrupt_error(
              ValueError('database disk image is malformed')))
    check('corrupt: "file is not a database" is corrupt',
          db_backend._is_corrupt_error(
              ValueError('file is not a database')))
    check('corrupt: corruption is also a recoverable DB error',
          db_backend._is_wal_error(
              ValueError('file is not a database')))
    check('corrupt: WAL errors still recoverable',
          db_backend._is_wal_error(
              ValueError('wal_insert_begin failed')))
    check('corrupt: a real SQL error is NOT recoverable',
          not db_backend._is_wal_error(
              ValueError('no such table: users'))
          and not db_backend._is_corrupt_error(
              ValueError('no such table: users')))


# ---------------------------------------------------------- db_backend
def db_backend_tests():
    # Local mode: corrupt file raises loudly, file untouched.
    data = Path(tempfile.mkdtemp(prefix='quad-malformed-local-'))
    try:
        db = data / 't.db'
        _healthy_libsql_file(db)
        _corrupt_header(db)
        db_backend._WAL_PROBE_DONE = False
        try:
            db_backend.connect_turso(db, sync_url=None, auth_token='')
            raised = False
        except Exception:
            raised = True
        check('db: corrupt replica in local mode raises loudly', raised)
        check('db: corrupt local file is NOT deleted', db.exists())
        check('db: corrupt local file is NOT quarantined away',
              not list(data.glob('*.corrupt-*')))
    finally:
        shutil.rmtree(data, ignore_errors=True)

    # Simulated Turso: _open_raw fails twice with the live error,
    # then delivers a fresh replica seeded from the cloud copy.
    data = Path(tempfile.mkdtemp(prefix='quad-malformed-cloud-'))
    seed_dir = Path(tempfile.mkdtemp(prefix='quad-malformed-seed-'))
    try:
        seed = seed_dir / 'seed.db'
        _healthy_libsql_file(seed)
        # Seed holds the cloud data (a different row from local).
        conn = db_backend.connect_turso(seed, sync_url=None, auth_token='')
        with conn as c:
            c.execute("INSERT INTO t(v) VALUES ('cloud-data')")
        conn.close()
        db = data / 't.db'
        _healthy_libsql_file(db)
        _corrupt_header(db)
        real_open = db_backend._open_raw
        calls = {'n': 0}

        def fake_open(path, sync_url=None, auth_token=''):
            calls['n'] += 1
            if calls['n'] <= 2:
                raise ValueError('file is not a database')
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
        check('db: corrupt replica rebuilds from Turso (cloud data)',
              values == ['keep-me', 'cloud-data'])
        check('db: damaged file is quarantined, not deleted',
              len(list(data.glob('t.db.corrupt-*'))) == 1)
        check('db: rebuilt file is a fresh local file', db.exists())
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(seed_dir, ignore_errors=True)

    # Self-check: quick_check != ok raises as corruption.
    class _FakeCur:
        def __init__(self, v):
            self.v = v

        def fetchone(self):
            return (self.v,)

    class _FakeConn:
        def __init__(self, v):
            self.v = v

        def execute(self, sql, params=()):
            return _FakeCur(self.v)

    try:
        db_backend._wal_self_check(_FakeConn('*** in database main ***'))
        raised = False
    except Exception as exc:
        raised = 'malformed' in str(exc).lower()
    check('db: quick_check failure raises as corruption', raised)
    try:
        db_backend._wal_self_check(_FakeConn('ok'))
        ok_passed = True
    except Exception:
        ok_passed = False
    check('db: quick_check ok passes the self-check', ok_passed)


# ---------------------------------------------------------- app gate
def _load_app(data, turso=False):
    env = {'PORT': '18311', 'PRODUCTION': '0', 'TRUST_PROXY': '0',
           'DATA_DIR': str(data), 'APP_SECRET': 'malformed-test-secret',
           'ADMIN_EMAILS': 'mitchellwhite018@gmail.com',
           'PBKDF2_ITERATIONS': '10000'}
    for key, value in env.items():
        os.environ[key] = value
    if turso:
        os.environ['TURSO_DATABASE_URL'] = 'libsql://fake.turso.io'
        os.environ['TURSO_AUTH_TOKEN'] = 'test-token'
    else:
        os.environ.pop('TURSO_DATABASE_URL', None)
        os.environ.pop('TURSO_AUTH_TOKEN', None)
    if 'the_network_production' in sys.modules:
        del sys.modules['the_network_production']
    import the_network_production as app
    return app


def ensure_healthy_tests():
    # No cloud copy: a corrupt DB must raise loudly, file intact.
    data = Path(tempfile.mkdtemp(prefix='quad-malformed-gate-local-'))
    try:
        app = _load_app(data, turso=False)
        app.init_db()
        db_path = app.DB_PATH
        # Grow the DB past a single page first: a fresh 4 KB
        # schema-only file is treated by plain sqlite as an empty
        # DB when its header is torn (it just recreates the
        # schema), which hides the corruption. Production has
        # real rows, so grow it with page_views rows first.
        with app.connect() as c:
            for i in range(200):
                c.execute(
                    'INSERT INTO page_views(user_id,path,created) '
                    'VALUES (NULL,?,?)',
                    ('/x' * 200 + str(i), 1700000000 + i))
        # Plain sqlite runs in WAL mode: the rows above live in
        # the -wal file until a checkpoint, so checkpoint first
        # before judging the main file's size.
        with app.connect() as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            rows = c.execute(
                'SELECT count(*) FROM page_views').fetchone()[0]
        assert rows == 200 and db_path.stat().st_size > 4096
        _corrupt_header(db_path)
        db_backend._WAL_PROBE_DONE = False
        try:
            app.ensure_database_healthy()
            raised = False
        except Exception as exc:
            raised = isinstance(exc, RuntimeError)
        check('gate: corrupt DB without cloud raises RuntimeError',
              raised)
        check('gate: corrupt local file is NOT deleted by the gate',
              db_path.exists())
    finally:
        shutil.rmtree(data, ignore_errors=True)

    # With a cloud copy: the gate rebuilds (mocked rebuild that
    # replaces the torn file with a fresh empty DB) and startup
    # then succeeds with a working schema. NOTE: init_db runs in
    # local mode first — a real Turso URL must never be used to
    # create the initial DB in a test, or libsql tries to sync
    # to it. The Turso env vars are switched on only for the
    # ensure_database_healthy() call itself.
    data = Path(tempfile.mkdtemp(prefix='quad-malformed-gate-cloud-'))
    try:
        app = _load_app(data, turso=False)
        app.init_db()
        db_path = app.DB_PATH
        os.environ['TURSO_DATABASE_URL'] = 'libsql://fake.turso.io'
        os.environ['TURSO_AUTH_TOKEN'] = 'test-token'
        _corrupt_header(db_path)
        db_backend._WAL_PROBE_DONE = False
        real_rebuild = db_backend.rebuild_replica

        def fake_rebuild(path, sync_url, auth_token):
            # Simulate Turso delivering a fresh replica: quarantine
            # the torn file, write a fresh empty SQLite file.
            # Also switch the app to the 'local' Turso sentinel so
            # the init_db() that follows the rebuild opens the
            # fresh local file instead of trying to sync to the
            # fake test URL (in production the URL is real).
            db_backend._quarantine_local_file(path)
            s = sqlite3.connect(str(path))
            s.execute('CREATE TABLE placeholder(id INTEGER)')
            s.commit()
            s.close()
            os.environ['TURSO_DATABASE_URL'] = 'local'
            return db_backend.connect_turso(path, sync_url=None,
                                            auth_token='')

        db_backend.rebuild_replica = fake_rebuild
        try:
            app.ensure_database_healthy()
            healed = True
        except Exception:
            healed = False
        finally:
            db_backend.rebuild_replica = real_rebuild
        check('gate: corrupt DB with cloud rebuilds and starts',
              healed)
        check('gate: damaged file was quarantined by the rebuild',
              len(list(Path(data).glob('network.db.corrupt-*'))) == 1)
        with app.connect() as c:
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='users'").fetchone()
        check('gate: schema exists after the rebuild', row is not None)
    finally:
        shutil.rmtree(data, ignore_errors=True)


# ---------------------------------------------------------- subprocess
def _run_app(data, port, extra_env=None):
    env = os.environ.copy()
    env.update({'PORT': str(port), 'PRODUCTION': '0', 'TRUST_PROXY': '0',
                'DATA_DIR': str(data), 'APP_SECRET': 'malformed-test-secret',
                'ADMIN_EMAILS': 'mitchellwhite018@gmail.com',
                'PBKDF2_ITERATIONS': '10000', 'HOSTED': '1'})
    env.pop('TURSO_DATABASE_URL', None)
    env.pop('TURSO_AUTH_TOKEN', None)
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, str(ROOT / 'the_network_production.py')],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)


def _healthz(port):
    try:
        with urllib.request.urlopen(
                'http://127.0.0.1:%d/healthz' % port, timeout=1) as r:
            return r.status
    except Exception:
        return None


def subprocess_tests():
    # Corrupt plain-SQLite DATA_DIR: the process must exit
    # non-zero, log FATAL, and never serve /healthz 200.
    data = Path(tempfile.mkdtemp(prefix='quad-malformed-sub-bad-'))
    port = 18312
    try:
        (data / 'network.db').write_bytes(b'X' * 200)
        proc = _run_app(data, port)
        served = False
        for _ in range(50):
            if _healthz(port) == 200:
                served = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        check('subprocess: corrupt DB exits non-zero',
              proc.returncode not in (0, None))
        check('subprocess: corrupt DB logs FATAL',
              'FATAL' in (out or ''))
        check('subprocess: corrupt DB never serves /healthz 200',
              not served)
    finally:
        shutil.rmtree(data, ignore_errors=True)

    # Healthy DB: binds first and serves /healthz 200.
    data = Path(tempfile.mkdtemp(prefix='quad-malformed-sub-ok-'))
    port = 18313
    proc = _run_app(data, port)
    try:
        ready = False
        for _ in range(100):
            if _healthz(port) == 200:
                ready = True
                break
            time.sleep(0.1)
        check('subprocess: healthy DB binds and serves /healthz 200',
              ready)
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        conn.request('GET', '/')
        resp = conn.getresponse()
        body = resp.read().decode('utf-8', 'replace')
        conn.close()
        check('subprocess: healthy DB serves homepage 200',
              resp.status == 200
              and 'Started in a dorm room' in body)
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        shutil.rmtree(data, ignore_errors=True)


def start_sh_tests():
    text = (ROOT / 'start.sh').read_text()
    check('start.sh: waits for /healthz before starting nginx',
          '/healthz' in text and 'nginx' in text
          and 'FATAL: app process exited before becoming healthy'
          in text)
    check('start.sh: does not start nginx unconditionally first',
          text.index('/healthz') < text.index("nginx -g 'daemon off;"))


if __name__ == '__main__':
    marker_tests()
    db_backend_tests()
    ensure_healthy_tests()
    subprocess_tests()
    start_sh_tests()
    sys.exit(report())

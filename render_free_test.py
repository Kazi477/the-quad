"""Test Render free + Turso mode (dev only, temp DBs wiped after each run).

What this proves:
  * db_backend's libSQL wrapper behaves like sqlite3: name-addressed
    rows, ``with`` blocks that commit, executescript, lastrowid, FTS5
    search, and UNIQUE errors surfacing as sqlite3.IntegrityError.
  * The whole app runs end-to-end in Turso mode (libSQL driver, local
    file, TURSO_DATABASE_URL='local' — the dev-test sentinel for
    "same driver, no cloud sync"): register, login, post, feed,
    FTS search, admin page, and a Turso-mode backup file.
  * Data survives a restart: stop the server, start it again on the
    same DATA_DIR, the post is still there and the same session
    cookie still logs in.
  * An unreachable Turso sync URL fails LOUDLY at startup instead of
    silently running on a local-only file (that would look fine and
    then lose data on the next Render restart).
  * render-free.yaml really is free: plan: free, no disk, health
    check /healthz, TURSO_* env vars marked sync:false, DATA_DIR on
    the ephemeral /tmp path.

What this does NOT prove (honest): a real Turso cloud sync. No
Turso account/database exists in this dev environment, so the cloud
handshake is untested here — the first real deploy is the first
live sync test. See RENDER_FREE.md.

Driver setup: libsql-experimental is not installed system-wide.
If it is missing, this file creates /tmp/quad-turso-venv, installs
it there, and re-runs itself under that venv (once). Render's
Docker build installs it from requirements.txt instead.

Usage:  cd ~/workspace/network-merge/dev && python3 render_free_test.py
"""

import http.cookiejar
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
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
    print('render_free_test: %d passed, %d failed'
          % (len(CHECKS) - len(failed), len(failed)))
    for name, ok in CHECKS:
        if not ok:
            print('  FAILED:', name)
    return 1 if failed else 0


# ---------------------------------------------------------- wrapper unit
def wrapper_unit_tests():
    data = Path(tempfile.mkdtemp(prefix='quad-turso-unit-'))
    try:
        conn = db_backend.connect_turso(data / 't.db', sync_url=None,
                                        auth_token='')
        check('wrapper: connect_turso local file opens', conn is not None)
        conn.executescript(
            'CREATE TABLE posts(id INTEGER PRIMARY KEY, body TEXT);'
            'CREATE TABLE u(id INTEGER PRIMARY KEY, email TEXT UNIQUE);')
        with conn as c:
            cur = c.execute('INSERT INTO posts(body) VALUES (?)',
                            ('hello quad world',))
            check('wrapper: lastrowid is 1', cur.lastrowid == 1)
            c.execute('INSERT INTO u(email) VALUES (?)', ('a@x',))
        row = conn.execute('SELECT * FROM posts WHERE id=?',
                           (1,)).fetchone()
        check('wrapper: row by column name', row['body'] == 'hello quad world')
        check('wrapper: row by index', row[0] == 1)
        check('wrapper: row.keys()', row.keys() == ['id', 'body'])
        try:
            conn.execute('INSERT INTO u(email) VALUES (?)', ('a@x',))
            conn.commit()
            check('wrapper: UNIQUE raises IntegrityError', False)
        except Exception as exc:  # noqa: BLE001 - checking the type
            import sqlite3
            check('wrapper: UNIQUE raises IntegrityError',
                  isinstance(exc, sqlite3.IntegrityError))
            conn.rollback()
        conn.execute(
            "CREATE VIRTUAL TABLE posts_fts USING fts5(body, content='posts',"
            " content_rowid='id')")
        conn.execute(
            "CREATE TRIGGER posts_fts_ai AFTER INSERT ON posts BEGIN "
            "INSERT INTO posts_fts(rowid, body) VALUES (new.id, new.body); END;")
        conn.execute("INSERT INTO posts_fts(rowid, body) SELECT id, body "
                     "FROM posts")
        conn.commit()
        hits = conn.execute(
            'SELECT p.body FROM posts_fts f JOIN posts p ON p.id=f.rowid '
            'WHERE posts_fts MATCH ?', ('quad',)).fetchall()
        check('wrapper: FTS5 MATCH works',
              len(hits) == 1 and hits[0]['body'] == 'hello quad world')
        # backup_turso with no sync URL: sync step is skipped by the
        # wrapper path — backup_turso needs a real sync URL, so here
        # just check it fails cleanly (returns False) instead of
        # crashing the app.
        ok = db_backend.backup_turso(data / 't.db', data / 'bak.db',
                                     'libsql://invalid.invalid', 'x')
        check('wrapper: backup_turso with bad sync URL returns False',
              ok is False)
        conn.close()
    finally:
        shutil.rmtree(data, ignore_errors=True)


# ---------------------------------------------------------- HTTP helpers
def make_client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))


def get(op, base, path):
    with op.open(base + path, timeout=5) as r:
        return r.status, r.read().decode('utf-8', 'replace')


def post(op, base, path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(base + path, data=body, method='POST',
                                 headers={'Content-Type':
                                          'application/x-www-form-urlencoded'})
    try:
        with op.open(req, timeout=5) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ''


def start_server(data, port, extra_env=None):
    env = os.environ.copy()
    env.update({'PORT': str(port), 'PRODUCTION': '0', 'TRUST_PROXY': '0',
                'DATA_DIR': str(data), 'APP_SECRET': 'render-free-test-secret',
                'ADMIN_EMAILS': 'mitchellwhite018@gmail.com',
                'PBKDF2_ITERATIONS': '10000',
                'TURSO_DATABASE_URL': 'local',
                'TURSO_AUTH_TOKEN': 'test-token-not-a-real-token'})
    if extra_env:
        env.update(extra_env)
    # The app is run with the venv python so libsql is importable.
    python = sys.executable
    proc = subprocess.Popen(
        [python, str(ROOT / 'the_network_production.py')],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = 'http://127.0.0.1:%d' % port
    ready = False
    for _ in range(100):
        try:
            with urllib.request.urlopen(base + '/healthz', timeout=1) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.1)
    return proc, base, ready


def stop_server(proc):
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ''


# ---------------------------------------------------------- end-to-end
def end_to_end_tests():
    data = Path(tempfile.mkdtemp(prefix='quad-render-free-'))
    port = 18281
    proc, base, ready = start_server(data, port)
    try:
        check('e2e: server starts in Turso (local) mode', ready)
        if not ready:
            return
        op = make_client()
        st, _ = post(op, base, '/register',
                     {'name': 'Mitchell', 'email': 'mitchellwhite018@gmail.com',
                      'password': 'correct-horse-battery-staple',
                      'confirm_password': 'correct-horse-battery-staple'})
        check('e2e: register owner in Turso mode', st == 200)
        st, _, = post(op, base, '/login',
                      {'email': 'mitchellwhite018@gmail.com',
                       'password': 'correct-horse-battery-staple'})
        check('e2e: login in Turso mode', st == 200)
        st, page = get(op, base, '/')
        token = csrf(page)
        check('e2e: feed loads with CSRF token', st == 200 and bool(token))
        st, _ = post(op, base, '/posts',
                     {'csrf': token, 'body': 'render free turso post'})
        check('e2e: post in Turso mode', st == 200)
        st, page = get(op, base, '/')
        check('e2e: post visible on feed', 'render free turso post' in page)
        st, page = get(op, base, '/search?q=turso')
        check('e2e: FTS search finds the post', 'render free turso post' in page)
        st, page = get(op, base, '/admin')
        check('e2e: admin page loads for owner', st == 200 and 'Admin' in page)
        backups = sorted((data / 'backups').glob('network-*.db'))
        check('e2e: startup backup exists in Turso mode', len(backups) >= 1)
    finally:
        stop_server(proc)
    # Restart on the same DATA_DIR: the post must still be there and
    # the old session cookie must still work (session rows live in
    # the same libSQL file).
    proc, base, ready = start_server(data, port + 1)
    try:
        check('e2e: server restarts on same DATA_DIR', ready)
        if ready:
            st, page = get(op, base, '/')
            check('e2e: post survives restart', 'render free turso post' in page)
            check('e2e: session survives restart (still logged in)',
                  'Mitchell' in page and '/logout' in page)
    finally:
        stop_server(proc)
        shutil.rmtree(data, ignore_errors=True)


def unreachable_sync_fails_loudly():
    data = Path(tempfile.mkdtemp(prefix='quad-render-free-bad-'))
    env = os.environ.copy()
    env.update({'PORT': '18282', 'PRODUCTION': '0', 'TRUST_PROXY': '0',
                'DATA_DIR': str(data), 'APP_SECRET': 'render-free-test-secret',
                'ADMIN_EMAILS': 'mitchellwhite018@gmail.com',
                'PBKDF2_ITERATIONS': '10000',
                'TURSO_DATABASE_URL': 'libsql://invalid.invalid',
                'TURSO_AUTH_TOKEN': 'bad-token'})
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / 'the_network_production.py')],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ready = False
    for _ in range(60):
        try:
            with urllib.request.urlopen('http://127.0.0.1:18282/healthz',
                                        timeout=1) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.1)
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    check('loud-fail: unreachable Turso URL never serves /healthz',
          not ready)
    check('loud-fail: startup log mentions Turso',
          'Turso' in (out or '') or 'turso' in (out or '').lower())
    shutil.rmtree(data, ignore_errors=True)


def yaml_checks():
    text = (ROOT / 'render-free.yaml').read_text()
    check('yaml: plan is free', 'plan: free' in text)
    check('yaml: no persistent disk block',
          not re.search(r'(?m)^\s*disk:\s*$', text))
    check('yaml: health check is /healthz', 'healthCheckPath: /healthz' in text)
    check('yaml: TURSO_DATABASE_URL is sync:false',
          'TURSO_DATABASE_URL' in text and 'sync: false' in text)
    check('yaml: TURSO_AUTH_TOKEN is sync:false',
          'TURSO_AUTH_TOKEN' in text and 'sync: false' in text)
    check('yaml: DATA_DIR is ephemeral /tmp path',
          'value: /tmp/quad-data' in text)
    check('yaml: APP_SECRET is generated, not hard-coded',
          'APP_SECRET' in text and 'generateValue: true' in text)
    paid = (ROOT / 'render.yaml').read_text()
    check('yaml: paid render.yaml untouched (still 0.5c-512mb)',
          '0.5c-512mb' in paid and 'network-data' in paid)


if __name__ == '__main__':
    wrapper_unit_tests()
    end_to_end_tests()
    unreachable_sync_fails_loudly()
    yaml_checks()
    sys.exit(report())

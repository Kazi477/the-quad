"""Dev-only test for Update 19 — recovery-code regeneration crash
on The Campus, live Fri 2026-11-20 ~01:24-01:28 AEST (live commit
ef50705).

Live failure: Settings -> Password recovery, "Create new recovery
codes" — the inline two-step confirm armed correctly, but the
second click (POST /recovery-codes/regenerate) crashed. Attempt 1:
Render 502, whole site down 2-3 minutes including /settings.
Attempt 2 after recovery: the app's own 500 page at
/recovery-codes/regenerate. No codes were created. Steps 1-3 and 6
of the same dummy test passed (create account, text-only post,
log out/in, self-delete), so this was specific to regenerate.

Root cause (proven in dev against a local sqld primary, Turso
mode, default 600k PBKDF2 iterations): generate_recovery_codes()
hashed each code INSIDE the INSERT loop, after the DELETE had
already acquired the single-writer lock (Update 16). The lock was
held for 10 x 600k PBKDF2 hashes — ~2.4s of CPU on this VM, ~25-35s
on Render free's 0.1 CPU — plus a dozen separate remote Turso
writes. While the lock was held, every other request that writes
(rate_limit on every page view, session touches) queued behind
it: rate_limit retries 3 x 10s, requests passed nginx's 60s
timeout and 502d, and a second regenerate hit the 10s lock
timeout and died with the app's 500 page. Pre-fix repro numbers
(local sqld, 600k iterations): regenerate alone 3.04s, a
concurrent GET / 2.69s, GET /settings 2.70s. At 5x hashing cost
(PBKDF2_ITERATIONS=3000000, simulating Render's slower CPU):
regenerate 13.04s, concurrent GETs 14.04s.

The fix (Update 19): hashes are computed BEFORE any database
write (prepare_recovery_codes, 100k iterations for codes — a
12-char code from a 32-symbol alphabet is ~60 bits of entropy,
so 100k PBKDF2 is ample and old 600k codes still verify because
check_password reads the iteration count from the stored hash);
the writes run as one explicit BEGIN IMMEDIATE transaction
instead of 12 separate remote transactions; and the handler
retries a transient lock/WAL failure (3 attempts) and then
answers with a friendly 503 "try again" page instead of a 500.
Post-fix repro numbers (same setup): regenerate 0.49s,
concurrent GET / 0.13s, GET /settings 0.16s.

Covers:
 - plain-sqlite mode: regenerate works, 10 codes shown once,
   stored hashes use 100k iterations, a full reset with one of
   the new codes works (logout -> forgot-password reset with
   the code -> login with the new password), and a code hashed
   at the old 600k iterations still verifies.
 - Turso mode (local sqld primary, skipped if none is running
   at QUAD_SQLD_URL, default http://127.0.0.1:8080): regenerate
   is fast (<3s at live iteration counts) and a concurrent
   GET / during a regenerate is fast (<3s, i.e. it is not
   wedged behind the writer lock); with the writer lock held
   by another thread for 12s, a regenerate answers 503 with
   the friendly message (not 500, not a hang), a GET / during
   the hold still answers 200, and a regenerate after the
   lock is released succeeds.

Run: python3 regen19_test.py   (from this folder)
"""
import http.cookiejar, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, threading, urllib.parse, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
results = []
def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), '-', name, ('| ' + detail if detail else ''))

def client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def wait_ready(base, timeout=90):
    for _ in range(int(timeout / 0.2)):
        try:
            with urllib.request.urlopen(base + '/healthz', timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False

def req(op, base, path, data=None, timeout=90):
    t = time.time()
    headers = {'X-Forwarded-For': XFF}
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    try:
        r = op.open(urllib.request.Request(base + path, data=data,
                     method='POST' if data is not None else 'GET',
                     headers=headers),
                     timeout=timeout)
        return r.status, r.read().decode('utf-8', 'replace'), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace'), time.time() - t
    except Exception as e:
        return 'ERR %s: %s' % (type(e).__name__, e), '', time.time() - t

def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ''

def post_form(op, base, path, fields):
    return req(op, base, path, urllib.parse.urlencode(fields).encode())

PW = 'correct-horse-battery-staple'
NEW_PW = 'another-horse-battery-staple'

# Unique client address per run, sent as X-Forwarded-For on every
# request. The Turso server runs with TRUST_PROXY=1, so its
# register-client rate-limit bucket (5/hour per client) is fresh
# for each run — without this, a full-suite run registers several
# users against the one shared local sqld primary and the Turso
# registration 429s once the bucket fills, failing every later
# Turso check for a reason unrelated to the fix. The plain server
# runs with TRUST_PROXY=0, so it ignores this header.
XFF = '10.200.%d.%d' % (int(time.time()) % 250 + 1, os.getpid() % 250 + 1)

def start_app(data_dir, port, extra_env=None):
    env = os.environ.copy()
    env.update({'PORT': str(port), 'PRODUCTION': '0', 'TRUST_PROXY': '0',
                'DATA_DIR': str(data_dir), 'APP_SECRET': 'regen19-test-secret',
                'ADMIN_EMAILS': 'owner@example.com'})
    if extra_env:
        env.update(extra_env)
    p = subprocess.Popen([sys.executable, str(ROOT / 'the_network_production.py')],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p

def stop_app(p):
    p.terminate()
    try:
        out, _ = p.communicate(timeout=5)
    except Exception:
        p.kill()
        out, _ = p.communicate()
    return out

def register_and_login(base, email, name='Diag'):
    op = client()
    st, _, _ = post_form(op, base, '/register',
                         {'name': name, 'email': email, 'password': PW, 'confirm_password': PW})
    if st != 200:
        return op, '', st
    st, home, _ = req(op, base, '/')
    return op, csrf(home), st

def regenerate(op, base, tok):
    return post_form(op, base, '/recovery-codes/regenerate', {'csrf': tok})

def codes_in(txt):
    return re.findall(r'<b>([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})</b>', txt)

# ---------------------------------------------------------------- plain mode
plain_data = Path(tempfile.mkdtemp(prefix='regen19-plain-'))
plain_port = '18260'
plain_base = 'http://127.0.0.1:' + plain_port
plain = start_app(plain_data, plain_port)
plain_log = ''
try:
    check('plain server ready', wait_ready(plain_base))
    op, tok, st = register_and_login(plain_base, 'diag@example.com')
    check('plain member registers and is logged in', bool(tok), 'status %s' % st)
    t0 = time.time()
    st, txt, dt = regenerate(op, plain_base, tok)
    check('plain regenerate -> 200 with codes shown once',
          st == 200 and 'Save these recovery codes now' in txt, 'got %s in %.2fs' % (st, dt))
    check('plain regenerate is fast (<2s at live iteration counts)', dt < 2, '%.2fs' % dt)
    plain_codes = codes_in(txt)
    check('plain regenerate shows 10 codes', len(plain_codes) == 10, 'got %d' % len(plain_codes))
    # Stored hashes use the new 100k iterations.
    db = sqlite3.connect(str(plain_data / 'network.db'))
    hashes = [r[0] for r in db.execute('SELECT code_hash FROM recovery_codes').fetchall()]
    db.close()
    check('plain stored code hashes use 100k iterations',
          len(hashes) == 10 and all(h.startswith('pbkdf2_sha256$100000$') for h in hashes),
          'got %d hashes, e.g. %s' % (len(hashes), hashes[0][:30] if hashes else 'none'))
    # Full reset flow with one of the new codes (via the
    # send-reset-code step, as forgot_test does it: the token on
    # the /forgot-password page is for send-reset, the reset form
    # returned by that step carries the reset-password token).
    st, txt, _ = req(op, plain_base, '/logout', urllib.parse.urlencode({'csrf': tok}).encode())
    st, forgot, _ = req(op, plain_base, '/forgot-password')
    toks = re.findall(r'name="reset_token" value="([^"]+)"', forgot)
    check('forgot-password form has a reset token', bool(toks))
    st, form, _ = post_form(op, plain_base, '/send-reset-code',
                            {'reset_token': toks[0], 'email': 'diag@example.com'})
    toks2 = re.findall(r'name="reset_token" value="([^"]+)"', form)
    check('send-reset-code returns a reset-password token', bool(toks2))
    st, txt, _ = post_form(op, plain_base, '/reset-password',
                           {'email': 'diag@example.com', 'code': plain_codes[0],
                            'password': NEW_PW, 'confirm_password': NEW_PW,
                            'reset_token': toks2[0]})
    check('reset with a new recovery code works', st == 200 and 'Password reset' in txt, 'got %s' % st)
    op2 = client()
    st, txt, _ = post_form(op2, plain_base, '/login', {'email': 'diag@example.com', 'password': NEW_PW})
    check('login with the new password after code reset works', st == 200 and 'Welcome' in txt, 'got %s' % st)
    # A code hashed at the OLD 600k iterations still verifies.
    sys.path.insert(0, str(ROOT))
    import importlib.util
    spec = importlib.util.spec_from_file_location('campus_app', str(ROOT / 'the_network_production.py'))
    app_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_mod)
    db = sqlite3.connect(str(plain_data / 'network.db'))
    db.row_factory = sqlite3.Row
    uid = db.execute("SELECT id FROM users WHERE email='diag@example.com'").fetchone()['id']
    with db:
        db.execute('DELETE FROM recovery_codes WHERE user_id=?', (uid,))
        old_hash = app_mod.password_hash(app_mod.normalise_recovery_code('ABCD-EFGH-JKLN'), iterations=600000)
        db.execute("INSERT INTO recovery_codes(user_id,code_hash,used,created) VALUES(?,?,0,?)",
                   (uid, old_hash, time.time()))
    with db:
        ok = app_mod.consume_recovery_code(db, uid, 'ABCD-EFGH-JKLN')
    db.close()
    check('a code hashed at the old 600k iterations still verifies', ok)
finally:
    plain_log = stop_app(plain)

# ---------------------------------------------------------------- Turso mode
# The server runs IN-PROCESS here (not a subprocess) so the test
# can hold the wrapper's single-writer lock itself and prove a
# wedged regenerate does not wedge other requests — a lock held
# in the test process cannot affect a server subprocess.
# Local sqld <=0.24.32 rejects PRAGMA busy_timeout/synchronous on a
# replica ("unsupported statement"); Turso cloud accepts them, so
# db_backend._apply_pragmas is patched tolerantly in-process —
# test-only, the shipped _apply_pragmas is untouched.
sqld_url = os.environ.get('QUAD_SQLD_URL', 'http://127.0.0.1:8080')
sqld_up = False
try:
    import libsql_experimental as _libsql
    _probe = os.path.join(tempfile.mkdtemp(), 'probe.db')
    _c = _libsql.connect(_probe, sync_url=sqld_url, auth_token='')
    _c.execute('SELECT 1').fetchone()
    _c.close()
    sqld_up = True
except Exception:
    sqld_up = False

if not sqld_up:
    check('Turso-mode tests (no sqld at %s, skipped)' % sqld_url, True)
else:
    sys.path.insert(0, str(ROOT))
    import db_backend
    _real_pragmas = db_backend._apply_pragmas
    def _tolerant_pragmas(conn):
        for _sql in ('PRAGMA foreign_keys=ON', 'PRAGMA busy_timeout=30000',
                     'PRAGMA synchronous=NORMAL'):
            try:
                conn.execute(_sql)
            except Exception as exc:
                if 'unsupported statement' not in str(exc):
                    raise
    db_backend._apply_pragmas = _tolerant_pragmas
    db_backend._WAL_PROBE_DONE = False
    turso_data = Path(tempfile.mkdtemp(prefix='regen19-turso-'))
    turso_port = 18261
    turso_base = 'http://127.0.0.1:%d' % turso_port
    os.environ.update({'PORT': str(turso_port), 'PRODUCTION': '0', 'TRUST_PROXY': '1',
                       'DATA_DIR': str(turso_data), 'APP_SECRET': 'regen19-turso-secret',
                       'ADMIN_EMAILS': 'owner@example.com',
                       'TURSO_DATABASE_URL': sqld_url, 'TURSO_AUTH_TOKEN': 'x'})
    os.environ.pop('PBKDF2_ITERATIONS', None)  # live default: 600k for passwords
    spec2 = importlib.util.spec_from_file_location(
        'campus_app_turso', str(ROOT / 'the_network_production.py'))
    app_t = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(app_t)
    srv = None
    try:
        app_t.ensure_database_healthy()
        app_t._DB_READY.set()
        srv = app_t.NetworkHTTPServer(('127.0.0.1', turso_port), app_t.Handler)
        threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.5},
                         daemon=True).start()
        check('turso server ready', wait_ready(turso_base, timeout=120))
        op, tok, st = register_and_login(turso_base, 'turso-diag-%d@example.com' % int(time.time()), name='TDiag')
        check('turso member registers and is logged in', bool(tok), 'status %s' % st)
        # Concurrent regenerate + GETs: the GETs must not wait on the lock.
        res = {}
        def do_regen():
            res['regen'] = regenerate(op, turso_base, tok)
        def do_get(path, key):
            res[key] = req(op, turso_base, path)
        th = threading.Thread(target=do_regen); th.start(); time.sleep(0.3)
        t2 = threading.Thread(target=do_get, args=('/', 'get')); t2.start()
        t3 = threading.Thread(target=do_get, args=('/settings', 'settings')); t3.start()
        th.join(); t2.join(); t3.join()
        check('turso regenerate during concurrency -> 200 with codes',
              res['regen'][0] == 200 and len(codes_in(res['regen'][1])) == 10,
              'got %s in %.2fs' % (res['regen'][0], res['regen'][2]))
        check('turso GET / during regenerate is fast (<3s, not wedged)',
              res['get'][0] == 200 and res['get'][2] < 3, 'got %s in %.2fs' % (res['get'][0], res['get'][2]))
        check('turso GET /settings during regenerate is fast (<3s)',
              res['settings'][0] == 200 and res['settings'][2] < 3,
              'got %s in %.2fs' % (res['settings'][0], res['settings'][2]))
        check('turso regenerate is fast (<3s at live iteration counts)',
              res['regen'][0] == 200 and res['regen'][2] < 3, '%.2fs' % res['regen'][2])

        def hold_lock(seconds):
            db_backend._WRITE_LOCK.acquire()
            time.sleep(seconds)
            db_backend._WRITE_LOCK.release()

        # Lock held 12s: the regenerate's retries must outlast the
        # hold and succeed, and a GET during the hold must answer.
        res2 = {}
        h = threading.Thread(target=hold_lock, args=(12,)); h.start(); time.sleep(0.5)
        r_th = threading.Thread(target=lambda: res2.__setitem__('regen', regenerate(op, turso_base, tok)))
        g_th = threading.Thread(target=lambda: res2.__setitem__('get', req(op, turso_base, '/')))
        r_th.start(); g_th.start(); r_th.join(); g_th.join(); h.join()
        check('regen with writer lock held 12s succeeds via retry (not 500)',
              res2['regen'][0] == 200 and len(codes_in(res2['regen'][1])) == 10,
              'got %s in %.2fs' % (res2['regen'][0], res2['regen'][2]))
        check('GET / with writer lock held 12s still answers 200',
              res2['get'][0] == 200, 'got %s in %.2fs' % (res2['get'][0], res2['get'][2]))

        # Lock held 55s (longer than the whole retry budget:
        # rate_limit ~30s + 2 handler attempts ~20s): the
        # regenerate must answer 503 with the friendly message —
        # not a 500, not a hang past nginx's 60s — and a GET during
        # the hold must still answer 200 via the rate-limit
        # in-memory fallback.
        res3 = {}
        h = threading.Thread(target=hold_lock, args=(55,)); h.start(); time.sleep(0.5)
        r_th = threading.Thread(target=lambda: res3.__setitem__('regen', regenerate(op, turso_base, tok)))
        g_th = threading.Thread(target=lambda: res3.__setitem__('get', req(op, turso_base, '/')))
        r_th.start(); g_th.start(); r_th.join(); g_th.join(); h.join()
        check('regen with writer lock held 55s answers 503, not 500',
              res3['regen'][0] == 503 and 'Could not create recovery codes' in res3['regen'][1],
              'got %s in %.2fs' % (res3['regen'][0], res3['regen'][2]))
        check('regen 503 arrives within 60s (not a hang)',
              res3['regen'][2] < 60, '%.2fs' % res3['regen'][2])
        check('GET / with writer lock held 55s still answers 200',
              res3['get'][0] == 200, 'got %s in %.2fs' % (res3['get'][0], res3['get'][2]))
        check('regen after the lock is released succeeds',
              regenerate(op, turso_base, tok)[0] == 200)
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        db_backend._apply_pragmas = _real_pragmas

if any(not ok for _, ok, _ in results):
    print('--- plain server log tail ---')
    print(plain_log[-3000:])

passed = sum(1 for _, ok, _ in results if ok)
failed = [(n, d) for n, ok, d in results if not ok]
print('REGEN19 TEST: %d passed, %d failed' % (passed, len(failed)))
sys.exit(1 if failed else 0)

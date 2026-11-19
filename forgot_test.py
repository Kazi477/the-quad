"""Dev-only test for Update 7 — Forgot Password with recovery codes.
NOT part of the production bundle.
Run: python3 forgot_test.py   (from this folder)

Covers:
 - Audit of the existing email-code flow: /login links to
   /forgot-password, which explains the no-SMTP situation honestly;
   unknown vs known email give the same response (no enumeration);
   with no SMTP configured no code row is created and the message
   points at recovery codes instead.
 - Email-code reset (code row seeded in the DB, since no SMTP exists
   in the test env): correct code resets, old password stops working,
   new password works, code is single-use, other sessions are
   invalidated, wrong/expired/exhausted codes fail generically,
   weak/mismatched new passwords rejected, forged reset token
   rejected, verify rate limit (429) after too many attempts.
 - Recovery codes (the Update 7 fallback): Settings shows none at
   first, generate creates 10 codes shown once (format
   XXXX-XXXX-XXXX, hashed in the DB, never stored in plaintext),
   Settings afterwards shows only the count; a code resets the
   password with no email, is consumed on use, other codes still
   work, wrong codes fail generically without consuming, regenerating
   replaces old codes, regenerate is CSRF-protected and rate-limited.
 - Feed nudge: a user with no recovery codes sees the "Save your
   recovery codes" banner; it disappears once codes exist.
 - Audit log records password resets with the method used.
"""
import http.cookiejar, importlib.util, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(tempfile.mkdtemp(prefix='network-forgot-'))
PORT = '18190'
env = os.environ.copy()
env.update({'PORT': PORT, 'PRODUCTION': '0', 'TRUST_PROXY': '0', 'DATA_DIR': str(DATA),
            'APP_SECRET': 'forgot-test-secret-value', 'ADMIN_EMAILS': 'owner@example.com',
            'PBKDF2_ITERATIONS': '10000'})
# Deliberately NO SMTP_* variables: the test env mirrors the real
# deployment, where email delivery is not configured.
p = subprocess.Popen([sys.executable, str(ROOT / 'the_network_production.py')], env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
BASE = 'http://127.0.0.1:' + PORT
results = []
def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), '-', name, ('| ' + detail if detail else ''))

def wait_ready():
    for _ in range(80):
        try:
            with urllib.request.urlopen(BASE + '/healthz', timeout=.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(.1)
    return False

def client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def get(op, path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with op.open(req, timeout=5) as r:
            return r.status, r.headers, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode('utf-8', 'replace')

def post(op, path, data, headers=None):
    body = urllib.parse.urlencode(data).encode()
    h = {'Content-Type': 'application/x-www-form-urlencoded'}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=body, method='POST', headers=h)
    try:
        with op.open(req, timeout=5) as r:
            return r.status, r.headers, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode('utf-8', 'replace')

def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ''

def reset_tokens(html):
    return re.findall(r'name="reset_token" value="([^"]+)"', html)

def db():
    return sqlite3.connect(str(DATA / 'network.db'))

def user_id(email):
    with db() as c:
        row = c.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    return row[0] if row else None

spec = importlib.util.spec_from_file_location('quad_app', str(ROOT / 'the_network_production.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PW = 'correct-horse-battery-staple'
NEW_PW = 'brand-new-horse-battery-99'

def seed_and_login(name, email):
    h = mod.password_hash(PW, iterations=10000)
    with db() as c:
        c.execute('INSERT INTO users(name,email,password_hash,is_admin,joined,last_seen) VALUES(?,?,?,?,?,?)',
                  (name, email, h, 0, time.time(), time.time()))
        uid = c.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()[0]
        c.execute('INSERT OR IGNORE INTO user_settings(user_id,private_account) VALUES(?,0)', (uid,))
    op = client()
    st, _, _ = post(op, '/login', {'email': email, 'password': PW})
    assert st == 200, (name, st)
    return op

def seed_code(email, code='123456', expires_in=600, attempts=0):
    uid = user_id(email)
    with db() as c:
        c.execute('INSERT INTO password_reset_codes(user_id,code_hash,expires,attempts,created) VALUES(?,?,?,0,?) ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash,expires=excluded.expires,attempts=0,created=excluded.created',
                  (uid, mod.password_hash(code, iterations=10000), time.time() + expires_in, time.time()))
    return uid

def code_row(email):
    with db() as c:
        return c.execute('SELECT * FROM password_reset_codes r JOIN users u ON u.id=r.user_id WHERE u.email=?', (email,)).fetchone()

def fresh_reset_token(op):
    st, _, page = get(op, '/forgot-password')
    toks = reset_tokens(page)
    assert toks, 'no reset token on forgot page'
    return toks[0]

def reset_with(op, email, code, password=NEW_PW, confirm=None):
    st, _, form = get(op, '/forgot-password')
    toks = reset_tokens(form)
    # The reset form is returned by /send-reset-code; fetch it to get a
    # reset-password token (first token on that page).
    st, _, form = post(op, '/send-reset-code', {'reset_token': toks[0], 'email': email})
    toks = reset_tokens(form)
    return post(op, '/reset-password', {'reset_token': toks[0], 'email': email, 'code': code,
                                        'password': password, 'confirm_password': confirm or password})

def login_ok(email, password):
    op = client()
    st, _, page = post(op, '/login', {'email': email, 'password': password})
    return st == 200 and 'Welcome,' in page

try:
    check('server starts / healthz', wait_ready())
    anon = client()

    # --- Audit of the existing flow: pages and links ---
    st, _, login_page = get(anon, '/login')
    check('/login has a Forgot password? link', st == 200 and 'href="/forgot-password"' in login_page)
    st, _, forgot = get(anon, '/forgot-password')
    check('/forgot-password explains no-SMTP + recovery codes', st == 200 and 'not set up' in forgot and 'recovery code' in forgot.lower() and 'Forgot password?' in forgot)
    check('/forgot-password does not promise an email code will arrive', 'Email delivery is not set up' in forgot)

    # --- Enumeration: unknown vs known email, no SMTP ---
    seed_and_login('Known', 'known@example.com')
    tok = fresh_reset_token(anon)
    st, _, known_page = post(anon, '/send-reset-code', {'reset_token': tok, 'email': 'known@example.com'})
    tok2 = fresh_reset_token(anon)
    st2, _, unknown_page = post(anon, '/send-reset-code', {'reset_token': tok2, 'email': 'nobody@example.com'})
    check('unknown email gets same response as known email (no enumeration)',
          st == 200 and st2 == 200 and 'recovery code' in known_page.lower() and 'recovery code' in unknown_page.lower()
          and 'cannot be sent' in known_page and 'cannot be sent' in unknown_page)
    check('no code row is created when SMTP is not configured', code_row('known@example.com') is None)

    # --- Email-code happy path (code seeded: no SMTP in test env) ---
    seed_code('known@example.com')
    st, _, page = reset_with(anon, 'known@example.com', '123456')
    check('correct email code resets the password', st == 200 and 'Password reset' in page and 'Sign in with your new password' in page)
    check('code row is deleted after use (single-use)', code_row('known@example.com') is None)
    check('new password signs in', login_ok('known@example.com', NEW_PW))
    check('old password no longer works', not login_ok('known@example.com', PW))
    with db() as c:
        audit = c.execute("SELECT metadata FROM audit_log WHERE action='password_reset' ORDER BY id DESC LIMIT 1").fetchone()
    check('reset is audit-logged with method email_code', audit and 'email_code' in audit[0])

    # --- Wrong / expired / exhausted codes ---
    seed_and_login('Wrong', 'wrong@example.com')
    seed_code('wrong@example.com')
    st, _, page = reset_with(anon, 'wrong@example.com', '000000')
    check('wrong code gives generic error, no reset', st == 200 and 'could not be verified' in page and login_ok('wrong@example.com', PW))
    with db() as c:
        att = c.execute('SELECT attempts FROM password_reset_codes r JOIN users u ON u.id=r.user_id WHERE u.email=?', ('wrong@example.com',)).fetchone()
    check('wrong attempt is counted (attempts=1)', att and att[0] == 1)
    st, _, page = reset_with(anon, 'wrong@example.com', '123456')
    check('code still works after one wrong attempt', st == 200 and 'Password reset' in page)
    seed_and_login('Expired', 'expired@example.com')
    seed_code('expired@example.com', expires_in=-10)
    st, _, page = reset_with(anon, 'expired@example.com', '123456')
    check('expired code fails generically and row is removed', st == 200 and 'could not be verified' in page and code_row('expired@example.com') is None)
    seed_and_login('Exhaust', 'exhaust@example.com')
    seed_code('exhaust@example.com', attempts=0)
    with db() as c:
        c.execute('UPDATE password_reset_codes SET attempts=4 WHERE user_id=?', (user_id('exhaust@example.com'),))
    st, _, page = reset_with(anon, 'exhaust@example.com', '000000')
    check('5th wrong attempt deletes the code row', code_row('exhaust@example.com') is None)
    st, _, page = reset_with(anon, 'exhaust@example.com', '123456')
    check('code cannot be used after exhaustion', st == 200 and 'could not be verified' in page)

    # --- Policy, mismatch, forged token, rate limit ---
    seed_and_login('Policy', 'policy@example.com')
    seed_code('policy@example.com')
    st, _, page = reset_with(anon, 'policy@example.com', '123456', password='short', confirm='short')
    check('weak new password rejected by policy', st == 200 and '12' in page and login_ok('policy@example.com', PW))
    st, _, page = reset_with(anon, 'policy@example.com', '123456', password=NEW_PW, confirm='different-password-123')
    check('mismatched confirm rejected', st == 200 and 'do not match' in page)
    st, _, page = post(anon, '/reset-password', {'reset_token': '1234.deadbeefdeadbeefdeadbeefdeadbeef', 'email': 'policy@example.com', 'code': '123456', 'password': NEW_PW, 'confirm_password': NEW_PW})
    check('forged reset token rejected', st == 200 and 'expired or was invalid' in page and login_ok('policy@example.com', PW))
    # Verify rate limit: 11 wrong attempts for a fresh email.
    seed_and_login('Rate', 'rate@example.com')
    seed_code('rate@example.com')
    tok = fresh_reset_token(anon)
    codes = []
    for _ in range(11):
        st, _, form = post(anon, '/send-reset-code', {'reset_token': tok, 'email': 'rate@example.com'})
        toks = reset_tokens(form)
        st, _, _ = post(anon, '/reset-password', {'reset_token': toks[0], 'email': 'rate@example.com', 'code': '000000', 'password': NEW_PW, 'confirm_password': NEW_PW})
        codes.append(st)
    check('verify rate limit hits 429 after too many attempts', codes[-1] == 429, str(codes))

    # --- Recovery codes: generate, show once, count, use ---
    op = seed_and_login('Recovery', 'recovery@example.com')
    st, _, feed = get(op, '/')
    check('feed nudges a user with no recovery codes', st == 200 and 'Save your recovery codes' in feed)
    st, _, settings = get(op, '/settings')
    check('Settings shows no recovery codes yet', st == 200 and 'no recovery codes yet' in settings.lower() and 'Password recovery' in settings)
    st, _, settings2 = post(op, '/recovery-codes/regenerate', {'csrf': csrf(settings)})
    codes_shown = re.findall(r'<b>([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})</b>', settings2)
    check('regenerate shows 10 codes once, in XXXX-XXXX-XXXX format', st == 200 and len(codes_shown) == 10, str(len(codes_shown)))
    check('codes are shown-once warning present', 'shown once' in settings2.lower())
    with db() as c:
        rows = c.execute('SELECT code_hash FROM recovery_codes WHERE user_id=? AND used=0', (user_id('recovery@example.com'),)).fetchall()
    check('10 hashed codes stored, plaintext never in the DB',
          len(rows) == 10 and all(codes_shown[0] not in r[0] for r in rows))
    st, _, settings3 = get(op, '/settings')
    check('Settings afterwards shows only the count, not the codes',
          '10 unused recovery code' in settings3 and codes_shown[0] not in settings3)
    st, _, feed2 = get(op, '/')
    check('feed nudge disappears once codes exist', 'Save your recovery codes' not in feed2)
    # CSRF on regenerate
    st, _, _ = post(op, '/recovery-codes/regenerate', {})
    check('regenerate without CSRF is rejected', st == 403)

    # Use one recovery code (no email involved at all).
    good_code = codes_shown[0]
    st, _, page = reset_with(anon, 'recovery@example.com', good_code)
    check('recovery code resets the password with no email', st == 200 and 'Password reset' in page and 'recovery code' in page.lower())
    check('recovery code signs in with the new password', login_ok('recovery@example.com', NEW_PW))
    st, _, page = reset_with(anon, 'recovery@example.com', good_code)
    check('a used recovery code cannot be reused', st == 200 and 'could not be verified' in page)
    with db() as c:
        left = c.execute('SELECT COUNT(*) FROM recovery_codes WHERE user_id=? AND used=0', (user_id('recovery@example.com'),)).fetchone()[0]
        audit = c.execute("SELECT metadata FROM audit_log WHERE action='password_reset' ORDER BY id DESC LIMIT 1").fetchone()
    check('other recovery codes still work (9 left)', left == 9)
    check('recovery reset is audit-logged with method recovery_code', audit and 'recovery_code' in audit[0])
    # Wrong recovery code: generic error, nothing consumed.
    st, _, page = reset_with(anon, 'recovery@example.com', 'AAAA-BBBB-CCCC')
    check('wrong recovery code fails generically', st == 200 and 'could not be verified' in page)
    with db() as c:
        left2 = c.execute('SELECT COUNT(*) FROM recovery_codes WHERE user_id=? AND used=0', (user_id('recovery@example.com'),)).fetchone()[0]
    check('wrong recovery code consumes nothing', left2 == 9)
    # Recovery code for an unknown email: same generic error.
    st, _, page = reset_with(anon, 'ghost@example.com', codes_shown[1])
    check('recovery code for unknown email fails generically', st == 200 and 'could not be verified' in page)
    # Regenerate replaces old codes.
    op2 = client()
    st, _, _ = post(op2, '/login', {'email': 'recovery@example.com', 'password': NEW_PW})
    st, _, settings4 = get(op2, '/settings')
    st, _, settings5 = post(op2, '/recovery-codes/regenerate', {'csrf': csrf(settings4)})
    new_codes = re.findall(r'<b>([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})</b>', settings5)
    check('regenerate creates a fresh set of 10', len(new_codes) == 10 and new_codes[0] not in codes_shown)
    st, _, page = reset_with(anon, 'recovery@example.com', codes_shown[1])
    check('old codes stop working after regenerate', st == 200 and 'could not be verified' in page)
    st, _, page = reset_with(anon, 'recovery@example.com', new_codes[0], password='another-new-horse-99')
    check('a code from the new set works', st == 200 and 'Password reset' in page and login_ok('recovery@example.com', 'another-new-horse-99'))

    # --- Sessions invalidated by a reset ---
    op3 = seed_and_login('Session', 'session@example.com')
    st, _, before = get(op3, '/')
    assert 'Welcome, Session' in before
    seed_code('session@example.com')
    st, _, page = reset_with(anon, 'session@example.com', '123456')
    check('email-code reset succeeds for session test', st == 200 and 'Password reset' in page)
    st, _, after = get(op3, '/')
    check('old session is invalidated by the reset', 'Welcome, Session' not in after and 'Welcome to The Quad' in after)

    # --- Regenerate rate limit (fresh user, 6 attempts, limit 5/hour) ---
    op4 = seed_and_login('RegenRate', 'regenrate@example.com')
    st, _, s = get(op4, '/settings')
    rcs = []
    for _ in range(6):
        st, _, s = post(op4, '/recovery-codes/regenerate', {'csrf': csrf(s)})
        rcs.append(st)
    check('regenerate rate limit hits 429 on the 6th attempt', rcs[-1] == 429 and rcs.count(200) == 5, str(rcs))

    # --- Real register flow still works and shows the nudge ---
    reg = client()
    st, _, page = post(reg, '/register', {'name': 'Newbie', 'email': 'newbie@example.com', 'password': PW, 'confirm_password': PW})
    check('register still returns the generic thanks page', st == 200 and 'Thanks' in page)
    st, _, feed3 = get(reg, '/')
    check('new account sees the recovery-code nudge on the feed', st == 200 and 'Save your recovery codes' in feed3)
finally:
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait()
    out = p.stdout.read() if p.stdout else ''
    print(out[-1500:])
    shutil.rmtree(DATA, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print('FORGOT TEST: %d passed, %d failed' % (len(results) - len(failed), len(failed)))
    if failed:
        print('FAILED:', [r[0] for r in failed])
    sys.exit(1 if failed else 0)

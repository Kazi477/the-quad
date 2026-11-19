"""Dev-only auth test for The Quad (renamed from The Network).
NOT part of the production bundle (Dockerfile copies only the_network_production.py).
Run: python3 auth_test.py   (from this folder)
Covers: official /login and /register screens, rename branding, valid/invalid
login, duplicate/weak/mismatched/invalid registration, auto-login after
register, new account can post, logout/login cycle, login rate-limit lockout.
Note: register is rate-limited to 5/hour per client IP in the app, so this
file runs on its own server/temp DB and keeps register POSTs to exactly 5.
"""
import http.cookiejar, os, re, shutil, subprocess, sys, tempfile, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(tempfile.mkdtemp(prefix='network-auth-'))
PORT = '18183'
env = os.environ.copy()
env.update({'PORT': PORT, 'PRODUCTION': '0', 'TRUST_PROXY': '0', 'DATA_DIR': str(DATA),
            'APP_SECRET': 'auth-test-secret-value', 'ADMIN_EMAILS': 'owner@example.com',
            'PBKDF2_ITERATIONS': '10000'})
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

def get(op, path):
    try:
        with op.open(BASE + path, timeout=3) as r:
            return r.status, r.headers, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode('utf-8', 'replace')

def post(op, path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body, method='POST',
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with op.open(req, timeout=4) as r:
            return r.status, r.headers, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode('utf-8', 'replace')

def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ''

try:
    check('server starts / healthz', wait_ready())
    anon = client()
    st, _, home = get(anon, '/')
    check('anon landing links to official screens', st == 200 and 'href="/login"' in home and 'href="/register"' in home and 'Welcome to The Quad' in home)
    check('no The Network branding on landing', 'The Network' not in home and 'the network' not in home)
    st, _, login_page = get(anon, '/login')
    check('/login screen: hero, show/hide, links', st == 200 and 'Log in to The Quad' in login_page and 'data-toggle-password="login-password"' in login_page and 'Forgot password?' in login_page and 'Create an account' in login_page and 'The Quad' in login_page)
    st, _, reg_page = get(anon, '/register')
    check('/register screen: confirm + strength meter', st == 200 and 'Create your account' in reg_page and 'name="confirm_password"' in reg_page and 'pw-meter-bar' in reg_page and 'pw-hint' in reg_page and '12+ characters' in reg_page)
    check('no The Network branding on auth screens', 'The Network' not in login_page and 'The Network' not in reg_page)

    # Register #1: valid owner — should auto-log in straight to the feed
    st, _, page = post(anon, '/register', {'name': 'Owner', 'email': 'owner@example.com', 'password': 'correct-horse-battery-staple', 'confirm_password': 'correct-horse-battery-staple'})
    check('valid register returns identical generic 200 page', st == 200 and 'Thanks' in page and 'Continue to The Quad' in page)
    st, _, page = get(anon, '/')
    check('valid register auto-logs in to feed', st == 200 and 'Welcome, Owner' in page and 'Quad moments' in page and 'Started in a dorm room' in page)
    token = csrf(page)
    check('new account can post', token != '')
    st, _, _ = post(anon, '/posts', {'csrf': token, 'body': 'first quad post'})
    st, _, page = get(anon, '/')
    check('new account post visible in feed', 'first quad post' in page)
    # Logged-in user visiting /login or /register goes to the feed
    st, _, page = get(anon, '/login')
    check('logged-in /login redirects to feed', 'Welcome, Owner' in page)
    st, _, page = get(anon, '/register')
    check('logged-in /register redirects to feed', 'Welcome, Owner' in page)
    # Logout, then login cycle
    st, _, _ = post(anon, '/logout', {'csrf': token})
    st, _, home2 = get(anon, '/')
    check('logout returns to landing', 'Welcome to The Quad' in home2 and 'Welcome, Owner' not in home2)
    st, _, page = post(anon, '/login', {'email': 'owner@example.com', 'password': 'correct-horse-battery-staple'})
    check('login after logout works', st == 200 and 'Welcome, Owner' in page)
    # Wrong password: 401, generic error, email preserved, no account hint
    st, _, bad = post(anon, '/login', {'email': 'owner@example.com', 'password': 'wrong-password-123'})
    check('wrong password 401 + generic error + email kept', st == 401 and 'Email or password is incorrect' in bad and 'value="owner@example.com"' in bad)
    # Unknown email: same generic 401 (no enumeration)
    st, _, bad2 = post(anon, '/login', {'email': 'nobody@example.com', 'password': 'wrong-password-123'})
    check('unknown email same generic 401', st == 401 and 'Email or password is incorrect' in bad2)
    # Register #2: duplicate email -> generic 200, NO enumeration (security fix).
    # The page must not confirm the email already exists, and no account is
    # created / no session is granted. Original password must still work.
    st, _, dup = post(anon, '/register', {'name': 'Owner Again', 'email': 'owner@example.com', 'password': 'correct-horse-battery-staple', 'confirm_password': 'correct-horse-battery-staple'})
    check('duplicate email generic 200, no enumeration', st == 200 and 'already exists' not in dup and 'already have an account' in dup and 'Welcome, Owner Again' not in dup)
    st, _, dup_feed = get(anon, '/')
    check('duplicate attempt did not sign the second client in as Owner', 'Welcome, Owner Again' not in dup_feed)
    st, _, page = post(anon, '/login', {'email': 'owner@example.com', 'password': 'correct-horse-battery-staple'})
    check('original account still logs in after duplicate attempt', st == 200 and 'Welcome, Owner' in page)
    # Register #3: weak password -> 400
    st, _, weak = post(anon, '/register', {'name': 'Weak', 'email': 'weak@example.com', 'password': 'short', 'confirm_password': 'short'})
    check('weak password 400 + clear message', st == 400 and '12' in weak)
    # Register #4: mismatched confirm -> 400
    st, _, mis = post(anon, '/register', {'name': 'Mismatch', 'email': 'mis@example.com', 'password': 'correct-horse-battery-staple', 'confirm_password': 'different-password-123'})
    check('mismatched confirm 400 + clear message', st == 400 and 'do not match' in mis)
    # Register #5: invalid email + missing name -> 400 (last register attempt allowed: 5/hour)
    st, _, inv = post(anon, '/register', {'name': '', 'email': 'not-an-email', 'password': 'correct-horse-battery-staple', 'confirm_password': 'correct-horse-battery-staple'})
    check('invalid email 400 + clear message', st == 400 and 'valid email' in inv)
    # Login lockout: 10 failed attempts for one email, 11th is rate-limited
    lock = client()
    codes = []
    for _ in range(11):
        st, _, _ = post(lock, '/login', {'email': 'lockout@example.com', 'password': 'wrong-password-123'})
        codes.append(st)
    check('login lockout after repeated failures', codes[-1] == 429 and codes.count(401) >= 10, str(codes))
    # No old branding anywhere served, and auth JS is present
    st, _, admin = get(anon, '/admin')
    check('admin page has The Quad branding, no The Network', st == 200 and 'The Quad' in admin and 'The Network' not in admin)
    st, _, js = get(anon, '/app.js')
    check('app.js has show/hide + strength meter + Quad Radio', st == 200 and 'data-toggle-password' in js and 'pw-meter-bar' in js and 'Quad Radio' in js)
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
    print('AUTH TEST: %d passed, %d failed' % (len(results) - len(failed), len(failed)))
    if failed:
        print('FAILED:', [r[0] for r in failed])
    sys.exit(1 if failed else 0)

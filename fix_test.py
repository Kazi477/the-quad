"""Dev-only test for Update 4 — fix ALL remaining issues.
NOT part of the production bundle (Dockerfile copies only the_network_production.py).
Run: python3 fix_test.py   (from this folder)

Covers:
 1. Forced password change (old hash + weak-policy password, incl. admin)
 2. Duplicate registration: identical status/body for new vs duplicate
 3. External /app.css, CSP style-src 'self', no unsafe-inline, no inline styles
 4. Signed reset tokens (missing/forged rejected, valid works)
 5. Video duration: no client-duration fallback — unverifiable video rejected
 6. Restore with id collisions: posts/comments remapped, no rows lost
 7. Responsive layout at 360px / desktop: pages render, CSS breakpoints present
"""
import hashlib, http.cookiejar, importlib.util, os, re, secrets, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(tempfile.mkdtemp(prefix='network-fix-'))
PORT = '18187'
env = os.environ.copy()
env.update({'PORT': PORT, 'PRODUCTION': '0', 'TRUST_PROXY': '0', 'DATA_DIR': str(DATA),
            'APP_SECRET': 'fix-test-secret-value', 'ADMIN_EMAILS': 'owner@example.com',
            'PBKDF2_ITERATIONS': '10000'})
p = subprocess.Popen([sys.executable, str(ROOT / 'the_network_production.py')], env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
BASE = 'http://127.0.0.1:' + PORT
results = []
def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), '-', name, ('| ' + detail if detail else ''))

spec = importlib.util.spec_from_file_location('quad_app', str(ROOT / 'the_network_production.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

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

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def client_noredir():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar))

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

def post_multipart(op, path, fields, files, headers=None):
    boundary = '----fix-test-' + secrets.token_hex(8)
    body = b''
    for k, v in fields.items():
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode()
    for k, (fname, ctype, data) in files.items():
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n' % (boundary, k, fname, ctype)).encode() + data + b'\r\n'
    body += ('--%s--\r\n' % boundary).encode()
    h = {'Content-Type': 'multipart/form-data; boundary=' + boundary}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=body, method='POST', headers=h)
    try:
        with op.open(req, timeout=10) as r:
            return r.status, r.headers, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode('utf-8', 'replace')

def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ''

def reset_token(html):
    m = re.search(r'name="reset_token" value="([^"]+)"', html)
    return m.group(1) if m else ''

def db():
    return sqlite3.connect(str(DATA / 'network.db'))

def seed_user(name, email, pwd='seeded-password-123', pwd_hash=None, admin=0):
    h = pwd_hash if pwd_hash is not None else mod.password_hash(pwd, iterations=10000)
    with db() as c:
        cur = c.execute('INSERT INTO users(name,email,password_hash,is_admin,joined,last_seen) VALUES(?,?,?,?,?,?)',
                        (name, email, h, admin, time.time(), time.time()))
        uid = cur.lastrowid
        c.execute('INSERT OR IGNORE INTO user_settings(user_id,private_account) VALUES(?,0)', (uid,))
    return uid

def seed_session(uid):
    token = secrets.token_urlsafe(32)
    csrf_tok = secrets.token_urlsafe(32)
    now = time.time()
    with db() as c:
        c.execute('INSERT INTO sessions(token,user_id,csrf,expires,last_seen) VALUES(?,?,?,?,?)',
                  (token, uid, csrf_tok, now + 7 * 86400, now))
    return {'Cookie': 'network_session=' + token}, csrf_tok

try:
    check('server starts / healthz', wait_ready())
    anon = client()
    nr = client_noredir()

    # ---------- 1. Forced password change ----------
    # (a) legacy SHA-256 hash (very old demo format) — rehash required.
    legacy_hash = hashlib.sha256('legacy-password-123'.encode()).hexdigest()
    legacy_id = seed_user('Old Timer', 'legacy@example.com', pwd_hash=legacy_hash)
    st, h, _ = post(nr, '/login', {'email': 'legacy@example.com', 'password': 'legacy-password-123'})
    check('legacy-hash login redirects to /change-password (303)',
          st == 303 and h.get('Location', '').endswith('/change-password'), str(st) + ' ' + h.get('Location', ''))
    # Follow the redirect with the cookie jar client (same jar is shared? nr has its own jar;
    # use a fresh client and log in again, then GET / must show the change page).
    lc = client()
    st, _, _ = post(lc, '/login', {'email': 'legacy@example.com', 'password': 'legacy-password-123'})
    st, _, feed = get(lc, '/')
    check('flagged user GET / shows change-password page, not feed',
          st == 200 and 'Your password needs updating' in feed and 'Welcome, Old Timer' not in feed, str(st))
    st, _, feed2 = get(lc, '/admin')
    check('flagged user cannot open /admin before changing password',
          'Your password needs updating' in feed2, str(st))
    tok = csrf(feed)
    st, _, _ = post(lc, '/posts', {'csrf': tok, 'body': 'should be blocked'})
    check('flagged user POST /posts is blocked (403)', st == 403, str(st))
    st, _, bad = post(lc, '/change-password', {'csrf': tok, 'current_password': 'wrong-password-123',
                                                'password': 'new-strong-pass-99', 'confirm_password': 'new-strong-pass-99'})
    check('change-password with wrong current password -> 401', st == 401, str(st))
    st, _, weak = post(lc, '/change-password', {'csrf': tok, 'current_password': 'legacy-password-123',
                                                'password': 'short', 'confirm_password': 'short'})
    check('change-password with weak new password -> 400', st == 400, str(st))
    st, _, _ = post(lc, '/change-password', {'csrf': tok, 'current_password': 'legacy-password-123',
                                             'password': 'new-strong-pass-99', 'confirm_password': 'new-strong-pass-99'})
    check('change-password with strong new password succeeds', st in (200, 303), str(st))
    st, _, feed3 = get(lc, '/')
    check('after change, feed works normally', st == 200 and 'Welcome, Old Timer' in feed3, str(st))
    st, _, _ = post(client(), '/login', {'email': 'legacy@example.com', 'password': 'new-strong-pass-99'})
    check('new password logs in', st == 200, str(st))
    st, _, _ = post(client(), '/login', {'email': 'legacy@example.com', 'password': 'legacy-password-123'})
    check('old password no longer works', st == 401, str(st))

    # (b) weak-policy password with a current-format hash.
    weak_id = seed_user('Weak User', 'weakpolicy@example.com', pwd='aaaaaaaaaaaa')
    st, h, _ = post(nr, '/login', {'email': 'weakpolicy@example.com', 'password': 'aaaaaaaaaaaa'})
    check('weak-policy password login redirects to /change-password',
          st == 303 and h.get('Location', '').endswith('/change-password'), str(st) + ' ' + h.get('Location', ''))

    # (c) admin with a weak password is not exempt.
    admin_id = seed_user('Admin Weak', 'adminweak@example.com', pwd='aaaaaaaaaaaa', admin=1)
    st, h, _ = post(nr, '/login', {'email': 'adminweak@example.com', 'password': 'aaaaaaaaaaaa'})
    check('admin with weak password is also forced to change',
          st == 303 and h.get('Location', '').endswith('/change-password'), str(st))

    # (d) strong-password user is NOT forced.
    st, _, _ = post(client(), '/register', {'name': 'Strong User', 'email': 'strong@example.com',
                                            'password': 'correct-horse-battery-staple',
                                            'confirm_password': 'correct-horse-battery-staple'})
    st, h, _ = post(nr, '/login', {'email': 'strong@example.com', 'password': 'correct-horse-battery-staple'})
    check('strong-password login goes to feed, not change-password',
          st == 303 and h.get('Location', '') == '/', str(st) + ' ' + h.get('Location', ''))

    # ---------- 2. Duplicate registration: identical response ----------
    c_new = client()
    st1, _, body_new = post(c_new, '/register', {'name': 'Dup Test', 'email': 'dup@example.com',
                                                 'password': 'correct-horse-battery-staple',
                                                 'confirm_password': 'correct-horse-battery-staple'})
    c_dup = client()
    st2, _, body_dup = post(c_dup, '/register', {'name': 'Dup Test Two', 'email': 'dup@example.com',
                                                 'password': 'correct-horse-battery-staple',
                                                 'confirm_password': 'correct-horse-battery-staple'})
    check('new vs duplicate register: same status', st1 == 200 and st2 == 200, '%s vs %s' % (st1, st2))
    check('new vs duplicate register: identical body', body_new == body_dup, 'bodies differ')
    st, _, feed_new = get(c_new, '/')
    check('new account IS signed in (feed after register)', 'Welcome, Dup Test' in feed_new, '')
    st, _, feed_dup = get(c_dup, '/')
    check('duplicate attempt is NOT signed in', 'Welcome, Dup Test' not in feed_dup and 'Welcome to The Campus' in feed_dup, '')

    # ---------- 3. External CSS / CSP ----------
    st, h, css = get(anon, '/app.css')
    check('/app.css serves text/css with Quad brand', st == 200 and 'text/css' in h.get('Content-Type', '') and '#0d9488' in css, str(st))
    check('/app.css has 650px and 400px breakpoints', 'max-width:650px' in css and 'max-width:400px' in css, '')
    check('/app.css has avatar size classes, no inline widths needed', '.av42' in css and '.av64' in css, '')
    st, h, home = get(anon, '/')
    csp = h.get('Content-Security-Policy', '')
    check('CSP style-src is self only, no unsafe-inline anywhere',
          "style-src 'self';" in csp and 'unsafe-inline' not in csp, csp)
    check('pages link /app.css and contain no <style> or style= attributes',
          'href="/app.css"' in home and '<style>' not in home and 'style="' not in home, '')
    st, _, feed_page = get(c_new, '/')
    check('feed page also has no inline styles', '<style>' not in feed_page and 'style="' not in feed_page, '')
    st, _, prof_page = get(c_new, '/profile')
    check('avatar uses a class, not an inline width', 'class="avatar av80"' in prof_page, '')

    # ---------- 4. Signed reset tokens ----------
    st, _, forgot = get(anon, '/forgot-password')
    tok = reset_token(forgot)
    check('forgot-password form carries a signed token', bool(tok), '')
    st, _, _ = post(anon, '/send-reset-code', {'email': 'strong@example.com'})
    check('send-reset without token -> 403', st == 403, str(st))
    st, _, _ = post(anon, '/send-reset-code', {'email': 'strong@example.com', 'reset_token': '1.00000000000000000000000000000000'})
    check('send-reset with forged token -> 403', st == 403, str(st))
    st, _, reset_form = post(anon, '/send-reset-code', {'email': 'strong@example.com', 'reset_token': tok})
    check('send-reset with valid token -> reset form (200)', st == 200 and 'Enter your code' in reset_form, str(st))
    tok2 = reset_token(reset_form)
    check('reset form carries a signed token', bool(tok2), '')
    st, _, _ = post(anon, '/reset-password', {'email': 'strong@example.com', 'code': '123456',
                                              'password': 'another-correct-password-123',
                                              'confirm_password': 'another-correct-password-123'})
    check('reset-password without token -> invalid-request page', 'expired or was invalid' in _, str(st))

    # ---------- 5. Video duration: no client fallback ----------
    # Use the strong user (signed in via client) for the upload.
    st, _, spage = get(c_new, '/')
    stok = csrf(spage)
    fake_mp4 = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00' + b'\x00' * 100
    st, _, _ = post_multipart(c_new, '/posts', {'csrf': stok, 'body': 'unverifiable video', 'duration_ms': '5000'},
                              {'media': ('v.mp4', 'video/mp4', fake_mp4)})
    check('unverifiable video rejected even with a short client duration_ms', st == 400, str(st))

    # ---------- 6. Restore with id collisions ----------
    # Scenario A: post id reused after removal.
    vic_a = seed_user('Restore A', 'restorea@example.com')
    with db() as c:
        c.execute('INSERT INTO posts(user_id,body,created) VALUES(?,?,?)', (vic_a, 'restore-a post', time.time()))
        pid_a = c.execute('SELECT id FROM posts WHERE user_id=?', (vic_a,)).fetchone()[0]
        c.execute('INSERT INTO comments(post_id,user_id,body,created) VALUES(?,?,?,?)', (pid_a, vic_a, 'restore-a comment', time.time()))
    # Admin (strong user is not admin; use owner admin via DB seed + session)
    admin_uid = seed_user('Fix Admin', 'fixadmin@example.com', admin=1)
    ah, acsrf, _ = seed_session(admin_uid)
    st, _, adm = get(client(), '/admin', headers=ah)
    st, _, _ = post(client(), '/admin/remove-user', {'csrf': csrf(adm), 'user_id': str(vic_a), 'confirm_email': 'restorea@example.com'}, headers=ah)
    check('restore scenario A: removal succeeds', st in (200, 303), str(st))
    # Reuse the freed post id explicitly for another user's post.
    other_id = seed_user('Other User', 'other@example.com')
    with db() as c:
        c.execute('INSERT INTO posts(id,user_id,body,created) VALUES(?,?,?,?)', (pid_a, other_id, 'other user post reusing id', time.time()))
    st, _, adm2 = get(client(), '/admin', headers=ah)
    m = re.search(r'name="removed_id" value="(\d+)"', adm2)
    check('restore scenario A: removed account listed', bool(m), '')
    if m:
        st, _, _ = post(client(), '/admin/restore-user', {'csrf': csrf(adm2), 'removed_id': m.group(1)}, headers=ah)
        check('restore scenario A: restore succeeds despite post-id collision', st in (200, 303), str(st))
        with db() as c:
            rows = c.execute('SELECT id,user_id,body FROM posts WHERE body IN (?,?)', ('restore-a post', 'other user post reusing id')).fetchall()
            bodies = {r[2]: (r[0], r[1]) for r in rows}
        check('restore scenario A: BOTH posts exist, saved post got a new id',
              'restore-a post' in bodies and 'other user post reusing id' in bodies
              and bodies['restore-a post'][0] != pid_a and bodies['other user post reusing id'][0] == pid_a,
              str(bodies))
        with db() as c:
            cm = c.execute('SELECT post_id,user_id FROM comments WHERE body=?', ('restore-a comment',)).fetchone()
        check('restore scenario A: comment follows the remapped post id',
              bool(cm) and cm[0] == bodies.get('restore-a post', (None,))[0], str(cm))

    # Scenario B: user id reused after removal.
    vic_b = seed_user('Restore B', 'restoreb@example.com')
    with db() as c:
        c.execute('INSERT INTO posts(user_id,body,created) VALUES(?,?,?)', (vic_b, 'restore-b post', time.time()))
    st, _, adm = get(client(), '/admin', headers=ah)
    st, _, _ = post(client(), '/admin/remove-user', {'csrf': csrf(adm), 'user_id': str(vic_b), 'confirm_email': 'restoreb@example.com'}, headers=ah)
    check('restore scenario B: removal succeeds', st in (200, 303), str(st))
    with db() as c:
        c.execute('INSERT INTO users(id,name,email,password_hash,is_admin,joined,last_seen) VALUES(?,?,?,?,?,?,?)',
                  (vic_b, 'Id Taker', 'taker@example.com', mod.password_hash('correct-horse-battery-staple', iterations=10000), 0, time.time(), time.time()))
    st, _, adm2 = get(client(), '/admin', headers=ah)
    m = re.search(r'name="removed_id" value="(\d+)"', adm2)
    if m:
        st, _, _ = post(client(), '/admin/restore-user', {'csrf': csrf(adm2), 'removed_id': m.group(1)}, headers=ah)
        check('restore scenario B: restore succeeds despite user-id collision', st in (200, 303), str(st))
        with db() as c:
            row = c.execute('SELECT id FROM users WHERE email=?', ('restoreb@example.com',)).fetchone()
            post_row = c.execute('SELECT user_id FROM posts WHERE body=?', ('restore-b post',)).fetchone()
        check('restore scenario B: restored user has a new id, post points at it',
              bool(row) and row[0] != vic_b and bool(post_row) and post_row[0] == row[0], str((row, post_row)))
        st, _, _ = post(client(), '/login', {'email': 'restoreb@example.com', 'password': 'seeded-password-123'})
        check('restore scenario B: restored user can log in again', st == 200, str(st))

    # ---------- 7. Responsive layout ----------
    pages = {}
    for path in ('/login', '/register', '/'):
        st, _, body = get(anon, path)
        pages[path] = (st, body)
    st, _, prof = get(c_new, '/profile')
    pages['/profile'] = (st, prof)
    st, _, adm_page = get(client(), '/admin', headers=ah)
    pages['/admin'] = (st, adm_page)
    check('responsive: login/register/landing/profile/admin all render 200',
          all(v[0] == 200 for v in pages.values()), str({k: v[0] for k, v in pages.items()}))
    check('responsive: viewport meta on all checked pages',
          all('width=device-width' in v[1] for v in pages.values()), '')
    check('responsive: no inline styles on any checked page',
          all('style="' not in v[1] and '<style>' not in v[1] for v in pages.values()), '')
    check('responsive: 360px breakpoint present, metric grid collapses to 1fr',
          'max-width:400px' in css and '.metric-grid{grid-template-columns:1fr}' in css, '')
    check('responsive: no fixed min-width over 360px in CSS',
          re.search(r'min-width:\s*(3[6-9]\d|[4-9]\d\d|\d{4,})px', css) is None, '')
    check('responsive: traffic bars use classes, not inline heights',
          'class="tb' in adm_page, '')
finally:
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait()
    out = p.stdout.read() if p.stdout else ''
    print(out[-1200:])
    shutil.rmtree(DATA, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print('FIX TEST: %d passed, %d failed' % (len(results) - len(failed), len(failed)))
    if failed:
        print('FAILED:', [r[0] for r in failed])
    sys.exit(1 if failed else 0)

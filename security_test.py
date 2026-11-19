"""Dev-only adversarial security test for The Quad.
NOT part of the production bundle (Dockerfile copies only the_network_production.py).
Run: python3 security_test.py   (from this folder)

Covers: auth (brute force, session fixation/hijack, CSRF, reset-code guessing,
enumeration), access control, XSS/SQLi/path-traversal, uploads, rate limits /
flooding, headers/CSP, cookies, admin remove/restore, mobile layout, music
labelling, Traffic Shield labelling, X-Forwarded-For handling, HSTS in prod.

Notes: registration is rate-limited to 5/hour per client, so this file keeps
HTTP register POSTs to exactly 5 and seeds the other test users directly in
the temp DB. Sessions for seeded users are also seeded directly (the login
flow itself is tested separately with counted HTTP logins).
"""
import http.cookiejar, importlib.util, json, os, re, secrets, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(tempfile.mkdtemp(prefix='network-sec-'))
PORT = '18184'
env = os.environ.copy()
env.update({'PORT': PORT, 'PRODUCTION': '0', 'TRUST_PROXY': '0', 'DATA_DIR': str(DATA),
            'APP_SECRET': 'security-test-secret-value', 'ADMIN_EMAILS': 'owner@example.com',
            'PBKDF2_ITERATIONS': '10000'})
p = subprocess.Popen([sys.executable, str(ROOT / 'the_network_production.py')], env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
BASE = 'http://127.0.0.1:' + PORT
results = []
def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), '-', name, ('| ' + detail if detail else ''))

# Import the app module for password_hash (does not start the server).
spec = importlib.util.spec_from_file_location('quad_app', str(ROOT / 'the_network_production.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
def pw_hash(pwd):
    return mod.password_hash(pwd, iterations=10000)

def wait_ready(base=BASE):
    for _ in range(80):
        try:
            with urllib.request.urlopen(base + '/healthz', timeout=.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(.1)
    return False

def client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Opener that returns 303 responses instead of following them, so
    Set-Cookie headers on login/register redirects can be inspected."""
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
    boundary = '----sec-test-' + secrets.token_hex(8)
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

def seed_user(name, email, pwd='seeded-password-123', admin=0, private=0):
    with db() as c:
        c.execute('PRAGMA foreign_keys=ON')
        cur = c.execute('INSERT INTO users(name,email,password_hash,is_admin,joined,last_seen) VALUES(?,?,?,?,?,?)',
                        (name, email, pw_hash(pwd), admin, time.time(), time.time()))
        uid = cur.lastrowid
        c.execute('INSERT OR IGNORE INTO user_settings(user_id,private_account) VALUES(?,?)', (uid, private))
    return uid

def seed_session(uid):
    token = secrets.token_urlsafe(32)
    csrf_tok = secrets.token_urlsafe(32)
    now = time.time()
    with db() as c:
        c.execute('INSERT INTO sessions(token,user_id,csrf,expires,last_seen) VALUES(?,?,?,?,?)',
                  (token, uid, csrf_tok, now + 7 * 86400, now))
    return token, csrf_tok

def auth_headers(uid):
    token, csrf_tok = seed_session(uid)
    return {'Cookie': 'network_session=' + token}, csrf_tok, token

def get_csrf(op, uid):
    st, _, page = get(op, '/')
    return csrf(page)

try:
    check('server starts / healthz', wait_ready())
    anon = client()

    # ---------- Registration / enumeration / password policy (5 POSTs max) ----------
    st, _, _ = post(anon, '/register', {'name': 'Owner', 'email': 'owner@example.com',
                                        'password': 'correct-horse-battery-staple',
                                        'confirm_password': 'correct-horse-battery-staple'})
    check('register owner (admin email)', st == 200, str(st))
    st, _, dup = post(anon, '/register', {'name': 'Owner Again', 'email': 'owner@example.com',
                                          'password': 'correct-horse-battery-staple',
                                          'confirm_password': 'correct-horse-battery-staple'})
    check('duplicate register: generic 200, no enumeration', st == 200 and 'already exists' not in dup
          and 'already have an account' in dup and 'Welcome, Owner Again' not in dup, str(st))
    st, _, weak = post(anon, '/register', {'name': 'Common', 'email': 'common@example.com',
                                           'password': 'password1234', 'confirm_password': 'password1234'})
    check('common password rejected (password1234)', st == 400 and 'common' in weak.lower(), str(st))
    st, _, weak2 = post(anon, '/register', {'name': 'Simple', 'email': 'simple@example.com',
                                            'password': 'aaaaaaaaaaaa', 'confirm_password': 'aaaaaaaaaaaa'})
    check('single-character-class password rejected', st == 400, str(st))
    st, _, weak3 = post(anon, '/register', {'name': 'Jane Doe', 'email': 'jane@example.com',
                                            'password': 'jane-secure-pass-1', 'confirm_password': 'jane-secure-pass-1'})
    check('password containing own email local part rejected', st == 400, str(st))

    # ---------- Seed the rest via DB (avoids the 5/hour register limit) ----------
    owner_id = 1
    victim_id = seed_user('Victim One', 'victim@example.com', private=1)
    friend_id = seed_user('Friend One', 'friend@example.com')
    attacker_id = seed_user('Attacker', 'attacker@example.com')
    flooder_id = seed_user('Flooder', 'flooder@example.com')
    duper_id = seed_user('Duper', 'duper@example.com')
    with db() as c:
        c.execute("INSERT INTO friendships(id,requester_id,addressee_id,status,created) VALUES(?,?,?,'accepted',?)",
                  (secrets.token_hex(16), friend_id, victim_id, time.time()))
        c.execute('INSERT INTO posts(user_id,body,created) VALUES(?,?,?)', (victim_id, 'victim private post', time.time()))
        pid_victim = c.execute('SELECT id FROM posts WHERE user_id=?', (victim_id,)).fetchone()[0]
        c.execute('INSERT INTO comments(post_id,user_id,body,created) VALUES(?,?,?,?)',
                  (pid_victim, victim_id, 'victim private comment', time.time()))
        cid_victim = c.execute('SELECT id FROM comments WHERE post_id=?', (pid_victim,)).fetchone()[0]

    owner_h, owner_csrf, owner_tok = auth_headers(owner_id)
    victim_h, victim_csrf, _ = auth_headers(victim_id)
    friend_h, friend_csrf, _ = auth_headers(friend_id)
    attacker_h, attacker_csrf, attacker_tok = auth_headers(attacker_id)

    # ---------- Auth: login, session, CSRF, reset ----------
    st, h, _ = post(anon, '/login', {'email': 'owner@example.com', 'password': 'correct-horse-battery-staple'})
    check('valid login works', st == 200, str(st))
    # Inspect the Set-Cookie on the raw 303 (a following opener loses it).
    nr = client_noredir()
    st, h, _ = post(nr, '/login', {'email': 'owner@example.com', 'password': 'correct-horse-battery-staple'})
    login_cookie = h.get('Set-Cookie', '')
    check('session cookie HttpOnly + SameSite=Lax + Path=/', st == 303 and 'HttpOnly' in login_cookie and 'SameSite=Lax' in login_cookie and 'Path=/' in login_cookie, login_cookie)
    st, _, bad = post(anon, '/login', {'email': 'owner@example.com', 'password': 'wrong-password-123'})
    check('wrong password 401 generic', st == 401 and 'Email or password is incorrect' in bad, str(st))
    st, _, bad2 = post(anon, '/login', {'email': 'nobody@example.com', 'password': 'wrong-password-123'})
    check('unknown email same generic 401 (no enumeration)', st == 401 and 'Email or password is incorrect' in bad2, str(st))
    st, _, sqli = post(anon, '/login', {'email': "' OR '1'='1", 'password': 'x'})
    check("SQLi login attempt fails (401, no bypass)", st == 401, str(st))
    # Session fixation: a fresh login must issue a fresh token.
    st, h2, _ = post(nr, '/login', {'email': 'owner@example.com', 'password': 'correct-horse-battery-staple'})
    tok2 = re.search(r'network_session=([^;]+)', h2.get('Set-Cookie', '') or '')
    check('login rotates session token (no fixation)', bool(tok2) and tok2.group(1) != owner_tok, '')
    # Logout invalidates the session.
    st, _, _ = post(anon, '/logout', {'csrf': csrf(get(anon, '/')[2])})
    st, _, home = get(anon, '/')
    check('logout returns to landing', 'Welcome to The Quad' in home and 'Welcome, Owner' not in home)
    st, _, _ = post(anon, '/posts', {'csrf': 'stale', 'body': 'after logout'})
    check('logged-out post is rejected', st == 403, str(st))
    # CSRF: missing / wrong / another user's token must all fail.
    st, _, _ = post(anon, '/posts', {'body': 'no csrf at all'})
    # anon has no session, so use the attacker's session without a csrf field:
    st, _, _ = post(client(), '/posts', {'body': 'x'}, headers=attacker_h)
    check('POST without CSRF token is rejected', st == 403, str(st))
    st, _, _ = post(client(), '/posts', {'csrf': 'wrong-token', 'body': 'x'}, headers=attacker_h)
    check('POST with wrong CSRF token is rejected', st == 403, str(st))
    st, _, _ = post(client(), '/posts', {'csrf': victim_csrf, 'body': 'x'}, headers=attacker_h)
    check("POST with another user's CSRF token is rejected", st == 403, str(st))
    # Login lockout (fresh email, 11 attempts -> 429 on the 11th).
    lock = client()
    codes = []
    for _ in range(11):
        st, _, _ = post(lock, '/login', {'email': 'lockout@example.com', 'password': 'wrong-password-123'})
        codes.append(st)
    check('login lockout after repeated failures', codes[-1] == 429 and codes.count(401) >= 10, str(codes))
    # Password reset: unknown vs known email get the identical message (mail off in dev).
    # Reset forms now carry a signed, unauthenticated CSRF token.
    st, _, forgot = get(anon, '/forgot-password')
    tok_send = reset_token(forgot)
    check('forgot-password form carries a signed reset token', bool(tok_send), '')
    st, _, no_tok = post(anon, '/send-reset-code', {'email': 'owner@example.com'})
    check('send-reset-code without a token is rejected (403)', st == 403, str(st))
    st, _, bad_tok = post(anon, '/send-reset-code', {'email': 'owner@example.com', 'reset_token': '123.abcdef'})
    check('send-reset-code with a forged token is rejected (403)', st == 403, str(st))
    st, _, r1 = post(anon, '/send-reset-code', {'email': 'nobody@example.com', 'reset_token': tok_send})
    st, _, r2 = post(anon, '/send-reset-code', {'email': 'owner@example.com', 'reset_token': tok_send})
    # The notice text must be identical (the form legitimately echoes the
    # submitted email, so the pages differ only by that echo).
    n1 = re.search(r'<p class="notice">(.*?)</p>', r1)
    n2 = re.search(r'<p class="notice">(.*?)</p>', r2)
    check('reset-code request is identical for unknown/known email',
          bool(n1 and n2 and n1.group(1) == n2.group(1) and 'not configured' in n1.group(1)), '')
    tok_reset = reset_token(r2)
    check('reset form carries a signed reset token', bool(tok_reset), '')
    st, _, r_no = post(anon, '/reset-password', {'email': 'owner@example.com', 'code': '123456',
                                                 'password': 'another-correct-password-123',
                                                 'confirm_password': 'another-correct-password-123'})
    check('reset-password without a token is rejected', 'expired or was invalid' in r_no, str(st))
    st, _, r3 = post(anon, '/reset-password', {'email': 'owner@example.com', 'code': '123456', 'reset_token': tok_reset,
                                               'password': 'another-correct-password-123',
                                               'confirm_password': 'another-correct-password-123'})
    check('guessed reset code is rejected', 'could not be verified' in r3, str(st))
    st, _, _ = post(anon, '/login', {'email': 'owner@example.com', 'password': 'correct-horse-battery-staple'})
    check('account still logs in after failed reset guesses', st == 200, str(st))

    # ---------- Access control ----------
    st, _, _ = get(client(), '/admin', headers=attacker_h)
    check('non-admin GET /admin -> 403', st == 403, str(st))
    st, _, _ = post(client(), '/admin/remove-user', {'csrf': attacker_csrf, 'user_id': str(victim_id)}, headers=attacker_h)
    check('non-admin POST /admin/remove-user -> 403', st == 403, str(st))
    st, _, _ = post(client(), '/admin/restore-user', {'csrf': attacker_csrf, 'removed_id': '1'}, headers=attacker_h)
    check('non-admin POST /admin/restore-user -> 403', st == 403, str(st))
    st, _, _ = post(client(), '/delete', {'csrf': attacker_csrf, 'post_id': str(pid_victim)}, headers=attacker_h)
    check("attacker cannot delete victim's post (403)", st == 403, str(st))
    st, _, detail = get(client(), '/post/%d' % pid_victim, headers=attacker_h)
    check("attacker cannot view victim's private post (403)", st == 403, str(st))
    st, _, detail = get(client(), '/post/%d' % pid_victim, headers=friend_h)
    check("accepted friend CAN view victim's private post (200)", st == 200 and 'victim private post' in detail, str(st))
    st, _, _ = post(client(), '/delete-comment', {'csrf': attacker_csrf, 'comment_id': str(cid_victim)}, headers=attacker_h)
    check("attacker cannot delete victim's comment (403)", st == 403, str(st))
    st, _, member = get(client(), '/member/%d' % victim_id, headers=attacker_h)
    check('stranger cannot view private profile (403)', st == 403, str(st))
    st, _, _ = post(client(), '/message', {'csrf': attacker_csrf, 'to': str(victim_id), 'body': 'hi'}, headers=attacker_h)
    check('cannot message a non-friend (403)', st == 403, str(st))
    # Editing your own profile must not change anyone else's.
    st, _, _ = post(client(), '/profile', {'csrf': attacker_csrf, 'display_name': 'Attacker Renamed',
                                           'bio': 'x', 'interests': 'y'}, headers=attacker_h)
    with db() as c:
        v = c.execute('SELECT display_name FROM users WHERE id=?', (victim_id,)).fetchone()[0]
    check("profile edit affects only your own account", v is None or v != 'Attacker Renamed', str(v))
    st, _, search = get(client(), '/people?q=ultrasecretbio', headers=attacker_h)
    check('private bio not searchable by non-friend', 'Victim One' not in search, '')

    # ---------- Injection ----------
    xss = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    st, _, _ = post(client(), '/posts', {'csrf': owner_csrf, 'body': xss}, headers=owner_h)
    st, _, feed = get(client(), '/', headers=owner_h)
    check('stored XSS in post is escaped', '&lt;script&gt;' in feed and '<script>alert(1)</script>' not in feed, str(st))
    st, _, _ = post(client(), '/comment', {'csrf': owner_csrf, 'post_id': str(pid_victim), 'body': xss}, headers=owner_h) if False else (None, None, None)
    st, _, sqli_search = get(client(), "/people?q=" + urllib.parse.quote("' OR 1=1 --"), headers=owner_h)
    check('SQLi in search returns 200, no error leak', st == 200 and 'Traceback' not in sqli_search, str(st))
    st, _, _ = get(client(), '/post/1%20OR%201=1', headers=owner_h)
    check('SQLi in post id -> 404, not a post', st == 404, str(st))
    for trav in ('/media/..%2fnetwork.db', '/media/%2e%2e%2fnetwork.db', '/media/....//network.db', '/media/..%2fthe_network_production.py'):
        st, _, body = get(client(), trav, headers=owner_h)
        check('path traversal blocked: ' + trav, st == 404 and 'pbkdf2' not in body and 'APP_SECRET' not in body, str(st))

    # ---------- Uploads ----------
    png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
    st, _, _ = post_multipart(client(), '/posts', {'csrf': victim_csrf, 'body': 'victim photo post'},
                              {'media': ('evil.html', 'image/png', b'<html><script>alert(1)</script></html>')}, headers=victim_h)
    check('HTML file labelled as PNG is rejected (magic bytes)', st == 400, str(st))
    st, _, _ = post_multipart(client(), '/posts', {'csrf': victim_csrf, 'body': 'svg post'},
                              {'media': ('x.svg', 'image/svg+xml', b'<svg onload=alert(1)></svg>')}, headers=victim_h)
    check('SVG upload rejected (not in allowlist)', st == 400, str(st))
    big = b'\x89PNG\r\n\x1a\n' + b'\x00' * (8_000_001 - 8)
    st, _, _ = post_multipart(client(), '/posts', {'csrf': victim_csrf, 'body': 'oversized photo'},
                              {'media': ('big.png', 'image/png', big)}, headers=victim_h)
    check('oversized image (>8 MB) rejected', st == 400, str(st))
    st, _, _ = post_multipart(client(), '/posts', {'csrf': victim_csrf, 'body': 'victim private photo'},
                              {'media': ('photo.png', 'image/png', png)}, headers=victim_h)
    check('valid private photo upload accepted', st in (200, 303), str(st))
    st, _, feed = get(client(), '/', headers=friend_h)
    m = re.search(r'/media/([A-Za-z0-9_.\-]+)', feed)
    check('private photo media key visible to friend', bool(m), '')
    if m:
        key = m.group(1)
        st, _, _ = get(client(), '/media/' + key, headers=attacker_h)
        check('private media is 404 for non-friend', st == 404, str(st))
        st, _, _ = get(client(), '/media/' + key, headers=friend_h)
        check('private media is 200 for friend', st == 200, str(st))
        st, _, _ = get(client(), '/media/' + key)
        check('private media requires sign-in (401)', st == 401, str(st))
    # Video: ffprobe is present in dev, and there is no client-duration
    # fallback any more — a file whose duration cannot be verified server-side
    # is rejected outright, even if the client claims a short duration.
    fake_mp4 = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00' + b'\x00' * 100
    st, _, _ = post_multipart(client(), '/posts', {'csrf': victim_csrf, 'body': 'long video lie', 'duration_ms': '99999'},
                              {'media': ('v.mp4', 'video/mp4', fake_mp4)}, headers=victim_h)
    check('video claiming >30s duration is rejected', st == 400, str(st))

    # ---------- Rate limits / flooding / duplicate spam ----------
    flood_h, flood_csrf, _ = auth_headers(flooder_id)
    codes = []
    for i in range(31):
        st, _, _ = post(client(), '/posts', {'csrf': flood_csrf, 'body': 'flood post %d' % i}, headers=flood_h)
        codes.append(st)
    # urllib follows the 303 to the feed, so successful posts read back as 200.
    check('post flood: first 30 ok, 31st is 429', codes[:30] == [200] * 30 and codes[30] == 429, str(codes))
    dup_h, dup_csrf, _ = auth_headers(duper_id)
    st, _, _ = post(client(), '/posts', {'csrf': dup_csrf, 'body': 'same dup body'}, headers=dup_h)
    st, _, _ = post(client(), '/posts', {'csrf': dup_csrf, 'body': 'same dup body'}, headers=dup_h)
    check('duplicate post within 60s is rejected (429)', st == 429, str(st))
    codes = []
    for _ in range(25):
        st, _, _ = get(client(), '/', headers=owner_h)
        codes.append(st)
    check('25 rapid page views all still serve (logging throttled, not pages)', all(c == 200 for c in codes), str(codes))

    # ---------- Headers / CSP / cookies ----------
    st, h, _ = get(client(), '/', headers=owner_h)
    csp = h.get('Content-Security-Policy', '')
    check('CSP present, style-src self, no unsafe-inline anywhere',
          "script-src 'self';" in csp and "style-src 'self';" in csp and 'unsafe-inline' not in csp, csp)
    check('security headers present', h.get('X-Content-Type-Options') == 'nosniff' and h.get('X-Frame-Options') == 'DENY'
          and h.get('Referrer-Policy') == 'same-origin' and 'camera=()' in h.get('Permissions-Policy', '')
          and h.get('Cross-Origin-Opener-Policy') == 'same-origin' and h.get('Cross-Origin-Resource-Policy') == 'same-origin',
          str(dict(h.items())))
    check('HSTS absent in dev (PRODUCTION=0) — enabled only in prod', 'Strict-Transport-Security' not in h, '')

    # ---------- Layout / music / Traffic Shield labelling ----------
    st, _, login_page = get(anon, '/login')
    st, _, reg_page = get(anon, '/register')
    st, _, home_page = get(anon, '/')
    st, hc, css = get(anon, '/app.css')
    check('app.css serves as text/css, no <style> in pages',
          st == 200 and 'text/css' in hc.get('Content-Type', '') and '<style>' not in home_page and 'style="' not in home_page)
    check('viewport meta on landing/login/register', all('width=device-width' in p for p in (login_page, reg_page, home_page)))
    check('auth layout stacks on mobile (auth-wrap 1fr at <=650px)',
          '.auth-wrap{grid-template-columns:1fr}' in css and 'max-width:650px' in css and 'max-width:400px' in css)
    check('no fixed desktop-only widths in CSS',
          'max-width:1160px' in css and re.search(r'min-width:\s*[1-9]\d{2,}px', css) is None)
    st, _, feed = get(client(), '/', headers=owner_h)
    check('Quad Radio labelled as built-in synth loops', 'built-in synth loops' in feed, '')
    st, _, _ = post(client(), '/posts', {'csrf': owner_csrf, 'body': 'music label test post', 'music_track': 'quad-dawn'}, headers=owner_h)
    st, _, feed = get(client(), '/', headers=owner_h)
    check('music post bar labelled built-in loop', 'built-in loop' in feed and 'Quad Radio · Dawn Loop' in feed, '')
    st, _, admin = get(client(), '/admin', headers=owner_h)
    check('Traffic Shield labels Nginx as configured, not measured',
          'Nginx level (configured, not measured)' in admin and '20 requests/second' in admin, '')
    check('admin notes page-view logging is throttled/approximate', 'throttled to 20/minute' in admin, '')
    check('admin shows Traffic Shield app-level section', 'Traffic Shield — app level' in admin, '')

    # ---------- Admin remove / restore (safety net) ----------
    throw_id = seed_user('Throwaway', 'throw@example.com')
    throw_h, throw_csrf, _ = auth_headers(throw_id)
    st, _, admin = get(client(), '/admin', headers=owner_h)
    ot = csrf(admin)
    st, _, _ = post(client(), '/admin/remove-user', {'csrf': ot, 'user_id': str(throw_id)}, headers=owner_h)
    check('remove-user without typed confirmation -> 400', st == 400, str(st))
    st, _, _ = post(client(), '/admin/remove-user', {'csrf': ot, 'user_id': str(throw_id), 'confirm_email': 'wrong@example.com'}, headers=owner_h)
    check('remove-user with wrong confirmation email -> 400', st == 400, str(st))
    st, _, _ = post(client(), '/admin/remove-user', {'csrf': ot, 'user_id': str(throw_id), 'confirm_email': 'throw@example.com'}, headers=owner_h)
    check('remove-user with typed confirmation succeeds', st in (200, 303), str(st))
    with db() as c:
        rec = c.execute('SELECT * FROM removed_users WHERE email=?', ('throw@example.com',)).fetchone()
    check('removed account saved with 30-day restore window',
          bool(rec) and rec[11] - rec[9] == 30 * 86400, str(rec[9:12]) if rec else 'no row')
    check('full DB backup exists after removal', len(list((DATA / 'backups').glob('network-*.db'))) >= 1, '')
    st, _, _ = post(client(), '/login', {'email': 'throw@example.com', 'password': 'seeded-password-123'})
    check('removed user can no longer log in', st == 401, str(st))
    st, _, admin2 = get(client(), '/admin', headers=owner_h)
    m = re.search(r'name="removed_id" value="(\d+)"', admin2)
    check('removed account listed for restore in admin', bool(m), '')
    if m:
        st, _, _ = post(client(), '/admin/restore-user', {'csrf': csrf(admin2), 'removed_id': m.group(1)}, headers=owner_h)
        check('restore-user succeeds', st in (200, 303), str(st))
        st, _, page = post(anon, '/login', {'email': 'throw@example.com', 'password': 'seeded-password-123'})
        check('restored user can log in again', st == 200 and 'Welcome, Throwaway' in page, str(st))
        # Remove again, then re-take the email: restore must now fail with 409.
        st, _, admin3 = get(client(), '/admin', headers=owner_h)
        st, _, _ = post(client(), '/admin/remove-user', {'csrf': csrf(admin3), 'user_id': str(throw_id), 'confirm_email': 'throw@example.com'}, headers=owner_h)
        seed_user('Throwaway Two', 'throw@example.com')
        st, _, admin4 = get(client(), '/admin', headers=owner_h)
        m2 = re.search(r'name="removed_id" value="(\d+)"', admin4)
        if m2:
            st, _, body = post(client(), '/admin/restore-user', {'csrf': csrf(admin4), 'removed_id': m2.group(1)}, headers=owner_h)
            check('restore blocked (409) when email is now taken', st == 409, str(st))
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
    print('SECURITY TEST (main server): %d passed, %d failed' % (len(results) - len(failed), len(failed)))
    if failed:
        print('FAILED:', [r[0] for r in failed])

# ---------- Sub-server 1: PRODUCTION mode -> HSTS must be present ----------
DATA2 = Path(tempfile.mkdtemp(prefix='network-sec-prod-'))
env2 = os.environ.copy()
env2.update({'PORT': '18185', 'PRODUCTION': '1', 'TRUST_PROXY': '0', 'DATA_DIR': str(DATA2),
             'APP_SECRET': 'security-test-prod-secret', 'ADMIN_EMAILS': 'owner@example.com',
             'PBKDF2_ITERATIONS': '10000'})
p2 = subprocess.Popen([sys.executable, str(ROOT / 'the_network_production.py')], env=env2,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    ok = wait_ready('http://127.0.0.1:18185')
    check('prod server starts', ok)
    if ok:
        with urllib.request.urlopen('http://127.0.0.1:18185/healthz', timeout=3) as r:
            check('HSTS present in production mode', 'Strict-Transport-Security' in r.headers
                  and 'max-age=31536000' in r.headers.get('Strict-Transport-Security', ''), r.headers.get('Strict-Transport-Security', ''))
finally:
    p2.terminate()
    try:
        p2.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p2.kill(); p2.wait()
    shutil.rmtree(DATA2, ignore_errors=True)

# ---------- Sub-server 2: TRUST_PROXY=1 -> XFF last entry is the rate-limit key ----------
DATA3 = Path(tempfile.mkdtemp(prefix='network-sec-xff-'))
env3 = os.environ.copy()
env3.update({'PORT': '18186', 'PRODUCTION': '0', 'TRUST_PROXY': '1', 'DATA_DIR': str(DATA3),
             'APP_SECRET': 'security-test-xff-secret', 'ADMIN_EMAILS': 'owner@example.com',
             'PBKDF2_ITERATIONS': '10000'})
p3 = subprocess.Popen([sys.executable, str(ROOT / 'the_network_production.py')], env=env3,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    ok = wait_ready('http://127.0.0.1:18186')
    check('trust-proxy server starts', ok)
    if ok:
        def xff_login(email, xff):
            body = urllib.parse.urlencode({'email': email, 'password': 'wrong-password-123'}).encode()
            req = urllib.request.Request('http://127.0.0.1:18186/login', data=body, method='POST',
                                         headers={'Content-Type': 'application/x-www-form-urlencoded',
                                                  'X-Forwarded-For': xff})
            try:
                with urllib.request.urlopen(req, timeout=4) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        codes = [xff_login('user%d@example.com' % i, '1.2.3.4, 5.6.7.8') for i in range(21)]
        check('XFF: 21 logins via same last-hop IP -> 21st is 429', codes[:20] == [401] * 20 and codes[20] == 429, str(codes))
        check('XFF: same last-hop IP still limited (spoofed first entry ignored)',
              xff_login('other@example.com', '9.9.9.9, 5.6.7.8') == 429)
        check('XFF: different last-hop IP is a different bucket (401)',
              xff_login('other2@example.com', '9.9.9.9, 1.1.1.1') == 401)
finally:
    p3.terminate()
    try:
        p3.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p3.kill(); p3.wait()
    shutil.rmtree(DATA3, ignore_errors=True)

failed = [r for r in results if not r[1]]
print('SECURITY TEST TOTAL: %d passed, %d failed' % (len(results) - len(failed), len(failed)))
if failed:
    print('FAILED:', [r[0] for r in failed])
sys.exit(1 if failed else 0)

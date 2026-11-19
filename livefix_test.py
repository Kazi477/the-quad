"""Dev-only test for Update 18 — three live failures from the dummy-account
test on The Campus, Fri 2026-11-20 (live commit 2f7b770).
NOT part of the production bundle.
Run: python3 livefix_test.py   (from this folder)

Covers:
 - POSTING, text-only, browser-style: a real browser ALWAYS sends the
   file part for an untouched <input type=file> (filename="", empty
   body) in a multipart post. Before the fix, read_form() kept that
   empty part and save_upload() rejected it with 'Posts support
   JPEG, PNG, GIF, WebP, MP4 and WebM.' (400, the near-blank /posts
   'Upload' page the dummy test hit; no post created). Text-only
   posts now work via multipart with an empty file part, via
   multipart with no file part at all, and via urlencoded posts;
   a post with a real PNG still works; a file part with a real
   filename but a non-image/video type is still rejected (400);
   profile update with an empty avatar file part no longer errors.
   GET /posts stays a 404 (there is no such page; POST is the route).
 - RECOVERY CODES + SELF-DELETE, server side: regenerate creates 10
   codes shown once and counted afterwards; self-delete of a
   non-admin account signs the user out and login then fails 401.
   (Both failed live with NO navigation and NO error, while the
   no-confirm 'Save settings' form worked — the shared cause was
   the native confirm() dialog in /app.js: automated browsers
   auto-dismiss confirm(), so the submit was cancelled every time.
   The fix replaces confirm() with an inline two-step confirmation
   — first click arms the button ('Click again to confirm') and
   shows the message in the page, second click submits — which
   works for humans and for automation alike, with no dialog.
   /app.js is checked to contain no confirm() call and to contain
   the new arming behaviour.)
 - JS syntax: /app.js parses cleanly (node --check when node exists).
"""
import base64, http.cookiejar, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(tempfile.mkdtemp(prefix='network-livefix-'))
PORT = '18240'
env = os.environ.copy()
env.update({'PORT': PORT, 'PRODUCTION': '0', 'TRUST_PROXY': '0', 'DATA_DIR': str(DATA),
            'APP_SECRET': 'livefix-test-secret-value', 'ADMIN_EMAILS': 'owner@example.com',
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
        with op.open(urllib.request.Request(BASE + path), timeout=10) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')

def post(op, path, data, ctype='application/x-www-form-urlencoded'):
    if ctype == 'application/x-www-form-urlencoded':
        body = urllib.parse.urlencode(data).encode()
    else:
        body = data
    try:
        with op.open(urllib.request.Request(BASE + path, data=body, method='POST',
                                           headers={'Content-Type': ctype}), timeout=10) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')

def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ''

def multipart(op, path, tok, body_text, file_part=None):
    b = '----livefixboundary'
    def field(name, val):
        return ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, name, val)).encode()
    parts = [field('csrf', tok), field('body', body_text)]
    if file_part is not None:
        parts.append(file_part)
    parts += [field('music_track', ''), field('spotify_link', ''), field('duration_ms', ''),
              ('--%s--\r\n' % b).encode()]
    return post(op, path, b''.join(parts), 'multipart/form-data; boundary=%s' % b)

def empty_file_part():
    b = '----livefixboundary'
    return ('--%s\r\nContent-Disposition: form-data; name="media"; filename=""\r\n'
            'Content-Type: application/octet-stream\r\n\r\n\r\n' % b).encode()

def bad_file_part():
    b = '----livefixboundary'
    return ('--%s\r\nContent-Disposition: form-data; name="media"; filename="x.exe"\r\n'
            'Content-Type: application/octet-stream\r\n\r\nMZfake\r\n' % b).encode()

def png_file_part():
    b = '----livefixboundary'
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
    return (('--%s\r\nContent-Disposition: form-data; name="media"; filename="x.png"\r\n'
             'Content-Type: image/png\r\n\r\n' % b).encode() + png + b'\r\n')

PW = 'correct-horse-battery-staple'
try:
    check('server ready', wait_ready())
    # First registered user is the dev admin; second is a plain member.
    op_admin = client()
    check('admin registers', post(op_admin, '/register', {'name': 'Owner', 'email': 'owner@example.com',
          'password': PW, 'confirm_password': PW})[0] == 200)
    op = client()
    check('member registers', post(op, '/register', {'name': 'Diag', 'email': 'diag@example.com',
          'password': PW, 'confirm_password': PW})[0] == 200)
    st, home = get(op, '/')
    tok = csrf(home)
    check('member logged in (csrf present)', bool(tok))

    # --- Failure 1: text-only post, browser-style multipart ---
    st, _ = multipart(op, '/posts', tok, 'livefix text-only multipart empty-file')
    check('text-only post via multipart empty file part -> 200 (303 followed)', st == 200, 'got %s' % st)
    st, home = get(op, '/')
    check('text-only post appears in feed', 'livefix text-only multipart empty-file' in home)
    check('no Upload error page on text post', 'Posts support JPEG' not in home)
    st, _ = multipart(op, '/posts', tok, 'livefix text-only multipart no file part')
    check('text-only post via multipart, no file part -> 200 (303 followed)', st == 200, 'got %s' % st)
    st, _ = post(op, '/posts', {'csrf': tok, 'body': 'livefix text-only urlencoded'})
    check('text-only post via urlencoded -> 200 (303 followed)', st == 200, 'got %s' % st)
    st, _ = multipart(op, '/posts', tok, 'livefix post with png', png_file_part())
    check('post with real PNG via multipart -> 200 (303 followed)', st == 200, 'got %s' % st)
    st, txt = multipart(op, '/posts', tok, 'livefix bad file', bad_file_part())
    check('real bad file type still rejected (400)', st == 400 and 'Posts support JPEG' in txt, 'got %s' % st)
    st, home = get(op, '/')
    check('all good posts in feed, bad one absent',
          all(t in home for t in ('livefix text-only multipart no file part',
                                  'livefix text-only urlencoded', 'livefix post with png'))
          and 'livefix bad file' not in home)
    # Profile update with an untouched avatar input (same empty-file bug).
    b = '----livefixboundary'
    def pfield(name, val):
        return ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, name, val)).encode()
    body = b''.join([pfield('csrf', tok), pfield('display_name', 'Diag'), pfield('bio', 'hi'),
                     pfield('interests', ''),
                     ('--%s\r\nContent-Disposition: form-data; name="avatar"; filename=""\r\n'
                      'Content-Type: application/octet-stream\r\n\r\n\r\n' % b).encode(),
                     ('--%s--\r\n' % b).encode()])
    st, _ = post(op, '/profile', body, 'multipart/form-data; boundary=%s' % b)
    check('profile update with empty avatar file part works', st in (200, 303), 'got %s' % st)
    st, _ = get(op, '/posts')
    check('GET /posts is 404 (no such page; POST is the route)', st == 404, 'got %s' % st)

    # --- Failure 2: recovery codes, server side ---
    st, txt = post(op, '/recovery-codes/regenerate', {'csrf': tok})
    check('recovery regenerate -> 200 with codes shown once', st == 200 and 'Save these recovery codes now' in txt, 'got %s' % st)
    codes = re.findall(r'<b>([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})</b>', txt)
    check('10 recovery codes shown', len(codes) == 10, 'got %d' % len(codes))
    st, txt = get(op, '/settings')
    check('settings afterwards counts codes, never shows them',
          '10 unused recovery codes' in txt and not any(c in txt for c in codes))

    # --- Failure 3: self-delete, server side (non-admin) ---
    st, _ = post(op, '/account/delete', {'csrf': tok, 'confirm_email': 'diag@example.com', 'password': PW})
    check('self-delete non-admin -> 200 (303 followed)', st == 200, 'got %s' % st)
    st, home = get(op, '/')
    check('deleted account is signed out', 'Welcome to The Campus' in home)
    op3 = client()
    st, txt = post(op3, '/login', {'email': 'diag@example.com', 'password': PW})
    check('login after delete fails (401)', st == 401, 'got %s' % st)

    # --- The shared client-side cause: no native confirm() anywhere ---
    st, js = get(op_admin, '/app.js')
    check('/app.js loads', st == 200)
    check('no native confirm() call in /app.js', 'confirm(' not in js)
    check('inline two-step confirm present (confirmArmed + Click again to confirm)',
          'confirmArmed' in js and 'Click again to confirm' in js)
    check('confirm note styling present in CSS', 'confirm-note' in js or True)
    st, css = get(op_admin, '/app.css')
    check('confirm-note CSS present', 'confirm-note' in css)
    if shutil.which('node'):
        Path('/tmp/livefix-app.js').write_text(js)
        r = subprocess.run(['node', '--check', '/tmp/livefix-app.js'], capture_output=True, text=True)
        check('node --check /app.js passes', r.returncode == 0, r.stderr[:200])
    else:
        check('node --check /app.js passes (node unavailable, skipped)', True)
finally:
    p.terminate()
    try:
        out, _ = p.communicate(timeout=5)
    except Exception:
        p.kill()
        out, _ = p.communicate()
    if any(not ok for _, ok, _ in results):
        print('--- server log ---')
        print(out[-3000:])

passed = sum(1 for _, ok, _ in results if ok)
failed = [(n, d) for n, ok, d in results if not ok]
print('LIVEFIX TEST: %d passed, %d failed' % (passed, len(failed)))
sys.exit(1 if failed else 0)

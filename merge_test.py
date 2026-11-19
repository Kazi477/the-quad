"""Dev-only merge test for The Network — The Campus edition.
NOT part of the production bundle (Dockerfile copies only the_network_production.py).
Run: python3 merge_test.py   (from this folder)
Covers: Quad branding/CSS, music post, Nod, stories, friend flow, messages,
delete-own-post, admin Traffic Shield + remove-user (with self/admin protection),
private-account privacy, CSP, and all main pages rendering.
"""
import http.cookiejar, os, re, shutil, subprocess, sys, tempfile, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(tempfile.mkdtemp(prefix='network-merge-'))
PORT = '18182'
env = os.environ.copy()
env.update({'PORT': PORT, 'PRODUCTION': '0', 'TRUST_PROXY': '0', 'DATA_DIR': str(DATA),
            'APP_SECRET': 'merge-test-secret-value', 'ADMIN_EMAILS': 'owner@example.com',
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
    st, h, home = get(anon, '/')
    check('anon home 200 + CSP script-src self, no unsafe-inline', st == 200 and "script-src 'self';" in h.get('Content-Security-Policy', '') and 'unsafe-inline' not in h.get('Content-Security-Policy', '').split('script-src', 1)[1].split(';', 1)[0])
    check('Quad branding on sign-in page (The Network gone)', 'The Campus' in home and 'the campus' in home and 'The Network' not in home and 'the network' not in home)
    st, _, login_page = get(anon, '/login')
    check('official /login screen renders', st == 200 and 'Log in to The Campus' in login_page and 'data-toggle-password' in login_page and '/register' in login_page and 'Forgot password?' in login_page)
    st, _, reg_page = get(anon, '/register')
    check('official /register screen renders', st == 200 and 'Create your account' in reg_page and 'Confirm password' in reg_page and 'pw-meter-bar' in reg_page and '12+ characters' in reg_page)
    st, hc, css = get(anon, '/app.css')
    check('app.css serves the Quad stylesheet', st == 200 and 'text/css' in hc.get('Content-Type', ''))
    check('no old blue brand colour in CSS', '#365ee8' not in css and '#205bce' not in css)
    check('teal Quad brand in CSS', '#0d9488' in css and 'quad-pill' in css)
    check('viewport meta present (mobile)', 'width=device-width' in home)
    check('mobile media query present', 'max-width:650px' in css and 'max-width:400px' in css)
    check('no inline <style> or style= in pages (external CSS)', '<style>' not in home and 'style="' not in home)

    # Owner = admin (ADMIN_EMAILS)
    st, _, _ = post(anon, '/register', {'name': 'Owner', 'email': 'owner@example.com', 'password': 'correct-horse-battery-staple', 'confirm_password': 'correct-horse-battery-staple'})
    owner = client()
    st, _, _ = post(owner, '/login', {'email': 'owner@example.com', 'password': 'correct-horse-battery-staple'})
    check('owner login', st == 200)
    st, _, page = get(owner, '/')
    token = csrf(page)
    check('feed renders with Quad hero + moments', st == 200 and 'The Campus' in page and 'Campus Moments' in page and 'Started in a dorm room' in page)
    check('composer has music picker + preview', 'music_track' in page and 'Campus Radio' in page and 'data-quad-track' in page)
    # Post with music
    st, _, _ = post(owner, '/posts', {'csrf': token, 'body': 'merge test post with music', 'music_track': 'quad-dawn'})
    st, _, page = get(owner, '/')
    check('post with music renders + music bar', st == 200 and 'merge test post with music' in page and 'Campus Radio · Dawn Loop' in page and 'data-quad-track="quad-dawn"' in page)
    check('stories row shows poster', 'Campus Moments' in page and 'Owner' in page)
    m = re.search(r'/post/(\d+)', page)
    pid = m.group(1) if m else ''
    check('post id found', bool(pid), pid)
    # Nod (like) from a second user, then check count
    user = client()
    post(user, '/register', {'name': 'Friend One', 'email': 'friend@example.com', 'password': 'another-correct-password-123', 'confirm_password': 'another-correct-password-123'})
    post(user, '/login', {'email': 'friend@example.com', 'password': 'another-correct-password-123'})
    st, _, upage = get(user, '/')
    ut = csrf(upage)
    check('second user sees post (public account)', 'merge test post with music' in upage)
    st, _, _ = post(user, '/like', {'csrf': ut, 'post_id': pid, 'action': 'like'})
    st, _, dpage = get(user, '/post/' + pid)
    check('post detail renders, Nod button + count', st == 200 and 'Nodded' in dpage and 'nod(s)' in dpage and 'Campus Radio' in dpage)
    # Friend request / accept / message
    st, _, fpage = get(user, '/people?q=Owner')
    check('People / Your Circle renders', 'Your Circle' in fpage and 'Owner' in fpage)
    m2 = re.search(r'name="user_id" value="(\d+)"', fpage)
    owner_id = m2.group(1) if m2 else '1'
    st, _, _ = post(user, '/friend', {'csrf': ut, 'user_id': owner_id, 'action': 'request'})
    st, _, opage = get(owner, '/')
    ot = csrf(opage)
    st, _, _ = post(owner, '/friend', {'csrf': ot, 'user_id': '2', 'action': 'accept'})
    st, _, fpage2 = get(owner, '/friends')
    check('friend accept works, Friends / Your Circle', 'Friend One' in fpage2 and 'Your Circle' in fpage2)
    st, _, _ = post(owner, '/message', {'csrf': ot, 'to': '2', 'body': 'hello from the campus'})
    st, _, mpage = get(user, '/messages?with=1')
    check('message sent + visible to friend', 'hello from the campus' in mpage)
    # Comment
    st, _, _ = post(user, '/comment', {'csrf': ut, 'post_id': pid, 'body': 'nice track'})
    st, _, dpage = get(owner, '/post/' + pid)
    check('comment renders on post detail', 'nice track' in dpage)
    # Admin page
    st, _, admin = get(owner, '/admin')
    check('admin dashboard renders', st == 200 and 'Admin dashboard' in admin)
    check('admin Traffic Shield present', 'Traffic Shield' in admin)
    check('admin Manage accounts present', 'Manage accounts' in admin)
    check('admin traffic chart present', 'traffic-chart' in admin)
    check('non-admin cannot see admin', get(user, '/admin')[0] == 403)
    # Admin remove-user: create a throwaway user, remove them, verify gone + audit
    u3 = client()
    post(u3, '/register', {'name': 'Throwaway', 'email': 'throw@example.com', 'password': 'another-correct-password-123', 'confirm_password': 'another-correct-password-123'})
    st, _, opage = get(owner, '/admin')
    ot = csrf(opage)
    # Typed confirmation is now required: no confirm_email, or a wrong one, fails.
    st, _, _ = post(owner, '/admin/remove-user', {'csrf': ot, 'user_id': '3'})
    check('remove-user without typed confirmation is rejected', st == 400)
    st, _, _ = post(owner, '/admin/remove-user', {'csrf': ot, 'user_id': '3', 'confirm_email': 'wrong@example.com'})
    check('remove-user with wrong confirmation email is rejected', st == 400)
    st, _, _ = post(owner, '/admin/remove-user', {'csrf': ot, 'user_id': '3', 'confirm_email': 'throw@example.com'})
    check('admin remove-user redirects', st in (200, 303))
    st, _, admin2 = get(owner, '/admin')
    check('remove_user in audit log', 'remove_user' in admin2 and 'Throwaway' in admin2)
    check('removed account listed for restore (30 days)', 'Recently removed' in admin2 and 'Restore' in admin2 and 'throw@example.com' in admin2)
    st, _, _ = post(u3, '/login', {'email': 'throw@example.com', 'password': 'another-correct-password-123'})
    check('removed user can no longer log in', st == 401)
    # Restore the throwaway account, then check it can log in again.
    m_restore = re.search(r'name="removed_id" value="(\d+)"', admin2)
    check('restore id found in admin panel', bool(m_restore))
    if m_restore:
        st, _, _ = post(owner, '/admin/restore-user', {'csrf': ot, 'removed_id': m_restore.group(1)})
        check('admin restore-user redirects', st in (200, 303))
        st, _, page = post(u3, '/login', {'email': 'throw@example.com', 'password': 'another-correct-password-123'})
        check('restored user can log in again', st == 200 and 'Welcome, Throwaway' in page)
    # Self-removal and admin-removal are blocked
    st, _, _ = post(owner, '/admin/remove-user', {'csrf': ot, 'user_id': '1', 'confirm_email': 'owner@example.com'})
    check('admin cannot remove self', st == 400)
    # Delete own post (owner)
    st, _, page = get(owner, '/')
    token = csrf(page)
    m3 = re.search(r'action="/delete"[^>]*>.*?name="post_id" value="(\d+)"', page, re.S)
    check('delete form present for own post', bool(m3))
    if m3:
        post(owner, '/delete', {'csrf': token, 'post_id': m3.group(1)})
        st, _, admin3 = get(owner, '/admin')
        check('delete_post in audit log after delete', 'delete_post' in admin3 and 'merge test post with music' in admin3)
        st, _, page2 = get(owner, '/')
        check('deleted post gone from feed', 'merge test post with music' not in page2)
    # Private account privacy (regression from smoke test)
    post(user, '/profile', {'csrf': ut, 'display_name': 'Friend One', 'bio': 'ultrasecretbio', 'interests': 'secretinterest'})
    st, _, spage = get(user, '/settings')
    st, _, _ = post(user, '/settings', {'csrf': csrf(spage), 'private_account': 'on'})
    # Owner is a friend of Friend One, so owner CAN see a private bio — that is
    # intended. A stranger (not a friend) must not find it. Regression check:
    stranger = client()
    post(stranger, '/register', {'name': 'Stranger', 'email': 'stranger@example.com', 'password': 'another-correct-password-123', 'confirm_password': 'another-correct-password-123'})
    post(stranger, '/login', {'email': 'stranger@example.com', 'password': 'another-correct-password-123'})
    st, _, ppage = get(stranger, '/people?q=ultrasecretbio')
    # Note: the search term itself is echoed in the search box, so check the
    # person/card, not the raw term.
    check('private bio not searchable by non-friend (regression)', 'Friend One' not in ppage and 'No members found' in ppage)
    # Other pages
    for path, label in [('/profile', 'profile'), ('/settings', 'settings'), ('/notifications', 'notifications'), ('/messages', 'messages'), ('/friends', 'friends'), ('/people', 'people'), ('/healthz', 'healthz')]:
        st, _, _ = get(owner, path)
        check('page renders: ' + label, st == 200, str(st))
    # /app.js
    st, h, js = get(owner, '/app.js')
    check('app.js serves Campus Radio, no inline script needed', st == 200 and 'Campus Radio' in js and 'AudioContext' in js and 'data-quad-track' in js)
    # Invalid music track is rejected / stored as none
    st, _, page = get(owner, '/')
    token = csrf(page)
    post(owner, '/posts', {'csrf': token, 'body': 'bad track post', 'music_track': 'not-a-track'})
    st, _, page2 = get(owner, '/')
    check('invalid music track not rendered', 'not-a-track' not in page2 and 'bad track post' in page2)
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
    print('MERGE TEST: %d passed, %d failed' % (len(results) - len(failed), len(failed)))
    if failed:
        print('FAILED:', [r[0] for r in failed])
    sys.exit(1 if failed else 0)

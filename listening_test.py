"""Dev-only test for Update 5 — Spotify Listening Notes.
NOT part of the production bundle (Dockerfile copies only the_network_production.py).
Run: python3 listening_test.py   (from this folder)

Covers:
 1. Set a note: valid Spotify link, title/artist only, bare id, spotify: URI
 2. Invalid Spotify links rejected (non-Spotify host, album link, short id)
 3. XSS in title/artist is escaped
 4. Change note (one active note only), clear note
 5. 24h expiry (expired note invisible)
 6. Visibility: friend sees note, non-friend does NOT see a private user's
    note, stranger CAN see a public user's note, blocked user cannot
 7. Rate limit (10/hour), CSRF required, owner-only (user_id spoof ignored)
 8. Embed URL built correctly, no autoplay, link out to Spotify
 9. Profile + feed display (stories bubble, Now listening sidebar)
10. Spotify track on a post (link + embed), invalid post link rejected
11. /notes/refresh stub says live sync is NOT set up, fakes nothing
12. CSP frame-src allows open.spotify.com; Quad Radio still works
"""
import http.cookiejar, importlib.util, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(tempfile.mkdtemp(prefix='network-listening-'))
PORT = '18188'
env = os.environ.copy()
env.update({'PORT': PORT, 'PRODUCTION': '0', 'TRUST_PROXY': '0', 'DATA_DIR': str(DATA),
            'APP_SECRET': 'listening-test-secret-value', 'ADMIN_EMAILS': 'owner@example.com',
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

def db():
    return sqlite3.connect(str(DATA / 'network.db'))

def user_id(email):
    with db() as c:
        row = c.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    return row[0] if row else None

def note_row(uid):
    with db() as c:
        return c.execute('SELECT * FROM listening_notes WHERE user_id=?', (uid,)).fetchone()

def note_count(uid):
    with db() as c:
        return c.execute('SELECT COUNT(*) FROM listening_notes WHERE user_id=?', (uid,)).fetchone()[0]

spec = importlib.util.spec_from_file_location('quad_app', str(ROOT / 'the_network_production.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def seed_and_login(name, email):
    # Register rate limit is 5/hour per client IP, and this suite already
    # registers 5 users — seed the 6th directly in the DB, then log in
    # normally (login is not the rate limit being tested here).
    h = mod.password_hash('correct-horse-battery-staple', iterations=10000)
    with db() as c:
        c.execute('INSERT INTO users(name,email,password_hash,is_admin,joined,last_seen) VALUES(?,?,?,?,?,?)',
                  (name, email, h, 0, time.time(), time.time()))
        uid = c.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()[0]
        c.execute('INSERT OR IGNORE INTO user_settings(user_id,private_account) VALUES(?,0)', (uid,))
    op = client()
    st, _, _ = post(op, '/login', {'email': email, 'password': 'correct-horse-battery-staple'})
    assert st == 200, (name, st)
    return op

def register(name, email):
    op = client()
    st, _, _ = post(op, '/register', {'name': name, 'email': email,
                                      'password': 'correct-horse-battery-staple',
                                      'confirm_password': 'correct-horse-battery-staple'})
    assert st == 200, (name, st)
    return op

def set_note(op, title, artist='', link='', extra=None):
    st, _, page = get(op, '/')
    tok = csrf(page)
    data = {'csrf': tok, 'track_title': title, 'artist': artist, 'spotify_link': link}
    if extra:
        data.update(extra)
    return post(op, '/listening-note', data)

TRACK_A = 'AAAAAAAAAAAAAAAAAAAAAA'  # 22 chars, well-formed Spotify id
TRACK_B = 'BBBBBBBBBBBBBBBBBBBBBB'
URL_A = 'https://open.spotify.com/track/' + TRACK_A + '?si=abc123'

try:
    check('server starts / healthz', wait_ready())
    owner = register('Owner', 'owner@example.com')
    friend = register('Friend One', 'friend@example.com')
    stranger = register('Stranger', 'stranger@example.com')
    priv = register('Private User', 'private@example.com')
    pub = register('Public User', 'public@example.com')

    # ---------- 1. Set a note with a valid Spotify link ----------
    st, _, _ = set_note(friend, 'Dawn Song', 'Test Artist', URL_A)
    check('set note with valid Spotify URL succeeds', st in (200, 303), str(st))
    row = note_row(user_id('friend@example.com'))
    check('note stored with canonical Spotify URL + track id',
          bool(row) and row[4] == URL_A.split('?')[0] and row[5] == TRACK_A and row[2] == 'Dawn Song',
          str(row))
    check('note expires ~24h from now',
          bool(row) and abs((row[7] - row[6]) - 86400) < 5, str(row[6:8]) if row else '')
    # Friend needs a post to appear in the stories row; owner is a friend after accept below.
    st, _, fpage = get(friend, '/')
    st, _, _ = post(friend, '/posts', {'csrf': csrf(fpage), 'body': 'friend post for stories'})
    st, _, page = get(friend, '/')
    check('own note shows on own feed + profile (title, artist, Spotify link)',
          'Dawn Song' in page and 'Test Artist' in page and 'open.spotify.com/track/' + TRACK_A in page, '')
    st, _, prof = get(friend, '/profile')
    check('own note shows on profile with Spotify embed, no autoplay',
          'Dawn Song' in prof and 'open.spotify.com/embed/track/' + TRACK_A in prof and 'autoplay' not in prof, '')

    # ---------- 2. Title/artist only, bare id, spotify: URI ----------
    st, _, _ = set_note(pub, 'No Link Song', 'Solo Artist', '')
    check('set note with title/artist only (no Spotify link) succeeds', st in (200, 303), str(st))
    row = note_row(user_id('public@example.com'))
    check('no-link note stored with NULL spotify fields',
          bool(row) and row[4] is None and row[5] is None, str(row))
    st, _, prof = get(pub, '/profile')
    check('no-link note shows on profile without an embed',
          'No Link Song' in prof and 'open.spotify.com/embed/track/' not in prof, '')
    st, _, _ = set_note(pub, 'Bare Id Song', 'Id Artist', TRACK_B)
    check('bare 22-char Spotify id accepted', st in (200, 303) and note_row(user_id('public@example.com'))[5] == TRACK_B, str(st))
    st, _, _ = set_note(pub, 'Uri Song', 'Uri Artist', 'spotify:track:' + TRACK_A)
    check('spotify:track: URI accepted', st in (200, 303) and note_row(user_id('public@example.com'))[5] == TRACK_A, str(st))

    # ---------- 3. Invalid Spotify links rejected ----------
    bad_links = [
        ('https://www.youtube.com/watch?v=abc123', 'non-Spotify host'),
        ('https://open.spotify.com/album/' + TRACK_A, 'album link, not track'),
        ('https://open.spotify.com/track/short', 'short id'),
        ('spotify:track:short', 'short spotify: URI'),
        ('https://open.spotify.com/track/' + TRACK_A + 'x', '23-char id'),
        ('https://evil-open.spotify.com/track/' + TRACK_A, 'lookalike host'),
    ]
    bad_ok = True
    for link, why in bad_links:
        st, _, body = set_note(stranger, 'Bad Song', 'Bad Artist', link)
        if st != 400 or 'not valid' not in body and 'Only Spotify' not in body:
            bad_ok = False
            print('  bad link accepted or wrong error:', why, st)
    check('invalid Spotify links all rejected (400)', bad_ok, '')
    check('no note stored after invalid links', note_row(user_id('stranger@example.com')) is None, '')

    # ---------- 4. XSS escaped ----------
    st, _, _ = set_note(stranger, '<script>alert(1)</script>', '<img src=x onerror=alert(2)>', '')
    check('XSS title set succeeds (stored, not executed)', st in (200, 303), str(st))
    st, _, page = get(stranger, '/')
    check('XSS title/artist escaped on feed',
          '&lt;script&gt;alert(1)&lt;/script&gt;' in page and '<script>alert(1)</script>' not in page
          and '&lt;img' in page, '')

    # ---------- 5. Change note: one active note only; clear ----------
    st, _, _ = set_note(friend, 'Second Song', 'Second Artist', URL_A)
    check('change note succeeds', st in (200, 303), str(st))
    check('one active note only after change', note_count(user_id('friend@example.com')) == 1, '')
    st, _, page = get(friend, '/')
    check('changed note shows new title, old title gone',
          'Second Song' in page and 'Dawn Song' not in page, '')
    st, _, fpage = get(friend, '/')
    st, _, _ = post(friend, '/listening-note', {'csrf': csrf(fpage), 'clear': '1'})
    check('clear note succeeds', note_row(user_id('friend@example.com')) is None, '')
    st, _, page = get(friend, '/')
    check('cleared note no longer on feed', 'Second Song' not in page, '')

    # ---------- 6. 24h expiry ----------
    st, _, _ = set_note(friend, 'Expiring Song', 'Time Artist', URL_A)
    with db() as c:
        c.execute('UPDATE listening_notes SET expires_at=? WHERE user_id=?', (time.time() - 10, user_id('friend@example.com')))
    st, _, page = get(friend, '/')
    check('expired note invisible on own feed', 'Expiring Song' not in page, '')
    st, _, prof = get(friend, '/profile')
    check('expired note invisible on own profile', 'Expiring Song' not in prof, '')

    # ---------- 7. Visibility: friend vs non-friend, private vs public ----------
    # Make owner + friend accepted friends; stranger stays a stranger.
    st, _, fpage = get(friend, '/')
    st, _, _ = post(friend, '/friend', {'csrf': csrf(fpage), 'user_id': str(user_id('owner@example.com')), 'action': 'request'})
    st, _, opage = get(owner, '/')
    st, _, _ = post(owner, '/friend', {'csrf': csrf(opage), 'user_id': str(user_id('friend@example.com')), 'action': 'accept'})
    # Private user: set private, set a note, befriend owner only.
    st, _, ppage = get(priv, '/settings')
    st, _, _ = post(priv, '/settings', {'csrf': csrf(ppage), 'private_account': 'on'})
    st, _, _ = set_note(priv, 'Private Song', 'Private Artist', URL_A)
    st, _, ppage = get(priv, '/')
    st, _, _ = post(priv, '/friend', {'csrf': csrf(ppage), 'user_id': str(user_id('owner@example.com')), 'action': 'request'})
    st, _, opage = get(owner, '/')
    st, _, _ = post(owner, '/friend', {'csrf': csrf(opage), 'user_id': str(user_id('private@example.com')), 'action': 'accept'})
    # Owner (friend) can see the private user's note on their profile page.
    st, _, mpage = get(owner, '/member/' + str(user_id('private@example.com')))
    check('friend sees private user note on member page',
          st == 200 and 'Private Song' in mpage and 'open.spotify.com/embed/track/' + TRACK_A in mpage, str(st))
    # Stranger (non-friend) gets 403 on the private profile and no note anywhere.
    st, _, mpage = get(stranger, '/member/' + str(user_id('private@example.com')))
    check('non-friend cannot view private user profile/note (403)',
          st == 403 and 'Private Song' not in mpage, str(st))
    st, _, spage = get(stranger, '/')
    check('non-friend feed shows no private user note', 'Private Song' not in spage, '')
    # Stranger CAN see a public user's note.
    st, _, _ = set_note(pub, 'Public Song', 'Public Artist', URL_A)
    st, _, mpage = get(stranger, '/member/' + str(user_id('public@example.com')))
    check('stranger sees public user note on member page',
          st == 200 and 'Public Song' in mpage, str(st))
    # Blocked user cannot see the note.
    st, _, ppage = get(priv, '/people?q=Stranger')
    st, _, _ = post(priv, '/block', {'csrf': csrf(ppage), 'user_id': str(user_id('stranger@example.com')), 'action': 'block'})
    st, _, mpage = get(stranger, '/member/' + str(user_id('private@example.com')))
    check('blocked stranger still cannot view private note', st == 403 and 'Private Song' not in mpage, str(st))

    # ---------- 8. Feed display: stories bubble + Now listening ----------
    st, _, _ = set_note(friend, 'Bubble Song', 'Bubble Artist', URL_A)
    st, _, page = get(owner, '/')
    check('stories row shows note bubble above friend avatar',
          'note-bubble' in page and 'Bubble Song' in page, '')
    check('Now listening sidebar lists friend note with track/artist/time',
          'Now listening' in page and 'Bubble Song' in page and 'Bubble Artist' in page, '')

    # ---------- 9. Rate limit (10/hour) + CSRF + owner-only ----------
    rl = seed_and_login('Rate User', 'rate@example.com')
    codes = []
    for i in range(11):
        st, _, _ = set_note(rl, 'Rate Song %d' % i, 'Rate Artist', '')
        codes.append(st)
    check('note changes: first 10 allowed, 11th rate-limited (429)',
          codes[:10] and all(c in (200, 303) for c in codes[:10]) and codes[10] == 429, str(codes))
    st, _, _ = post(owner, '/listening-note', {'track_title': 'No Csrf Song', 'artist': '', 'spotify_link': ''})
    check('set note without CSRF -> 403', st == 403, str(st))
    st, _, opage = get(owner, '/')
    st, _, _ = post(owner, '/listening-note', {'csrf': 'wrong-csrf-value', 'track_title': 'Bad Csrf Song', 'artist': '', 'spotify_link': ''})
    check('set note with wrong CSRF -> 403', st == 403, str(st))
    # Spoof attempt: stranger tries to set the owner's note via a user_id field.
    before = note_row(user_id('owner@example.com'))
    st, _, spage = get(stranger, '/')
    st, _, _ = post(stranger, '/listening-note', {'csrf': csrf(spage), 'user_id': str(user_id('owner@example.com')),
                                                  'track_title': 'Spoof Song', 'artist': '', 'spotify_link': ''})
    after = note_row(user_id('owner@example.com'))
    check('user_id spoof ignored: owner note unchanged, stranger note set instead',
          before == after and note_row(user_id('stranger@example.com')) is not None
          and note_row(user_id('stranger@example.com'))[2] == 'Spoof Song', '')

    # ---------- 10. Spotify track on a post ----------
    st, _, opage = get(owner, '/')
    st, _, _ = post(owner, '/posts', {'csrf': csrf(opage), 'body': '', 'spotify_link': URL_A})
    st, _, page = get(owner, '/')
    check('Spotify-only post succeeds and renders link + embed',
          'Listen on Spotify' in page and 'open.spotify.com/embed/track/' + TRACK_A in page, '')
    m = re.search(r'/post/(\d+)', page)
    pid = m.group(1) if m else ''
    st, _, dpage = get(owner, '/post/' + pid) if pid else (0, {}, '')
    check('post detail renders Spotify link + embed',
          pid and 'Listen on Spotify' in dpage and 'open.spotify.com/embed/track/' + TRACK_A in dpage, pid)
    st, _, opage = get(owner, '/')
    st, _, bad = post(owner, '/posts', {'csrf': csrf(opage), 'body': 'bad spotify post', 'spotify_link': 'https://www.youtube.com/watch?v=abc'})
    check('post with invalid Spotify link rejected (400)', st == 400, str(st))
    st, _, page = get(owner, '/')
    check('rejected post was not created', 'bad spotify post' not in page, '')

    # ---------- 11. /notes/refresh stub + settings + CSP ----------
    st, _, refresh = get(owner, '/notes/refresh')
    check('/notes/refresh says live sync is NOT set up, fakes nothing',
          st == 200 and 'not set up' in refresh and 'not connected' in refresh
          and 'Connected to Spotify' not in refresh and 'Now playing' not in refresh.replace('live now-playing', ''), str(st))
    st, _, settings = get(owner, '/settings')
    check('settings explains live Spotify sync needs a Spotify app + OAuth',
          'live now-playing' in settings and 'Spotify developer app' in settings
          and 'user-read-currently-playing' in settings, '')
    st, h, _ = get(owner, '/')
    check('CSP allows Spotify embed frames',
          'frame-src https://open.spotify.com' in h.get('Content-Security-Policy', ''), h.get('Content-Security-Policy', ''))
    check('CSP still has script-src self, no unsafe-inline',
          "script-src 'self';" in h.get('Content-Security-Policy', '') and 'unsafe-inline' not in h.get('Content-Security-Policy', ''), '')
    st, _, page = get(owner, '/')
    check('composer still offers Quad Radio + Spotify field',
          'Quad Radio' in page and 'spotify_link' in page and 'music_track' in page, '')
    st, _, _ = post(owner, '/posts', {'csrf': csrf(page), 'body': 'quad radio still works', 'music_track': 'quad-dawn'})
    st, _, page = get(owner, '/')
    check('Quad Radio post still renders', 'quad radio still works' in page and 'Quad Radio · Dawn Loop' in page, '')
    st, _, css = get(owner, '/app.css')
    check('listening-note CSS classes served', '.note-bubble' in css and '.spotify-embed' in css, '')
except Exception:
    import traceback; traceback.print_exc()
    results.append(('test run completed without an exception', False, 'see traceback above'))
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
    print('LISTENING TEST: %d passed, %d failed' % (len(results) - len(failed), len(failed)))
    if failed:
        print('FAILED:', [r[0] for r in failed])
    sys.exit(1 if failed else 0)

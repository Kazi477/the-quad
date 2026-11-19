"""Dev-only test for Update 8 — dual backend with failover.
NOT part of the production bundle.
Run: python3 failover_test.py   (from this folder)

Proves, against one SHARED database (one SQLite file in one DATA_DIR,
WAL mode), with two backend processes on different ports:
 1. Both backends start against the same live database.
 2. A post/account made via A is visible via B, and vice versa.
 3. A session + CSRF token created on A works on B (shared sessions,
    shared APP_SECRET).
 4. Concurrent writes from both backends all land exactly once —
    no lost or duplicated posts.
 5. FTS search on B finds a post written via A (shared FTS index).
 6. Failover: with the primary killed, requests through a front-door
    proxy (same primary-then-backup semantics as
    nginx_failover.conf.template) still succeed via the secondary;
    a post made during the outage is stored, and after the primary
    restarts it is visible via the primary too — no data lost.
 7. Backups from both processes exist, have distinct names, and pass
    SQLite integrity checks.
 8. The nginx failover config contains the upstream, both backends,
    the `backup` marker, proxy_next_upstream and max_fails.

Honest limit: nginx itself is NOT installed in this dev VM (no nginx
package in its apt repos), so the live front door in this test is a
small stdlib Python proxy implementing the same semantics, not a real
nginx. The nginx config is checked by content, not by a live nginx
run — it must be validated with `nginx -t` in the Docker build
before any deploy. See DUAL_BACKEND.md.
"""
import http.cookiejar, http.server, os, re, shutil, socketserver, sqlite3, subprocess, sys, tempfile, threading, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(tempfile.mkdtemp(prefix='network-failover-'))
PORT_A = '18281'
PORT_B = '18282'
PORT_PROXY = '18280'
BASE_A = 'http://127.0.0.1:' + PORT_A
BASE_B = 'http://127.0.0.1:' + PORT_B
BASE_PROXY = 'http://127.0.0.1:' + PORT_PROXY

results = []
def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), '-', name, ('| ' + detail if detail else ''))

def base_env(port):
    env = os.environ.copy()
    env.update({'PORT': port, 'PRODUCTION': '0', 'TRUST_PROXY': '0',
                'DATA_DIR': str(DATA), 'APP_SECRET': 'failover-test-secret-value',
                'ADMIN_EMAILS': 'owner@example.com', 'PBKDF2_ITERATIONS': '10000'})
    return env

def start_backend(port):
    p = subprocess.Popen([sys.executable, str(ROOT / 'the_network_production.py')],
                         env=base_env(port), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    return p

def wait_ready(base, timeout=40):
    for _ in range(int(timeout / 0.1)):
        try:
            with urllib.request.urlopen(base + '/healthz', timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False

def stop_proc(p):
    if p is None or p.poll() is not None:
        return
    p.terminate()
    try:
        p.wait(timeout=8)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()

def client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def get(base, op, path):
    req = urllib.request.Request(base + path)
    try:
        with op.open(req, timeout=5) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')

def post(base, op, path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(base + path, data=body, method='POST',
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with op.open(req, timeout=5) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')

def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ''

def feed_bodies(base, op):
    st, page = get(base, op, '/')
    return st, page

# --- Front-door proxy: same semantics as nginx_failover.conf.template ---
# Try the primary; on connection failure (refused/timeout) try the
# backup once. An HTTP error status from a live backend is a real
# answer and is NOT failed over (matches proxy_next_upstream, which
# only retries on error/timeout/502/503/504).
class FailoverProxy(http.server.BaseHTTPRequestHandler):
    backends = [BASE_A, BASE_B]
    def _forward(self, method):
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else None
        headers = {'Content-Type': self.headers.get('Content-Type', 'application/x-www-form-urlencoded')}
        cookie = self.headers.get('Cookie')
        if cookie:
            headers['Cookie'] = cookie
        last_err = None
        for backend in self.backends:
            try:
                req = urllib.request.Request(backend + self.path, data=body,
                                             method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=3) as r:
                    data = r.read()
                    self.send_response(r.status)
                    ct = r.headers.get('Content-Type')
                    if ct:
                        self.send_header('Content-Type', ct)
                    for sc in (r.headers.get_all('Set-Cookie') or []):
                        self.send_header('Set-Cookie', sc)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            except urllib.error.HTTPError as e:
                data = e.read()
                self.send_response(e.code)
                ct = e.headers.get('Content-Type')
                if ct:
                    self.send_header('Content-Type', ct)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as exc:  # connection refused / timeout: try next backend
                last_err = exc
                continue
        data = ('Bad gateway: no backend available (%s)' % last_err).encode()
        self.send_response(502)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        self._forward('GET')
    def do_POST(self):
        self._forward('POST')
    def log_message(self, *args):
        pass

class ProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

PW = 'correct-horse-battery-staple'
pA = pB = None
proxy = None
proxy_thread = None
try:
    # 1. Both backends start against the same live database.
    pA = start_backend(PORT_A)
    check('primary backend A starts and answers /healthz', wait_ready(BASE_A))
    pB = start_backend(PORT_B)
    check('secondary backend B starts against the same live shared DB', wait_ready(BASE_B))

    owner = client()
    st, _ = post(BASE_A, owner, '/register',
                 {'name': 'Owner', 'email': 'owner@example.com',
                  'password': PW, 'confirm_password': PW})
    check('owner account created via A', st == 200, 'status %s' % st)
    st, _ = post(BASE_A, owner, '/login', {'email': 'owner@example.com', 'password': PW})
    check('owner logs in via A (session created on A)', st == 200, 'status %s' % st)

    # 2. Write via A, read via B.
    st, page = get(BASE_A, owner, '/')
    token_a = csrf(page)
    st, _ = post(BASE_A, owner, '/posts', {'csrf': token_a, 'body': 'failoveralpha post written via A'})
    check('post written via A', st == 200, 'status %s' % st)
    friend = client()
    st, page = get(BASE_B, owner, '/')
    check('post written via A is visible on B (shared data A->B)',
          'failoveralpha post written via A' in page, 'status %s' % st)

    # 3. Session + CSRF from A work on B (shared sessions, shared secret).
    st, page = get(BASE_B, owner, '/')
    check('session created on A is valid on B', st == 200 and 'Owner' in page, 'status %s' % st)
    st, _ = post(BASE_B, owner, '/posts', {'csrf': token_a, 'body': 'post via B using A-issued CSRF token'})
    check('CSRF token issued by A is accepted by B (shared APP_SECRET)', st == 200, 'status %s' % st)

    # 4. Write via B, read via A.
    st, _ = post(BASE_B, friend, '/register',
                 {'name': 'Friend', 'email': 'friend@example.com',
                  'password': PW, 'confirm_password': PW})
    check('second account created via B', st == 200, 'status %s' % st)
    st, _ = post(BASE_B, friend, '/login', {'email': 'friend@example.com', 'password': PW})
    check('second user logs in via B', st == 200, 'status %s' % st)
    st, page = get(BASE_B, friend, '/')
    st, _ = post(BASE_B, friend, '/posts', {'csrf': csrf(page), 'body': 'failoverbeta post written via B'})
    check('post written via B', st == 200, 'status %s' % st)
    st, page = get(BASE_A, owner, '/')
    check('post written via B is visible on A (shared data B->A)',
          'failoverbeta post written via B' in page, 'status %s' % st)

    # 5. FTS search on B finds a post written via A.
    st, page = get(BASE_B, owner, '/search?q=failoveralpha')
    check('search on B finds post written via A (shared FTS index)',
          'failoveralpha post written via A' in page, 'status %s' % st)

    # 6. Concurrent writes from both backends land exactly once.
    concurrent_bodies = []
    errors = []
    def concurrent_post(base, who, i):
        try:
            op = client()
            post(base, op, '/login', {'email': 'owner@example.com', 'password': PW})
            _, pg = get(base, op, '/')
            body = 'concurrent post %s %d' % (who, i)
            s, _ = post(base, op, '/posts', {'csrf': csrf(pg), 'body': body})
            if s == 200:
                concurrent_bodies.append(body)
            else:
                errors.append((who, i, s))
        except Exception as exc:
            errors.append((who, i, repr(exc)))
    threads = []
    for i in range(2):
        threads.append(threading.Thread(target=concurrent_post, args=(BASE_A, 'viaA', i)))
        threads.append(threading.Thread(target=concurrent_post, args=(BASE_B, 'viaB', i)))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    with sqlite3.connect(str(DATA / 'network.db')) as c:
        rows = c.execute('SELECT body, COUNT(*) n FROM posts GROUP BY body').fetchall()
    counts = dict(rows)
    check('concurrent writes from A and B all succeeded',
          not errors and len(concurrent_bodies) == 4, 'errors=%s wrote=%d' % (errors, len(concurrent_bodies)))
    check('concurrent posts appear exactly once each (no duplicates, none lost)',
          all(counts.get(b) == 1 for b in concurrent_bodies),
          'counts=%s' % {b: counts.get(b) for b in concurrent_bodies})
    st, page_a = get(BASE_A, owner, '/')
    st, page_b = get(BASE_B, owner, '/')
    check('A and B agree on the concurrent posts',
          all(b in page_a and b in page_b for b in concurrent_bodies))

    # 7. Failover through the front-door proxy.
    proxy = ProxyServer(('127.0.0.1', int(PORT_PROXY)), FailoverProxy)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    st, _ = get(BASE_PROXY, client(), '/healthz')
    check('front-door proxy serves /healthz while A is up', st == 200, 'status %s' % st)

    stop_proc(pA)
    pA = None
    check('primary A is stopped (simulated crash)', True)
    st, _ = get(BASE_PROXY, client(), '/healthz')
    check('proxy still serves /healthz with A down (failover to B)', st == 200, 'status %s' % st)
    st, page = get(BASE_PROXY, friend, '/')
    check('proxy serves the feed with A down', st == 200 and 'failoverbeta post written via B' in page,
          'status %s' % st)
    st, _ = post(BASE_PROXY, friend, '/posts', {'csrf': csrf(page), 'body': 'outage post written while A was down'})
    check('a post made through the proxy during the outage is accepted', st == 200, 'status %s' % st)
    with sqlite3.connect(str(DATA / 'network.db')) as c:
        n = c.execute("SELECT COUNT(*) FROM posts WHERE body='outage post written while A was down'").fetchone()[0]
    check('outage post is in the shared DB exactly once', n == 1, 'count %d' % n)

    # 8. Primary returns: nothing lost, both backends agree.
    pA = start_backend(PORT_A)
    check('primary A restarts against the same shared DB', wait_ready(BASE_A))
    st, page = get(BASE_A, owner, '/')
    check('outage post is visible via A after it restarts (no data lost)',
          'outage post written while A was down' in page, 'status %s' % st)
    with sqlite3.connect(str(DATA / 'network.db')) as c:
        total = c.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
        dupes = c.execute('SELECT COUNT(*) FROM (SELECT body FROM posts GROUP BY body HAVING COUNT(*)>1)').fetchone()[0]
        integrity = c.execute('PRAGMA integrity_check').fetchone()[0]
    # Expected posts: A-post, B-via-A-token, B-post, 4 concurrent, outage post = 8
    check('post count is consistent after failover (8 posts, none lost)',
          total == 8, 'total %d' % total)
    check('no duplicated post bodies after failover', dupes == 0, 'duplicated bodies %d' % dupes)
    check('shared DB integrity check passes after failover', integrity == 'ok', str(integrity))
    st, _ = get(BASE_PROXY, client(), '/healthz')
    check('front-door proxy still serves after A returns', st == 200, 'status %s' % st)

    # 9. Backups from both processes: distinct names, all valid.
    backups = sorted((DATA / 'backups').glob('network-*.db'))
    names = {b.name for b in backups}
    ok_backups = len(backups) >= 2 and len(names) == len(backups)
    for b in backups:
        with sqlite3.connect(str(b)) as c:
            if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                ok_backups = False
    check('backups from both backends exist with distinct names and pass integrity checks',
          ok_backups, '%d backup files' % len(backups))

    # 10. Nginx failover config (content check — no live nginx in this VM).
    cfg = (ROOT / 'nginx_failover.conf.template').read_text()
    check('nginx config defines the quad_backend upstream with both backends',
          'upstream quad_backend' in cfg and '${BACKEND_PORT}' in cfg
          and '${BACKEND_PORT_SECONDARY}' in cfg)
    check('nginx config marks the secondary as backup and retries the next upstream',
          'backup' in cfg and 'proxy_next_upstream error timeout http_502 http_503 http_504' in cfg
          and 'max_fails=2' in cfg)
finally:
    if proxy is not None:
        proxy.shutdown()
        proxy.server_close()
    stop_proc(pA)
    stop_proc(pB)
    shutil.rmtree(DATA, ignore_errors=True)

failed = [r for r in results if not r[1]]
print('FAILOVER TEST: %d passed, %d failed' % (len(results) - len(failed), len(failed)))
for name, ok, detail in results:
    if not ok:
        print('FAILED:', name, detail)
sys.exit(1 if failed else 0)

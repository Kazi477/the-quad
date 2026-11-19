"""Test the PythonAnywhere WSGI adapter locally (dev only, temp DB).

Sets QUAD_CODE_DIR to this folder, DATA_DIR to a temp dir and a fixed
test APP_SECRET, then serves pythonanywhere_wsgi.application through a
real wsgiref server and drives it over HTTP: /healthz, register, login,
post to the feed, feed load, /app.js + /app.css, and a second request
reusing the same session (proves the loopback backend stays up and
sessions survive across WSGI requests).

Note on cookies: with PRODUCTION=1 (as on PythonAnywhere) the session
cookie is marked Secure, so it is only sent back over HTTPS in real
use. This local test server is plain HTTP, so Python's CookieJar
refuses to return a Secure cookie over it. The test client below
therefore manages cookies manually (parsing Set-Cookie and sending
Cookie back) — that tests the server side, which is what matters here.

Usage:  cd ~/workspace/network-merge/dev && python3 pythonanywhere_test.py
"""
import http.client
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(tempfile.mkdtemp(prefix='quad-pa-test-'))
os.environ['QUAD_CODE_DIR'] = str(ROOT)
os.environ['DATA_DIR'] = str(DATA)
os.environ['APP_SECRET'] = 'pa-test-secret-not-for-production-0123456789'

sys.path.insert(0, str(ROOT))
import pythonanywhere_wsgi as pa_wsgi  # noqa: E402

from wsgiref.simple_server import make_server, WSGIServer  # noqa: E402
from socketserver import ThreadingMixIn  # noqa: E402


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class Client:
    """Minimal HTTP client with manual cookie handling (see module note)."""

    def __init__(self, port):
        self.port = port
        self.cookies = {}

    def request(self, method, path, data=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {}
        if self.cookies:
            headers['Cookie'] = '; '.join(
                '%s=%s' % kv for kv in self.cookies.items())
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        for key, value in resp.getheaders():
            if key.lower() == 'set-cookie':
                pair = value.split(';', 1)[0]
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    if v:
                        self.cookies[k.strip()] = v.strip()
                    else:
                        self.cookies.pop(k.strip(), None)
        status = resp.status
        # Follow one redirect (login returns 303 to /).
        if status in (301, 302, 303, 307, 308):
            loc = resp.getheader('Location', '/')
            conn.close()
            return self.request('GET', loc)
        conn.close()
        return status, raw.decode('utf-8', 'replace')

    def get(self, path):
        return self.request('GET', path)

    def post(self, path, data):
        return self.request('POST', path, data)


def main():
    checks = []

    def check(name, ok, detail=''):
        checks.append((name, bool(ok)))
        print(('PASS' if ok else 'FAIL'), '-', name,
              ('(%s)' % detail if detail else ''))

    httpd = make_server('127.0.0.1', 0, pa_wsgi.application,
                        server_class=ThreadingWSGIServer)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def csrf(html):
        m = re.search(r'name="csrf" value="([^"]+)"', html)
        return m.group(1) if m else ''

    try:
        anon = Client(port)
        for _ in range(50):
            try:
                st, body = anon.get('/healthz')
                if st == 200 and body.strip() == 'ok':
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError('WSGI server did not become ready')
        check('healthz via WSGI', True)

        st, home = anon.get('/')
        check('landing page loads via WSGI',
              st == 200 and 'The Campus' in home, 'status %s' % st)

        st, body = anon.post('/register', {
            'name': 'Mitchell', 'email': 'mitchellwhite018@gmail.com',
            'password': 'correct-horse-battery-staple',
            'confirm_password': 'correct-horse-battery-staple'})
        check('register via WSGI', st == 200, 'status %s' % st)

        owner = Client(port)
        st, body = owner.post('/login', {
            'email': 'mitchellwhite018@gmail.com',
            'password': 'correct-horse-battery-staple'})
        check('login via WSGI', st == 200, 'status %s' % st)
        check('session cookie set (Secure, as in production)',
              'network_session' in owner.cookies)

        st, page = owner.get('/')
        token = csrf(page)
        check('feed loads + CSRF token present',
              st == 200 and bool(token), 'status %s' % st)
        st, body = owner.post('/posts', {
            'csrf': token, 'body': 'wsgi adapter test post'})
        check('post via WSGI', st == 200, 'status %s' % st)
        st, page = owner.get('/')
        check('post visible on feed (same session, second request)',
              'wsgi adapter test post' in page)

        st, js = owner.get('/app.js')
        check('/app.js via WSGI', st == 200 and 'csrf' in js,
              'status %s' % st)
        st, css = owner.get('/app.css')
        check('/app.css via WSGI', st == 200 and '--brand' in css,
              'status %s' % st)

        st, admin = owner.get('/admin')
        check('admin page via WSGI',
              st == 200 and 'Admin' in admin, 'status %s' % st)

        db = DATA / 'network.db'
        check('database created in DATA_DIR', db.is_file())
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(DATA, ignore_errors=True)

    failed = [n for n, ok in checks if not ok]
    print()
    print('PYTHONANYWHERE WSGI TEST: %d passed, %d failed'
          % (len(checks) - len(failed), len(failed)))
    if failed:
        print('Failed:', ', '.join(failed))
        sys.exit(1)


if __name__ == '__main__':
    main()

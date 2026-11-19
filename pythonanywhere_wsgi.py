"""The Quad on PythonAnywhere (free Beginner account) — WSGI adapter.

Why this file exists
--------------------
`the_network_production.py` is a stdlib `http.server` app
(`BaseHTTPRequestHandler` + `ThreadingHTTPServer` on a port). It has no
native WSGI entry point, and PythonAnywhere web apps are WSGI-only: they
load a file that exposes ``application(environ, start_response)`` and do
not run a long-lived server on a port. Rewriting the whole app as native
WSGI would touch all 2,406 lines and risk breaking every tested route.

So this adapter does the smallest honest thing: on first use it starts
the existing backend server on a loopback-only port (127.0.0.1, ephemeral,
never exposed to the internet) in a daemon thread, and each WSGI request
is proxied to it over that loopback socket with `http.client`. The
backend itself is unchanged — same routes, auth, sessions, CSRF, rate
limits, SQLite database. The proxy only translates WSGI <-> HTTP.

What is proxied faithfully
--------------------------
* Method, path + query string, request body (bytes, untouched — so
  multipart uploads and form posts work exactly as before).
* Cookies, Content-Type, Origin (same-origin checks still run).
* `X-Forwarded-Proto: https` so session cookies are marked Secure and
  `X-Forwarded-For: <real client IP>` so rate limiting is per visitor,
  not per "127.0.0.1" (the backend uses the *last* X-Forwarded-For entry,
  which is the one this adapter sets).
* Response status, headers (except hop-by-hop ones) and body, including
  `Set-Cookie`.

Environment (set these BEFORE this file imports the backend)
-----------------------------------------------------------
* ``QUAD_CODE_DIR`` — folder holding `the_network_production.py`.
  Default: ``/home/mitchellwhite/quad`` (the PythonAnywhere path).
* ``DATA_DIR`` — persistent folder for the SQLite DB, uploads, backups.
  Default: ``/home/mitchellwhite/quad-data`` (inside the home directory,
  which survives reloads/restarts on PythonAnywhere free).
* ``APP_SECRET`` — a long random secret, SAME on every reload. It signs
  CSRF tokens and password-reset tokens. If it changes, every signed-in
  user is effectively logged out and reset links break. **You must set
  this.** This file refuses to start with the placeholder still in
  place. Generate one, e.g. ``python3 -c "import secrets;
  print(secrets.token_urlsafe(48))"``, and paste it below (or set it as
  an environment variable in the PythonAnywhere web app settings).
* ``PRODUCTION`` is forced to ``1`` and ``TRUST_PROXY`` to ``1`` here,
  because PythonAnywhere terminates TLS in front of the app.

Limitations (honest)
--------------------
* One web app only on PythonAnywhere free, so the dual-backend /
  failover setup from Update 8 does NOT apply here: there is no second
  backend process and no nginx front door. If the PythonAnywhere worker
  restarts, the loopback backend restarts with it; the database (on
  disk, WAL mode) survives.
* The 6-hourly backup worker from `main()` is started here too (one
  daemon thread), and a startup backup is taken, as in `main()`. If the
  worker is ever recycled, backups only happen on the next (re)start —
  the database itself is never at risk from a missed backup.
* Video uploads need `ffprobe`, which PythonAnywhere free does not
  provide. The backend already rejects videos whose duration cannot be
  verified server-side, so video uploads will be rejected on
  PythonAnywhere until/unless ffprobe is available. Image uploads
  (<= 8 MB) work.
* SMTP is not configured by this file. Password-reset *emailed* codes
  will not send until SMTP is configured on the server; the recovery
  codes from Update 7 (Settings -> Password recovery) are the working
  path until then.
* PythonAnywhere free allows 100 CPU-seconds/day and one web app. This
  is fine for a small friends/community site; it is not a
  high-traffic design.
"""

import http.client
import os
import sys
import threading
import time

# ---------------------------------------------------------------- settings
CODE_DIR = os.environ.get(
    'QUAD_CODE_DIR', '/home/mitchellwhite/quad')
DATA_DIR = os.environ.get(
    'DATA_DIR', '/home/mitchellwhite/quad-data')
# Replace this placeholder with a real long random secret before use.
# (Or set the APP_SECRET environment variable on PythonAnywhere instead.)
APP_SECRET = os.environ.get('APP_SECRET', '').strip() or \
    'PASTE-A-LONG-RANDOM-SECRET-HERE'

if APP_SECRET == 'PASTE-A-LONG-RANDOM-SECRET-HERE':
    raise RuntimeError(
        'Set APP_SECRET in pythonanywhere_wsgi.py (or as an environment '
        'variable) before loading this app. Generate one with: '
        "python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"")

if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

# These must be set BEFORE importing the backend: it reads them at
# import time and creates DATA_DIR / the database paths from them.
os.environ['DATA_DIR'] = DATA_DIR
os.environ['APP_SECRET'] = APP_SECRET
os.environ['PRODUCTION'] = '1'
os.environ['TRUST_PROXY'] = '1'

import the_network_production as quad  # noqa: E402  (env setup above)

# ------------------------------------------------------- backend (loopback)
_backend_lock = threading.Lock()
_backend_port = None  # set once the loopback server is up


def _start_backend():
    """Start the existing backend on a loopback-only port, once.

    Mirrors what main() does at startup, minus binding a public port,
    signal handlers and opening a browser. Returns the port number.
    """
    global _backend_port
    with _backend_lock:
        if _backend_port is not None:
            return _backend_port
        quad.init_db()
        with quad.connect() as c:
            check = c.execute('PRAGMA quick_check').fetchone()[0]
            if check != 'ok':
                raise RuntimeError(
                    'SQLite integrity check failed: %s' % check)
        quad.backup_database()
        threading.Thread(target=quad.backup_worker,
                         name='database-backups', daemon=True).start()
        srv = quad.NetworkHTTPServer(('127.0.0.1', 0), quad.Handler)
        _backend_port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.5},
                         name='quad-loopback-backend', daemon=True).start()
        # Wait until it answers /healthz (max ~5 s) before serving traffic.
        for _ in range(50):
            try:
                conn = http.client.HTTPConnection(
                    '127.0.0.1', _backend_port, timeout=1)
                conn.request('GET', '/healthz')
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status == 200:
                    return _backend_port
            except Exception:
                time.sleep(0.1)
        raise RuntimeError('Quad backend did not become ready on loopback')


# ------------------------------------------------------------------- WSGI
_HOP_BY_HOP = {
    'connection', 'keep-alive', 'proxy-connection', 'te', 'trailer',
    'transfer-encoding', 'upgrade', 'server', 'date',
}


def application(environ, start_response):
    """WSGI entry point. Proxies one request to the loopback backend."""
    try:
        port = _start_backend()
    except Exception as exc:  # backend could not start: fail loudly
        body = ('The Quad backend could not start: %s' % exc).encode('utf-8')
        start_response('502 Bad Gateway', [
            ('Content-Type', 'text/plain; charset=utf-8'),
            ('Content-Length', str(len(body))),
        ])
        return [body]

    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/') or '/'
    query = environ.get('QUERY_STRING', '')
    if query:
        path = path + '?' + query

    # Read the request body exactly as sent (multipart uploads included).
    try:
        length = int(environ.get('CONTENT_LENGTH', '0') or '0')
    except ValueError:
        length = 0
    body = environ['wsgi.input'].read(length) if length > 0 else b''

    headers = {}
    for key, value in environ.items():
        if key.startswith('HTTP_'):
            name = key[5:].replace('_', '-').title()
            if name.lower() not in ('host', 'connection'):
                headers[name] = value
    if environ.get('CONTENT_TYPE'):
        headers['Content-Type'] = environ['CONTENT_TYPE']
    if body:
        headers['Content-Length'] = str(len(body))
    # PythonAnywhere terminates TLS in front of the app.
    headers['X-Forwarded-Proto'] = 'https'
    remote = environ.get('REMOTE_ADDR', '') or ''
    if remote:
        # Backend uses the LAST X-Forwarded-For entry, so this one wins.
        headers['X-Forwarded-For'] = remote

    try:
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        status = '%s %s' % (resp.status, resp.reason)
        out_headers = [(k, v) for k, v in resp.getheaders()
                       if k.lower() not in _HOP_BY_HOP]
        conn.close()
    except Exception as exc:
        body = ('The Quad backend is unavailable: %s' % exc).encode('utf-8')
        start_response('502 Bad Gateway', [
            ('Content-Type', 'text/plain; charset=utf-8'),
            ('Content-Length', str(len(body))),
        ])
        return [body]

    start_response(status, out_headers)
    return [resp_body]

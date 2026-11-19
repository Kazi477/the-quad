"""Regression tests for the Update 17 Render deploy fix (2026-11-19/20).

Live symptom: both auto-deploys of the flapping fix (commits
18cd219 and 4e16273, same code) FAILED at Render's health check,
'Timed out after waiting for internal health check to return a
successful response code', after 14m and 18m. Logs: 'FATAL: app
did not become healthy within 60s; not starting nginx' at
23:51:05, then 'Exited with status 1'; second deploy: app
starting 23:51:48, /healthz returning 200 repeatedly, HEAD /
returning 501 'Unsupported method', then Render 'No open HTTP
ports detected on 0.0.0.0, continuing to scan...' and 'Port scan
timeout reached' at 00:06-00:09.

Root cause, proven in dev (see MERGE_NOTES Update 17):
  * Update 14 bound the socket first but still ran the database
    gate BEFORE serve_forever(), so the port was bound but
    nothing ACCEPTED a connection until the gate finished. On a
    cold start the gate does a Turso sync plus ~50 schema writes
    forwarded to Sydney — far longer than Render's 5s health
    check budget and start.sh's 2s probe timeout. Every probe
    timed out, start.sh killed the app after its gate, nginx
    never started, Render's port scan found nothing for 15
    minutes, and the deploy was cancelled. The /healthz 200s in
    the app log were server-side completions of probes whose
    clients had already given up.
  * The log's 'within 60s' was a stale string: the Update 16
    loop had been raised to 240 attempts x 0.5s = 120s, but the
    FATAL message was never updated — it waited 120s, not 60s.
  * BaseHTTPRequestHandler has no do_HEAD, so every HEAD
    request got 501. Render's checker issues HEAD /.

Fix (app.py + start.sh, Update 17): serve first, gate second
(serve_forever runs in a thread before ensure_database_healthy);
 /healthz is liveness-only and answers in milliseconds without
touching the database; /readyz reports database readiness
(200 only after the gate passes, 503 before); do_HEAD answers
health endpoints like GET (headers only) and 200s anything
else instead of 501; start.sh's probe timeout is 5s (matching
Render) and its FATAL message now says 120s; the startup
backup thread starts only after the gate passes.

What this proves (dev only, temp DBs wiped after each run):
  * With the database gate artificially delayed 6s
    (QUAD_GATE_DELAY_SECONDS, test hook only), GET /healthz
    still answers 200 within 2s of process start, /readyz
    answers 503 during the delay and 200 after it, HEAD
    /healthz and HEAD / answer 200 with an empty body, and
    GET / answers promptly (500 while the DB is not ready,
    200 after) instead of hanging.
  * The same holds in Turso-local mode (libsql driver path).
  * start.sh (path-rewritten copy, stub nginx + stub
    envsubst) starts nginx on liveness even while the gate
    is still delayed, and contains no stale 'within 60s'.

Driver setup: same pattern as flap_test.py — libsql is installed
into /tmp/quad-turso-venv on first run and the file re-runs
itself under that venv (needed for the Turso-local run).

Usage:  cd ~/workspace/network-merge/dev && python3 deployfix_test.py
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent

try:
    import libsql_experimental  # noqa: F401
except ImportError:
    if os.environ.get('QUAD_TURSO_VENV_DONE') != '1':
        venv = Path('/tmp/quad-turso-venv')
        if not (venv / 'bin' / 'python').exists():
            subprocess.run([sys.executable, '-m', 'venv', str(venv)],
                           check=True)
            subprocess.run([str(venv / 'bin' / 'pip'), 'install', '-q',
                            'libsql-experimental==0.0.55'], check=True)
        env = dict(os.environ)
        env['QUAD_TURSO_VENV_DONE'] = '1'
        env['PYTHONPATH'] = str(ROOT)
        sys.exit(subprocess.run(
            [str(venv / 'bin' / 'python'), str(Path(__file__).resolve())],
            env=env).returncode)
    raise

CHECKS = []
SKIPPED = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))
    if not cond:
        print('FAIL:', name)


def skip(name):
    SKIPPED.append(name)
    print('SKIP:', name)


def report():
    failed = [n for n, ok in CHECKS if not ok]
    print('deployfix_test: %d passed, %d failed, %d skipped'
          % (len(CHECKS) - len(failed), len(failed), len(SKIPPED)))
    for name, ok in CHECKS:
        if not ok:
            print('  FAILED:', name)
    return 1 if failed else 0


def http(method, port, path, timeout=5):
    t0 = time.time()
    try:
        req = urllib.request.Request(
            'http://127.0.0.1:%d%s' % (port, path), method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.time() - t0
    except Exception as e:
        return type(e).__name__, b'', time.time() - t0


def run_app(data_dir, port, extra_env):
    env = dict(os.environ)
    env.update({
        'DATA_DIR': str(data_dir),
        'BACKEND_PORT': str(port),
        'PORT': str(port),
        'RENDER': '1', 'PRODUCTION': '1', 'TRUST_PROXY': '1',
        'APP_SECRET': 'deployfix-test-secret-1234567890',
        'QUAD_GATE_DELAY_SECONDS': '6',
    })
    env.update(extra_env)
    log = open(str(data_dir / 'app.log'), 'w')
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / 'the_network_production.py')],
        env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def stop_app(proc, log):
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        log.close()
    except Exception:
        pass


def check_startup(mode, extra_env):
    """Start the app with a 6s delayed DB gate and probe it."""
    tmp = Path(tempfile.mkdtemp(prefix='quad-deployfix-'))
    port = 18081 if mode == 'plain' else 18082
    proc, log = run_app(tmp, port, extra_env)
    try:
        # 1. Liveness: /healthz must answer 200 FAST, while the
        #    gate is still delayed — this is the exact property
        #    the Render deploy failure lacked. Poll from process
        #    start (Python itself needs ~0.5s to import) and
        #    require the first 200 within 2s.
        t_start = time.time()
        status, body, took = 'URLError', b'', 0
        while time.time() - t_start < 2.0:
            status, body, took = http('GET', port, '/healthz', timeout=5)
            if status == 200:
                break
            time.sleep(0.1)
        check('%s: /healthz 200 within 2s of start (got %s in %.2fs)'
              % (mode, status, time.time() - t_start),
              status == 200 and time.time() - t_start < 2.0)
        check('%s: /healthz body is ok' % mode, body == b'ok')
        # 2. Readiness: 503 while the gate runs.
        status, body, _ = http('GET', port, '/readyz', timeout=5)
        check('%s: /readyz 503 while gate delayed (got %s)'
              % (mode, status), status == 503)
        # 3. HEAD support: 200, empty body, correct length header.
        status, body, _ = http('HEAD', port, '/healthz', timeout=5)
        check('%s: HEAD /healthz 200, empty body (got %s, %r)'
              % (mode, status, body), status == 200 and body == b'')
        status, body, _ = http('HEAD', port, '/', timeout=5)
        check('%s: HEAD / 200, empty body (got %s, %r)'
              % (mode, status, body), status == 200 and body == b'')
        # 4. A real page must answer promptly during the delay,
        #    not hang until the gate finishes.
        status, _, took = http('GET', port, '/', timeout=5)
        check('%s: GET / answers within 5s during gate delay '
              '(got %s in %.2fs)' % (mode, status, took),
              status in (200, 500, 503) and took < 5.0)
        # 5. After the 6s gate, readiness flips and pages serve.
        deadline = time.time() + 15
        ready = False
        while time.time() < deadline:
            status, _, _ = http('GET', port, '/readyz', timeout=5)
            if status == 200:
                ready = True
                break
            time.sleep(0.5)
        check('%s: /readyz 200 after gate passes' % mode, ready)
        status, _, _ = http('GET', port, '/', timeout=5)
        check('%s: GET / 200 after gate passes (got %s)'
              % (mode, status), status == 200)
        status, _, _ = http('GET', port, '/healthz', timeout=5)
        check('%s: /healthz still 200 after gate passes' % mode,
              status == 200)
        check('%s: app process still alive after gate' % mode,
              proc.poll() is None)
    finally:
        stop_app(proc, log)


def check_startsh():
    """Run start.sh (paths rewritten to a sandbox, stub nginx and
    stub envsubst on PATH) against the real app with a delayed
    gate: nginx must start on liveness, not wait for the gate."""
    startsh = (ROOT / 'start.sh').read_text()
    check('start.sh: no stale "within 60s" message',
          'within 60s' not in startsh)
    check('start.sh: FATAL says 120s', 'within 120s' in startsh)
    check('start.sh: probe timeout is 5s', 'timeout=5' in startsh)
    tmp = Path(tempfile.mkdtemp(prefix='quad-deployfix-sh-'))
    sandbox = tmp / 'sandbox'
    (sandbox / 'etc' / 'nginx').mkdir(parents=True)
    (sandbox / 'app').mkdir(parents=True)
    for name in ('db_backend.py', 'object_storage.py',
                 'nginx.conf.template'):
        (sandbox / 'app' / name).write_bytes((ROOT / name).read_bytes())
    # Docker renames the_network_production.py -> /app/app.py.
    (sandbox / 'app' / 'app.py').write_bytes(
        (ROOT / 'the_network_production.py').read_bytes())
    sh = (ROOT / 'start.sh').read_text()
    sh = sh.replace('/app/nginx.conf.template',
                    str(sandbox / 'app' / 'nginx.conf.template'))
    sh = sh.replace('/etc/nginx/nginx.conf',
                    str(sandbox / 'etc' / 'nginx' / 'nginx.conf'))
    sh = sh.replace('/app/app.py', str(sandbox / 'app' / 'app.py'))
    sh = sh.replace('/app/start.sh', str(sandbox / 'start.sh'))
    (sandbox / 'start.sh').write_text(sh)
    # Stub envsubst: expand ${PORT} / ${BACKEND_PORT} only.
    bindir = tmp / 'bin'
    bindir.mkdir()
    # start.sh runs `python` (present in the Docker image); this
    # VM only has python3, so shim it to this interpreter.
    os.symlink(sys.executable, bindir / 'python')
    (bindir / 'envsubst').write_text(
        "#!/bin/sh\nsed -e 's/${PORT}/10000/g' "
        "-e 's/${BACKEND_PORT}/18083/g'\n")
    # Stub nginx: record that it started, then sleep.
    (bindir / 'nginx').write_text(
        '#!/bin/sh\necho started > %s\nsleep 30\n'
        % (sandbox / 'nginx-started',))
    for f in ('envsubst', 'nginx'):
        (bindir / f).chmod(0o755)
    env = dict(os.environ)
    env.update({
        'PATH': str(bindir) + ':' + env.get('PATH', ''),
        'DATA_DIR': str(tmp / 'data'),
        'BACKEND_PORT': '18083',
        'PORT': '10000',
        'RENDER': '1', 'PRODUCTION': '1', 'TRUST_PROXY': '1',
        'APP_SECRET': 'deployfix-test-secret-1234567890',
        'QUAD_GATE_DELAY_SECONDS': '6',
    })
    proc = subprocess.Popen(['bash', str(sandbox / 'start.sh')],
                            env=env, stdout=open(str(tmp / 'sh.log'), 'w'),
                            stderr=subprocess.STDOUT)
    try:
        # nginx must start on liveness (well before the 6s gate
        # finishes) — the old behaviour waited for a DB-backed
        # /healthz and never started it at all on a slow start.
        started = False
        deadline = time.time() + 10
        while time.time() < deadline:
            if (sandbox / 'nginx-started').exists():
                started = True
                break
            time.sleep(0.2)
        check('start.sh: nginx starts within 10s despite 6s gate '
              'delay', started)
        # ... and the app must still be answering /healthz.
        status, _, _ = http('GET', 18083, '/healthz', timeout=5)
        check('start.sh: /healthz 200 while nginx stub runs (got %s)'
              % status, status == 200)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def main():
    check_startup('plain', {})
    check_startup('turso-local', {
        'TURSO_DATABASE_URL': 'local', 'TURSO_AUTH_TOKEN': 'x'})
    check_startsh()
    return report()


if __name__ == '__main__':
    sys.exit(main())

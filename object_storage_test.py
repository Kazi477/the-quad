"""Test object storage for photos (dev only, temp DBs wiped after each run).

What this proves:
  * object_storage.py speaks real S3 (SigV4) to a LOCAL fake S3
    server: PUT/GET/DELETE round-trip, Authorization header present
    and well-formed, payload hash matches the body, keys with a
    slash/traversal are refused, missing objects return None, a dead
    endpoint raises ObjectStorageError (never a silent fallback).
  * End-to-end in object mode (fake S3, real app server): a photo
    post lands in the bucket (not on local disk), the feed renders
    /media/<key>, /media/ serves the exact bytes, a private photo is
    404 for a non-friend WITHOUT touching S3 (permission is checked
    first), avatar replace deletes the old object, deleting a post
    deletes the object, a 1-second video uploads through ffprobe
    verification into the bucket, and admin remove/restore keeps
    the object in the bucket so the restore can bring it back.
  * With storage down (endpoint on a closed port) an upload fails
    with a plain error and creates NO post and NO local file — it
    never silently falls back to ephemeral local disk.
  * Without the env vars the app is unchanged: uploads go to the
    local folder, storage='local'.
  * A legacy local photo still serves when object mode is switched
    on (old rows default to storage='local', no migration needed).
  * render-free.yaml carries the OBJECT_STORAGE_* vars as
    sync:false secrets with no values, and Dockerfile copies
    object_storage.py. requirements.txt gains no new dependency
    (stdlib only — no boto3).

What this does NOT prove (honest): a real R2/Tigris bucket. No
account or bucket was created here, so the live handshake is
untested — the first real deploy is the first live-bucket test.
The fake S3 checks the SigV4 header shape and payload hash, not
a full signature recomputation. See RENDER_FREE.md.

Usage:  cd ~/workspace/network-merge/dev && python3 object_storage_test.py
"""

import hashlib
import http.cookiejar
import http.server
import importlib.util
import json
import os
import re
import secrets
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import object_storage  # noqa: E402

CHECKS = []


def check(name, cond, detail=''):
    CHECKS.append((name, bool(cond)))
    print(('PASS' if cond else 'FAIL'), '-', name,
          ('| ' + str(detail) if detail else ''))


def report():
    failed = [n for n, ok in CHECKS if not ok]
    print('object_storage_test: %d passed, %d failed'
          % (len(CHECKS) - len(failed), len(failed)))
    for name in failed:
        print('  FAILED:', name)
    return 1 if failed else 0


# ---------------------------------------------------------- fake S3
class FakeS3Handler(http.server.BaseHTTPRequestHandler):
    store = {}
    counts = {'PUT': 0, 'GET': 0, 'DELETE': 0}
    last_auth = ''

    def log_message(self, *args):
        pass

    def _auth_ok(self, body=b''):
        auth = self.headers.get('Authorization', '')
        FakeS3Handler.last_auth = auth
        if not auth.startswith('AWS4-HMAC-SHA256 '):
            return False
        if 'SignedHeaders=' not in auth or 'Signature=' not in auth:
            return False
        sig = auth.split('Signature=', 1)[1].split(',')[0].strip()
        if not re.fullmatch(r'[0-9a-f]{64}', sig):
            return False
        expect = hashlib.sha256(body).hexdigest()
        if self.headers.get('x-amz-content-sha256') != expect:
            return False
        if not self.headers.get('x-amz-date'):
            return False
        return True

    def _key(self):
        parts = urllib.parse.unquote(self.path).strip('/').split('/', 1)
        if len(parts) != 2:
            return None
        return parts[1]

    def do_PUT(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(length) if length else b''
        if not self._auth_ok(body):
            self.send_response(403)
            self.end_headers()
            return
        key = self._key()
        if key is None:
            self.send_response(400)
            self.end_headers()
            return
        FakeS3Handler.store[key] = (
            body, self.headers.get('Content-Type', 'application/octet-stream'))
        FakeS3Handler.counts['PUT'] += 1
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        if not self._auth_ok():
            self.send_response(403)
            self.end_headers()
            return
        key = self._key()
        FakeS3Handler.counts['GET'] += 1
        if key is None or key not in FakeS3Handler.store:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body, ctype = FakeS3Handler.store[key]
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        if not self._auth_ok():
            self.send_response(403)
            self.end_headers()
            return
        key = self._key()
        FakeS3Handler.counts['DELETE'] += 1
        FakeS3Handler.store.pop(key, None)
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.end_headers()


class FakeS3(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_fake_s3():
    FakeS3Handler.store = {}
    FakeS3Handler.counts = {'PUT': 0, 'GET': 0, 'DELETE': 0}
    srv = FakeS3(('127.0.0.1', 0), FakeS3Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, 'http://127.0.0.1:%d' % srv.server_address[1]


def storage_env(endpoint):
    return {
        'OBJECT_STORAGE_ENDPOINT': endpoint,
        'OBJECT_STORAGE_BUCKET': 'the-quad-media',
        'OBJECT_STORAGE_ACCESS_KEY': 'test-access-key',
        'OBJECT_STORAGE_SECRET_KEY': 'test-secret-key-not-a-real-key',
        'OBJECT_STORAGE_REGION': 'auto',
    }


# ---------------------------------------------------------- unit tests
def unit_tests():
    srv, endpoint = start_fake_s3()
    try:
        os.environ.update(storage_env(endpoint))
        check('unit: enabled() with all four vars', object_storage.enabled())
        cfg = object_storage.config()
        check('unit: config region defaults to auto',
              cfg and cfg['region'] == 'auto')
        check('unit: config bucket', cfg and cfg['bucket'] == 'the-quad-media')
        data = b'\x89PNG\r\n\x1a\n' + b'\x00' * 50
        check('unit: put_object', object_storage.put_object(
            'abc123.png', data, 'image/png') is True)
        check('unit: fake S3 saw a signed PUT',
              FakeS3Handler.counts['PUT'] == 1
              and FakeS3Handler.last_auth.startswith('AWS4-HMAC-SHA256 '))
        got = object_storage.get_object('abc123.png')
        check('unit: get_object round-trips bytes', got == data)
        check('unit: get missing returns None',
              object_storage.get_object('nope.png') is None)
        check('unit: delete missing is True',
              object_storage.delete_object('nope.png') is True)
        check('unit: delete_object removes it',
              object_storage.delete_object('abc123.png') is True
              and object_storage.get_object('abc123.png') is None)
        for bad in ('a/b.png', '../evil.png', '', 'a\\b.png'):
            try:
                object_storage.put_object(bad, b'x', 'image/png')
                check('unit: bad key %r refused' % bad, False)
            except object_storage.ObjectStorageError:
                check('unit: bad key %r refused' % bad, True)
        # Dead endpoint: transport failure raises, never silently OK.
        os.environ['OBJECT_STORAGE_ENDPOINT'] = 'http://127.0.0.1:1'
        try:
            object_storage.put_object('x.png', b'x', 'image/png')
            check('unit: dead endpoint raises', False)
        except object_storage.ObjectStorageError:
            check('unit: dead endpoint raises', True)
        # Unconfigured: disabled, and operations refuse.
        for var in ('OBJECT_STORAGE_ENDPOINT', 'OBJECT_STORAGE_BUCKET',
                    'OBJECT_STORAGE_ACCESS_KEY', 'OBJECT_STORAGE_SECRET_KEY'):
            os.environ.pop(var, None)
        check('unit: disabled without env vars',
              not object_storage.enabled())
        try:
            object_storage.put_object('x.png', b'x', 'image/png')
            check('unit: put unconfigured raises', False)
        except object_storage.ObjectStorageError:
            check('unit: put unconfigured raises', True)
    finally:
        for var in ('OBJECT_STORAGE_ENDPOINT', 'OBJECT_STORAGE_BUCKET',
                    'OBJECT_STORAGE_ACCESS_KEY', 'OBJECT_STORAGE_SECRET_KEY',
                    'OBJECT_STORAGE_REGION'):
            os.environ.pop(var, None)
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------- HTTP helpers
def make_client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def get_bytes(op, base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with op.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get(op, base, path, headers=None):
    st, h, b = get_bytes(op, base, path, headers)
    return st, h, b.decode('utf-8', 'replace')


def post(op, base, path, data, headers=None):
    body = urllib.parse.urlencode(data).encode()
    h = {'Content-Type': 'application/x-www-form-urlencoded'}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=body, method='POST', headers=h)
    try:
        with op.open(req, timeout=10) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def post_multipart(op, base, path, fields, files, headers=None):
    boundary = '----obj-test-' + secrets.token_hex(8)
    body = b''
    for k, v in fields.items():
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                 % (boundary, k, v)).encode()
    for k, (fname, ctype, data) in files.items():
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
                 % (boundary, k, fname, ctype)).encode() + data + b'\r\n'
    body += ('--%s--\r\n' % boundary).encode()
    h = {'Content-Type': 'multipart/form-data; boundary=' + boundary}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=body, method='POST', headers=h)
    try:
        with op.open(req, timeout=15) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def wait_ready(base, timeout=10):
    for _ in range(int(timeout * 10)):
        try:
            with urllib.request.urlopen(base + '/healthz', timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


# Import the app module for password_hash (does not start the server).
_spec = importlib.util.spec_from_file_location(
    'quad_app', str(ROOT / 'the_network_production.py'))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def start_app(data, port, extra_env=None):
    env = os.environ.copy()
    env.update({'PORT': str(port), 'PRODUCTION': '0', 'TRUST_PROXY': '0',
                'DATA_DIR': str(data), 'APP_SECRET': 'object-storage-test-secret',
                'ADMIN_EMAILS': 'mitchellwhite018@gmail.com',
                'PBKDF2_ITERATIONS': '10000'})
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / 'the_network_production.py')],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = 'http://127.0.0.1:%d' % port
    return proc, base, wait_ready(base)


def stop_app(proc):
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ''


def seed_user(data, name, email, admin=0, private=0):
    db = sqlite3.connect(str(data / 'network.db'))
    with db:
        db.execute('PRAGMA foreign_keys=ON')
        cur = db.execute(
            'INSERT INTO users(name,email,password_hash,is_admin,joined,last_seen)'
            ' VALUES(?,?,?,?,?,?)',
            (name, email, _mod.password_hash('seeded-password-123', iterations=10000),
             admin, time.time(), time.time()))
        uid = cur.lastrowid
        db.execute('INSERT OR IGNORE INTO user_settings(user_id,private_account)'
                   ' VALUES(?,?)', (uid, private))
    db.close()
    return uid


def auth_headers(data, uid):
    token = secrets.token_urlsafe(32)
    csrf_tok = secrets.token_urlsafe(32)
    now = time.time()
    db = sqlite3.connect(str(data / 'network.db'))
    with db:
        db.execute('INSERT INTO sessions(token,user_id,csrf,expires,last_seen)'
                   ' VALUES(?,?,?,?,?)', (token, uid, csrf_tok, now + 7 * 86400, now))
    db.close()
    return {'Cookie': 'network_session=' + token}, csrf_tok


def db_rows(data, sql, args=()):
    db = sqlite3.connect(str(data / 'network.db'))
    db.row_factory = sqlite3.Row
    rows = db.execute(sql, args).fetchall()
    db.close()
    return rows


PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 120


def make_mp4(path):
    """A real 1-second mp4 via ffmpeg (present in this dev VM)."""
    r = subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
                        '-i', 'color=c=red:s=64x64:d=1', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(path)],
                       capture_output=True, timeout=30)
    return r.returncode == 0 and path.exists() and path.stat().st_size > 0


# ---------------------------------------------------------- end-to-end
def e2e_object_mode():
    srv, endpoint = start_fake_s3()
    data = Path(tempfile.mkdtemp(prefix='quad-obj-'))
    proc, base, ready = start_app(data, 18301, storage_env(endpoint))
    try:
        check('e2e: server starts in object mode', ready)
        if not ready:
            return
        op = make_client()
        admin = seed_user(data, 'Mitchell', 'mitchellwhite018@gmail.com', admin=1)
        alice = seed_user(data, 'Alice', 'alice@example.com', private=1)
        bob = seed_user(data, 'Bob', 'bob@example.com')
        carol = seed_user(data, 'Carol', 'carol@example.com')
        # Alice and Bob are friends; Carol is a stranger to Alice.
        db = sqlite3.connect(str(data / 'network.db'))
        with db:
            db.execute("INSERT INTO friendships(id,requester_id,addressee_id,status,created)"
                       " VALUES(?,?,?,?,?)",
                       (secrets.token_hex(8), alice, bob, 'accepted', time.time()))
        db.close()
        ah, alice_csrf = auth_headers(data, alice)
        bh, _ = auth_headers(data, bob)
        ch, _ = auth_headers(data, carol)

        # 1. Photo post in object mode.
        st, _ = post_multipart(op, base, '/posts',
                               {'csrf': alice_csrf, 'body': 'object photo post'},
                               {'media': ('photo.png', 'image/png', PNG)},
                               headers=ah)
        check('e2e: photo post uploads in object mode', st == 200, st)
        st, _, feed = get(op, base, '/', headers=ah)
        m = re.search(r'/media/([A-Za-z0-9_.\-]+)', feed)
        check('e2e: feed renders /media/ link for the photo', bool(m))
        key = m.group(1) if m else ''
        rows = db_rows(data, 'SELECT * FROM uploads WHERE key=?', (key,)) if key else []
        check('e2e: uploads row records storage=object',
              len(rows) == 1 and rows[0]['storage'] == 'object',
              rows[0]['storage'] if rows else 'no row')
        check('e2e: object is in the bucket', key in FakeS3Handler.store)
        check('e2e: no local copy on disk',
              not (data / 'network_uploads' / key).exists())

        # 2. /media/ serves the exact bytes through the app.
        st, h, body = get_bytes(op, base, '/media/' + key, headers=bh)
        check('e2e: friend fetches private photo via /media/',
              st == 200 and body == PNG, st)
        check('e2e: /media/ content type is image/png',
              h.get('Content-Type') == 'image/png', h.get('Content-Type'))

        # 3. Private photo: stranger gets 404 and S3 is never touched.
        gets_before = FakeS3Handler.counts['GET']
        st, _, _ = get(op, base, '/media/' + key, headers=ch)
        check('e2e: stranger gets 404 for private photo', st == 404, st)
        check('e2e: stranger 404 did not hit S3 (permission first)',
              FakeS3Handler.counts['GET'] == gets_before,
              FakeS3Handler.counts['GET'] - gets_before)
        st, _, _ = get(op, base, '/media/' + key)
        check('e2e: signed-out gets 401 for the photo', st == 401, st)

        # 4. Avatar upload + replace deletes the old object.
        st, _ = post_multipart(op, base, '/profile',
                               {'csrf': alice_csrf, 'display_name': 'Alice',
                                'bio': '', 'interests': ''},
                               {'avatar': ('a1.png', 'image/png', PNG)},
                               headers=ah)
        check('e2e: avatar upload in object mode', st == 200, st)
        av1 = db_rows(data, 'SELECT avatar_key FROM users WHERE id=?', (alice,))[0]['avatar_key']
        check('e2e: first avatar is in the bucket', av1 in FakeS3Handler.store)
        st, _ = post_multipart(op, base, '/profile',
                               {'csrf': alice_csrf, 'display_name': 'Alice',
                                'bio': '', 'interests': ''},
                               {'avatar': ('a2.png', 'image/png', PNG)},
                               headers=ah)
        av2 = db_rows(data, 'SELECT avatar_key FROM users WHERE id=?', (alice,))[0]['avatar_key']
        check('e2e: avatar replace stores the new object',
              st == 200 and av2 in FakeS3Handler.store and av2 != av1, st)
        check('e2e: avatar replace deletes the old object',
              av1 not in FakeS3Handler.store)

        # 5. Deleting the post deletes the object.
        pid = db_rows(data, 'SELECT id FROM posts WHERE media_key=?', (key,))[0]['id']
        st, _ = post(op, base, '/delete',
                     {'csrf': alice_csrf, 'post_id': str(pid)}, headers=ah)
        check('e2e: delete post', st == 200, st)
        check('e2e: delete post deletes the object',
              key not in FakeS3Handler.store)
        st, _, _ = get(op, base, '/media/' + key, headers=bh)
        check('e2e: deleted photo is 404', st == 404, st)

        # 6. Video: verified by ffprobe, then stored in the bucket.
        mp4_path = data / 'clip.mp4'
        if make_mp4(mp4_path):
            mp4 = mp4_path.read_bytes()
            st, _ = post_multipart(op, base, '/posts',
                                   {'csrf': alice_csrf, 'body': 'object video post'},
                                   {'media': ('clip.mp4', 'video/mp4', mp4)},
                                   headers=ah)
            check('e2e: 1s video uploads in object mode', st == 200, st)
            vrows = db_rows(data, "SELECT * FROM uploads WHERE content_type='video/mp4'")
            check('e2e: video row is storage=object',
                  len(vrows) == 1 and vrows[0]['storage'] == 'object')
            if vrows:
                check('e2e: video object is in the bucket',
                      vrows[0]['key'] in FakeS3Handler.store)
                st, _, vbody = get_bytes(op, base, '/media/' + vrows[0]['key'], headers=bh)
                check('e2e: video serves via /media/',
                      st == 200 and vbody == mp4, st)
        else:
            check('e2e: ffmpeg made a test mp4', False, 'ffmpeg failed')

        # 7. Legacy local photo still serves with object mode on.
        legacy_key = secrets.token_hex(16) + '.png'
        (data / 'network_uploads' / legacy_key).write_bytes(PNG)
        db = sqlite3.connect(str(data / 'network.db'))
        with db:
            db.execute('INSERT INTO uploads(key,user_id,content_type,size,created,storage)'
                       ' VALUES(?,?,?,?,?,?)',
                       (legacy_key, alice, 'image/png', len(PNG), time.time(), 'local'))
            db.execute('INSERT INTO posts(user_id,body,created,media_key,media_type)'
                       ' VALUES(?,?,?,?,?)',
                       (alice, 'legacy local photo', time.time(), legacy_key, 'image/png'))
        db.close()
        gets_before = FakeS3Handler.counts['GET']
        st, _, lbody = get_bytes(op, base, '/media/' + legacy_key, headers=bh)
        check('e2e: legacy local photo still serves in object mode',
              st == 200 and lbody == PNG, st)
        check('e2e: legacy local photo did not hit S3',
              FakeS3Handler.counts['GET'] == gets_before)

        # 8. Admin remove keeps the object; restore brings it back.
        st, _ = post_multipart(op, base, '/posts',
                               {'csrf': alice_csrf, 'body': 'victim photo for remove'},
                               {'media': ('v.png', 'image/png', PNG)},
                               headers=ah)
        st, _, feed = get(op, base, '/', headers=ah)
        vm = re.search(r'/media/([A-Za-z0-9_.\-]+)', feed)
        vkey = vm.group(1) if vm else ''
        check('e2e: victim photo uploaded for remove/restore', bool(vkey))
        mh, mitch_csrf = auth_headers(data, admin)
        st, _ = post(op, base, '/admin/remove-user',
                     {'csrf': mitch_csrf, 'user_id': str(alice),
                      'confirm_email': 'alice@example.com'}, headers=mh)
        check('e2e: admin removes the account', st == 200, st)
        check('e2e: removed account object stays in bucket (restorable)',
              vkey in FakeS3Handler.store)
        st, _, _ = get(op, base, '/media/' + vkey, headers=bh)
        check('e2e: removed account photo is 404 (row gone)', st == 404, st)
        removed = db_rows(data, 'SELECT id FROM removed_users WHERE original_user_id=?', (alice,))
        check('e2e: removed_users row exists', len(removed) == 1)
        if removed:
            st, _ = post(op, base, '/admin/restore-user',
                         {'csrf': mitch_csrf, 'removed_id': str(removed[0]['id'])},
                         headers=mh)
            check('e2e: admin restores the account', st == 200, st)
            rows = db_rows(data, 'SELECT * FROM uploads WHERE key=?', (vkey,))
            check('e2e: restored upload row keeps storage=object',
                  len(rows) == 1 and rows[0]['storage'] == 'object',
                  rows[0]['storage'] if rows else 'no row')
            # Re-seed a session for the restored Alice and fetch it.
            ah2, _ = auth_headers(data, alice)
            st, _, rbody = get_bytes(op, base, '/media/' + vkey, headers=ah2)
            check('e2e: restored photo serves again', st == 200 and rbody == PNG, st)
    finally:
        stop_app(proc)
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(data, ignore_errors=True)


def e2e_storage_down():
    """Upload with the bucket unreachable: loud failure, nothing created."""
    data = Path(tempfile.mkdtemp(prefix='quad-obj-down-'))
    env = storage_env('http://127.0.0.1:1')  # closed port
    proc, base, ready = start_app(data, 18303, env)
    try:
        check('down: server still starts with storage down', ready)
        if not ready:
            return
        op = make_client()
        uid = seed_user(data, 'Dave', 'dave@example.com')
        h, csrf_tok = auth_headers(data, uid)
        before = len(db_rows(data, 'SELECT id FROM posts'))
        st, page = post_multipart(op, base, '/posts',
                                  {'csrf': csrf_tok, 'body': 'photo when storage is down'},
                                  {'media': ('photo.png', 'image/png', PNG)},
                                  headers=h)
        check('down: upload fails with a plain error',
              st == 400 and 'unavailable' in page.lower(), st)
        check('down: no post was created',
              len(db_rows(data, 'SELECT id FROM posts')) == before)
        check('down: no local file was written as a fallback',
              len(list((data / 'network_uploads').glob('*'))) == 0)
        check('down: no uploads row was created',
              len(db_rows(data, 'SELECT * FROM uploads')) == 0)
    finally:
        stop_app(proc)
        shutil.rmtree(data, ignore_errors=True)


def e2e_local_fallback():
    """No env vars: the app is exactly as before (local disk)."""
    data = Path(tempfile.mkdtemp(prefix='quad-obj-local-'))
    proc, base, ready = start_app(data, 18305, {})
    try:
        check('local: server starts without object storage', ready)
        if not ready:
            return
        op = make_client()
        uid = seed_user(data, 'Eve', 'eve@example.com')
        h, csrf_tok = auth_headers(data, uid)
        st, _ = post_multipart(op, base, '/posts',
                               {'csrf': csrf_tok, 'body': 'local photo post'},
                               {'media': ('photo.png', 'image/png', PNG)},
                               headers=h)
        check('local: photo post uploads', st == 200, st)
        st, _, feed = get(op, base, '/', headers=h)
        m = re.search(r'/media/([A-Za-z0-9_.\-]+)', feed)
        key = m.group(1) if m else ''
        check('local: feed renders the photo', bool(m))
        rows = db_rows(data, 'SELECT * FROM uploads WHERE key=?', (key,)) if key else []
        check('local: uploads row is storage=local',
              len(rows) == 1 and rows[0]['storage'] == 'local',
              rows[0]['storage'] if rows else 'no row')
        check('local: file is on local disk',
              key and (data / 'network_uploads' / key).exists())
        st, _, body = get_bytes(op, base, '/media/' + key, headers=h)
        check('local: /media/ serves the local file',
              st == 200 and body == PNG, st)
    finally:
        stop_app(proc)
        shutil.rmtree(data, ignore_errors=True)


def config_checks():
    text = (ROOT / 'render-free.yaml').read_text()
    for var in ('OBJECT_STORAGE_ENDPOINT', 'OBJECT_STORAGE_BUCKET',
                'OBJECT_STORAGE_ACCESS_KEY', 'OBJECT_STORAGE_SECRET_KEY'):
        check('yaml: %s is sync:false' % var,
              var in text and 'sync: false' in text)
    check('yaml: no secret values in the file',
          'r2.cloudflarestorage.com' not in text.replace(
              '<account-id>.r2.cloudflarestorage.com', '')
          and 'test-secret' not in text)
    check('yaml: object storage region is auto',
          'OBJECT_STORAGE_REGION' in text and '"auto"' in text)
    docker = (ROOT / 'Dockerfile').read_text()
    check('docker: copies object_storage.py',
          'COPY object_storage.py' in docker)
    req = (ROOT / 'requirements.txt').read_text()
    check('requirements: no new dependency (stdlib only, no boto3)',
          'boto' not in req.lower())
    src = (ROOT / 'the_network_production.py').read_text()
    check('app: no public object URL is ever rendered',
          'r2.dev' not in src and 'OBJECT_STORAGE_PUBLIC_URL' not in src)


if __name__ == '__main__':
    unit_tests()
    e2e_object_mode()
    e2e_storage_down()
    e2e_local_fallback()
    config_checks()
    sys.exit(report())

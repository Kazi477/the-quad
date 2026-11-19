"""Object storage for The Campus — photos (and videos) that survive restarts.

Problem this solves (Update 11): on Render free the local disk is
ephemeral, so a photo written to ``DATA_DIR/network_uploads/`` is
wiped the first time the instance sleeps/restarts, and the post it
belonged to breaks. The database already survives (Turso, Update 10);
this module makes the *files* survive too, by storing them in an
S3-compatible object store instead of on the local disk.

Design — private bucket, proxied through the app (NOT public URLs)
------------------------------------------------------------------
The Campus has private accounts and blocked users, and ``/media/``
enforces that today (``can_view_upload``: a private user's photo is
404 for a non-friend, a blocked user gets nothing). A public bucket
URL (R2 ``r2.dev`` / custom domain) would hand anyone with the link
a photo the app itself would refuse to show them — so this build
deliberately does NOT use public URLs, does NOT enable ``r2.dev``,
and does NOT need CORS. The bucket stays private; the app fetches
the bytes server-side with its own key and serves them through the
existing ``/media/<key>`` route, after the same permission check.
That means:

* no CSP change — browsers still only ever load ``img-src 'self'``;
* no URL scheme change — feed/post-detail/avatar rendering is
  untouched, every ``/media/<key>`` link keeps working;
* a leaked key still only ever reaches the app's own permission
  checks; a leaked *object* key is useless without a signed-in
  session the app accepts.

Which provider
--------------
Any S3-compatible store works — the code only speaks plain S3
(SigV4, path-style ``/<bucket>/<key>``), so the provider is just
four environment variables. The recommended one is Cloudflare R2
(free tier, verified against pricing pages on 2026-11-19: 10 GB
storage, 1M Class A writes/month, 10M Class B reads/month, $0
egress, no expiry). One honest caveat, also verified from multiple
sources the same day: several sources say **activating R2 requires
a payment method on file** (nothing is charged inside the free
limits, but the checkout flow asks for a card). That conflicts with
Mitchell's no-payment rule, so: if R2 asks for a card at signup,
**do not add one** — use the no-card fallback instead. The fallback
is Tigris (free tier: 5 GB storage, zero egress, no card required;
S3-compatible, same four env vars, different endpoint). Backblaze B2
(10 GB, no card) is a second fallback. The code does not care which
one it is; only the endpoint/keys change. Re-verify the card
question on Cloudflare's own pricing page before signing up.

Environment variables (all four required to enable; secrets only,
never in code or in render-free.yaml — Render prompts for them):
  OBJECT_STORAGE_ENDPOINT   e.g. https://<account-id>.r2.cloudflarestorage.com
                            (R2) or https://fly.storage.tigris.dev (Tigris)
  OBJECT_STORAGE_BUCKET     e.g. the-campus-media
  OBJECT_STORAGE_ACCESS_KEY R2 API token access key / Tigris access key
  OBJECT_STORAGE_SECRET_KEY the matching secret key
  OBJECT_STORAGE_REGION     optional, default "auto" (R2's region)
  OBJECT_STORAGE_PREFIX     optional key prefix, default "" (e.g. "quad/")

When the env vars are NOT set the app behaves exactly as before:
uploads go to the local ``network_uploads/`` folder. When they ARE
set, new uploads go to object storage and the ``uploads`` table
records ``storage='object'`` per file; old local files keep
``storage='local'`` and keep serving from disk. There is no
automatic migration of old files (see "What is NOT done").

Why no boto3
------------
``boto3``/``botocore`` is a large dependency (tens of MB, its own
credential chain and retry behaviour) for four operations (PUT/GET/
DELETE/HEAD). The app is stdlib-only by design, and S3 SigV4
signing is ~60 lines of hashlib/hmac — so this module is stdlib
only (``urllib``, ``hashlib``, ``hmac``). No new entry in
requirements.txt, no Docker change beyond copying this file.

Failure behaviour (deliberate, tested)
--------------------------------------
* Upload with storage down/misconfigured: the upload FAILS with a
  plain "photo storage is unavailable" error and nothing is
  created. It never silently falls back to local disk — a silent
  fallback would look fine and then lose the photo on the next
  restart, which is exactly what this module exists to prevent.
* Serving with storage down: ``/media/`` returns 404 for that
  file (the post itself still renders). The app does not crash.
* Delete/remove: best effort — a failed object delete is logged,
  never raised, so deleting a post can never fail because the
  bucket is having a bad day. The DB row is the source of truth;
  an orphaned object is harmless (unreachable without a row).

What is NOT tested here
-----------------------
A real R2/Tigris bucket. No account or bucket was created (that
needs Mitchell's signup, and R2 may ask for a card — see above).
Everything is tested end-to-end in dev against a local fake S3
server in ``object_storage_test.py`` (same HTTP + SigV4 code path,
same app routes). The first real deploy is the first live-bucket
test. See RENDER_FREE.md for the deploy steps.
"""

import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request


class ObjectStorageError(Exception):
    """Raised when the object store rejects a request or is unreachable."""


# ------------------------------------------------------------ config
def _env(name):
    return os.environ.get(name, '').strip()


def config():
    """Return the storage config dict, or None when not configured.

    Enabled only when all four required vars are set. A missing
    secret is a configuration error, not a reason to run local-only
    while claiming object storage — callers check ``enabled()``
    first, and ``put_object`` raises if called unconfigured.
    """
    endpoint = _env('OBJECT_STORAGE_ENDPOINT')
    bucket = _env('OBJECT_STORAGE_BUCKET')
    access = _env('OBJECT_STORAGE_ACCESS_KEY')
    secret = _env('OBJECT_STORAGE_SECRET_KEY')
    if not (endpoint and bucket and access and secret):
        return None
    return {
        'endpoint': endpoint.rstrip('/'),
        'bucket': bucket,
        'access_key': access,
        'secret_key': secret,
        'region': _env('OBJECT_STORAGE_REGION') or 'auto',
        'prefix': _env('OBJECT_STORAGE_PREFIX').strip('/'),
    }


def enabled():
    """True only when object storage is fully configured."""
    return config() is not None


def _check_key(key):
    # Keys are generated by the app (uuid hex + extension). Refuse
    # anything with a path separator or traversal so a crafted key
    # can never address another object in the bucket.
    if not key or '/' in key or '\\' in key or '..' in key:
        raise ObjectStorageError('invalid object key')
    return key


def _object_path(cfg, key):
    path = '%s/%s' % (cfg['bucket'], key)
    if cfg['prefix']:
        path = '%s/%s' % (cfg['prefix'], path)
    return path


# ------------------------------------------------------------ SigV4
def _sign(cfg, method, path, headers, payload_hash):
    """Return the Authorization header for one S3 request (SigV4)."""
    now = headers['x-amz-date']
    datestamp = now[:8]
    scope = '%s/%s/s3/aws4_request' % (datestamp, cfg['region'])
    signed_headers = ';'.join(sorted(h.lower() for h in headers))
    canonical_headers = ''.join(
        '%s:%s\n' % (h.lower(), headers[h].strip())
        for h in sorted(headers))
    canonical_request = '\n'.join([
        method, urllib.parse.quote(path, safe='/'), '',
        canonical_headers, signed_headers, payload_hash])
    string_to_sign = '\n'.join([
        'AWS4-HMAC-SHA256', now, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest()])
    k_date = hmac.new(('AWS4' + cfg['secret_key']).encode(),
                      datestamp.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, cfg['region'].encode(),
                        hashlib.sha256).digest()
    k_service = hmac.new(k_region, b's3', hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b'aws4_request',
                         hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode(),
                         hashlib.sha256).hexdigest()
    return ('AWS4-HMAC-SHA256 Credential=%s/%s, '
            'SignedHeaders=%s, Signature=%s'
            % (cfg['access_key'], scope, signed_headers, signature))


def _request(cfg, method, key, data=None, content_type=None):
    """One signed S3 request. Returns (status, body bytes).

    Raises ObjectStorageError on transport failure or on any
    non-2xx/404 status (404 is returned, not raised, so callers
    can treat "not there" as a normal answer).
    """
    _check_key(key)
    path = '/' + _object_path(cfg, key)
    url = cfg['endpoint'] + path
    body = data if data is not None else b''
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        'host': urllib.parse.urlparse(cfg['endpoint']).netloc,
        'x-amz-content-sha256': payload_hash,
        'x-amz-date': _amz_date(),
    }
    if content_type:
        headers['content-type'] = content_type
    headers['Authorization'] = _sign(cfg, method, path, headers,
                                     payload_hash)
    req = urllib.request.Request(url, data=body if method in ('PUT', 'POST')
                                 else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, b''
        raise ObjectStorageError(
            'object storage %s %s failed: HTTP %s'
            % (method, key, exc.code)) from exc
    except Exception as exc:  # noqa: BLE001 - transport errors vary
        raise ObjectStorageError(
            'object storage %s %s failed: %s'
            % (method, key, type(exc).__name__)) from exc


def _amz_date():
    import time
    return time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())


# ------------------------------------------------------------ operations
def put_object(key, data, content_type, cfg=None):
    """Store bytes under key. Raises ObjectStorageError on failure."""
    cfg = cfg or config()
    if cfg is None:
        raise ObjectStorageError('object storage is not configured')
    status, _ = _request(cfg, 'PUT', key, data=data,
                         content_type=content_type)
    if status not in (200, 201, 204):
        raise ObjectStorageError(
            'object storage PUT %s failed: HTTP %s' % (key, status))
    return True


def get_object(key, cfg=None):
    """Return the bytes for key, or None when the object is absent.

    Raises ObjectStorageError when the store is unreachable or
    rejects the request (anything other than 404).
    """
    cfg = cfg or config()
    if cfg is None:
        raise ObjectStorageError('object storage is not configured')
    status, body = _request(cfg, 'GET', key)
    if status == 404:
        return None
    if status != 200:
        raise ObjectStorageError(
            'object storage GET %s failed: HTTP %s' % (key, status))
    return body


def delete_object(key, cfg=None):
    """Delete key. True when gone (including already absent).

    Raises ObjectStorageError on a real failure (e.g. 403). Callers
    that must never fail a user action should catch it and log.
    """
    cfg = cfg or config()
    if cfg is None:
        raise ObjectStorageError('object storage is not configured')
    status, _ = _request(cfg, 'DELETE', key)
    if status in (200, 204, 404):
        return True
    raise ObjectStorageError(
        'object storage DELETE %s failed: HTTP %s' % (key, status))

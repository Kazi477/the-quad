"""Database backend for The Campus — local SQLite or Turso (libSQL).

Default behaviour is unchanged: plain ``sqlite3`` on a local file
(``DATA_DIR/network.db``), exactly as before. Nothing about the app
changes unless the Turso environment variables are set.

Turso mode (Render free + Turso free)
-------------------------------------
Set ``TURSO_DATABASE_URL`` (the ``libsql://...turso.io`` URL of the
Turso database) and ``TURSO_AUTH_TOKEN``. The app then opens the
database as a libSQL **embedded replica**: a local SQLite file in
``DATA_DIR`` that syncs with the Turso cloud primary. Reads hit the
local file (fast, no per-query network hop); writes are forwarded to
the cloud primary and applied locally. On a Render free instance the
local disk is ephemeral — when the instance sleeps/restarts, the
local replica file is wiped and re-synced from Turso on the next
startup, so accounts, posts and messages survive. That is the whole
point of this module: without it, a Render free deploy would lose
its SQLite file on the first restart.

Why a wrapper is needed
------------------------
``libsql-experimental`` (the Python libSQL driver) is *almost*
``sqlite3``-compatible, but not quite — verified in dev on
2026-11-19 against libsql-experimental 0.0.55:
* no ``row_factory`` — rows come back as plain tuples, while the app
  reads rows by column name (``row['email']``) in ~80 places;
* connections do not support the context-manager protocol, while the
  app uses ``with connect() as c:`` everywhere (83 call sites);
* no ``Connection.backup()`` — the app's ``backup_database()`` uses
  the sqlite3 backup API;
* errors are plain ``ValueError`` (e.g. ``UNIQUE constraint failed``),
  not ``sqlite3.Error`` / ``sqlite3.IntegrityError``, which the app
  catches in three places.

This module wraps the libSQL connection/cursor so it *looks* like
``sqlite3`` to the app: name-addressable rows, ``with`` blocks that
commit on success, ``executescript``/``lastrowid``, and errors
translated back to ``sqlite3.Error`` / ``sqlite3.IntegrityError``.
The app's ``connect()`` only calls into here when the Turso env vars
are set; otherwise it never imports libsql at all, so the default
local run stays stdlib-only.

What is NOT tested here
-----------------------
The wrapper and the whole app are tested end-to-end in dev against a
*local* libSQL file (same driver, same code path, no sync URL) in
``render_free_test.py``. A real Turso cloud sync has NOT been tested
here: no Turso account/database was created (that needs Mitchell to
sign up with GitHub — no card — and paste the URL/token into Render).
The first real deploy is the first live sync test. If the sync URL is
unreachable at startup, ``connect_turso()`` fails loudly instead of
silently running on a local-only file — a silent fallback would look
fine and then lose data on restart, which is exactly what this
module exists to prevent.

Uploads are NOT stored in Turso: photos/videos still go to
``DATA_DIR/network_uploads/`` on the (ephemeral) Render disk and are
lost when the instance restarts. See RENDER_FREE.md.
"""

import os
import shutil
import sqlite3
import threading
from pathlib import Path

# ---------------------------------------------------------------- env
TURSO_DATABASE_URL = os.environ.get('TURSO_DATABASE_URL', '').strip()
TURSO_AUTH_TOKEN = os.environ.get('TURSO_AUTH_TOKEN', '').strip()

# Markers for the WAL/write errors the live site hit (see connect_turso).
_WAL_ERROR_MARKERS = ('wal_insert_begin', 'database is locked',
                      'database is busy', 'wal', 'busy', 'locked',
                      'cannot start a transaction within a transaction')
# The WAL self-check (below) runs once per process, guarded by this
# lock so concurrent first requests cannot run recovery twice.
_WAL_PROBE_LOCK = threading.Lock()
_WAL_PROBE_DONE = False


def _is_wal_error(exc):
    """True for the WAL/lock errors that mean 'the replica cannot
    write right now' (as opposed to a real SQL/programming error)."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _WAL_ERROR_MARKERS)


def _remove_stale_wal_files(db_path):
    """Delete a replica's -wal/-shm sidecar files.

    Safe: the -wal only holds frames not yet checkpointed into the
    main file, and on a Turso replica the cloud primary is the source
    of truth — a checkpointed main file plus a re-sync recovers the
    data. Only ever called after a WAL error, never on a healthy DB.
    """
    removed = []
    for suffix in ('-wal', '-shm'):
        try:
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
                removed.append(suffix)
        except OSError:
            pass
    return removed


def turso_enabled():
    """True only when both Turso env vars are set."""
    return bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)


# ------------------------------------------------------------ row/cursor
class _Row(tuple):
    """A tuple row that also supports ``row['column_name']``."""

    def __new__(cls, values, keys):
        obj = super().__new__(cls, tuple(values))
        obj._keys = tuple(keys or ())
        return obj

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return super().__getitem__(self._keys.index(key))
            except ValueError:
                raise KeyError(key) from None
        return super().__getitem__(key)

    def keys(self):
        return list(self._keys)


def _translate_error(exc):
    """Map a libSQL error onto the sqlite3 error the app expects."""
    msg = str(exc)
    if 'UNIQUE constraint failed' in msg or 'PRIMARY KEY' in msg:
        return sqlite3.IntegrityError(msg)
    if isinstance(exc, sqlite3.Error):
        return exc
    return sqlite3.Error(msg)


class _Cursor:
    """Wraps a libSQL cursor so it behaves like a sqlite3 cursor."""

    def __init__(self, raw):
        self._raw = raw  # a real libsql Cursor, not the connection

    # -- execution ------------------------------------------------
    def execute(self, sql, params=()):
        try:
            self._raw.execute(sql, params)
        except Exception as exc:  # libsql raises ValueError, not sqlite3.Error
            raise _translate_error(exc) from None
        return self

    def executemany(self, sql, seq):
        try:
            self._raw.executemany(sql, seq)
        except Exception as exc:
            raise _translate_error(exc) from None
        return self

    def executescript(self, sql):
        try:
            self._raw.executescript(sql)
        except Exception as exc:
            raise _translate_error(exc) from None
        return self

    # -- results --------------------------------------------------
    @property
    def description(self):
        return self._raw.description

    @property
    def lastrowid(self):
        return self._raw.lastrowid

    @property
    def rowcount(self):
        return self._raw.rowcount

    def _keys(self):
        desc = self._raw.description
        return [d[0] for d in desc] if desc else []

    def fetchone(self):
        try:
            row = self._raw.fetchone()
        except Exception as exc:
            raise _translate_error(exc) from None
        return _Row(row, self._keys()) if row is not None else None

    def fetchall(self):
        try:
            rows = self._raw.fetchall()
        except Exception as exc:
            raise _translate_error(exc) from None
        keys = self._keys()
        return [_Row(r, keys) for r in rows]

    def fetchmany(self, size=1):
        try:
            rows = self._raw.fetchmany(size)
        except Exception as exc:
            raise _translate_error(exc) from None
        keys = self._keys()
        return [_Row(r, keys) for r in rows]

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        self._raw.close()


class _Connection:
    """Wraps a libSQL connection so it behaves like sqlite3."""

    def __init__(self, raw, db_path, sync_url=None, auth_token=''):
        self._raw = raw
        self._db_path = str(db_path)
        self._sync_url = sync_url
        self._auth_token = auth_token

    # -- execution ------------------------------------------------
    def execute(self, sql, params=()):
        try:
            raw_cur = self._raw.execute(sql, params)
        except Exception as exc:
            raise _translate_error(exc) from None
        return _Cursor(raw_cur)

    def executemany(self, sql, seq):
        try:
            raw_cur = self._raw.executemany(sql, seq)
        except Exception as exc:
            raise _translate_error(exc) from None
        return _Cursor(raw_cur)

    def executescript(self, sql):
        try:
            raw_cur = self._raw.executescript(sql)
        except Exception as exc:
            raise _translate_error(exc) from None
        return _Cursor(raw_cur)

    def cursor(self):
        return _Cursor(self._raw.cursor())

    # -- transactions ---------------------------------------------
    def commit(self):
        try:
            self._raw.commit()
        except Exception as exc:
            raise _translate_error(exc) from None

    def rollback(self):
        try:
            self._raw.rollback()
        except Exception:
            pass

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    # -- Turso sync ------------------------------------------------
    def sync(self):
        """Push/pull the embedded replica. No-op for a local-only file."""
        if not self._sync_url:
            return
        try:
            self._raw.sync()
        except Exception as exc:
            raise sqlite3.Error('Turso sync failed: %s' % exc) from None


# ------------------------------------------------------------ entry points
def _open_raw(db_path, sync_url=None, auth_token=''):
    """One libsql.connect call. Kept separate so the recovery path
    in connect_turso can re-open the replica cleanly."""
    import libsql_experimental as libsql
    kwargs = {}
    if sync_url:
        kwargs['sync_url'] = sync_url
        kwargs['auth_token'] = auth_token or ''
        kwargs['sync_interval'] = 60
    return libsql.connect(str(db_path), **kwargs)


def _apply_pragmas(conn):
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA synchronous=NORMAL')


def _wal_self_check(conn):
    """Once per process: prove this replica can actually write to its
    WAL before the server starts serving pages.

    Why: on Render free the process is killed when the instance
    spins down, and /tmp can keep the replica's -wal/-shm files.
    A WAL left half-written by that unclean shutdown replays fine
    for reads (so /healthz, which only does SELECT 1, stays green)
    but every real write then fails with
    ``sqlite3.Error: wal_insert_begin failed`` — which is exactly
    what took the live homepage down on 2026-11-19: every GET /
    writes through rate_limit() first, so one broken WAL 502s the
    whole site while the health check looks healthy. A checkpoint
    forces the WAL to be exercised; if it raises a WAL/lock error
    the replica is broken and the caller must run recovery.
    Non-WAL errors (e.g. the driver not supporting checkpoints)
    are logged and ignored — the check must never break a healthy DB.
    """
    try:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    except Exception as exc:
        if _is_wal_error(exc):
            raise
        print('wal_checkpoint skipped:', type(exc).__name__, exc)


def connect_turso(db_path, sync_url=None, auth_token=''):
    """Open the database as a libSQL embedded replica (or local file).

    ``sync_url``/``auth_token`` come from TURSO_DATABASE_URL /
    TURSO_AUTH_TOKEN. With no sync URL this is just a local libSQL
    file — same driver, same wrapper, no cloud sync (used by the dev
    tests). With a sync URL, libsql syncs the replica from the cloud
    primary on open; if that sync fails, it raises instead of
    silently running local-only.

    WAL recovery (Update 13): if the replica's WAL is broken after
    an unclean shutdown — the live 502 of 2026-11-19 — opening is
    retried after removing the stale -wal/-shm sidecars, and, only
    when there is a real ``libsql://`` sync URL to rebuild from,
    after deleting the local file itself so it re-syncs fresh from
    Turso (the cloud primary is the source of truth). The local
    file is NEVER deleted when there is no sync URL: in dev/local
    mode it is the only copy of the data.
    """
    try:
        import libsql_experimental as libsql  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            'Turso mode needs the libsql driver: '
            'pip install libsql-experimental') from exc
    try:
        raw = _open_raw(db_path, sync_url=sync_url, auth_token=auth_token)
    except Exception as exc:
        if not _is_wal_error(exc):
            raise sqlite3.Error(
                'Could not open Turso database (sync failed?): %s' % exc) from None
        # The WAL can be so broken that even opening the replica
        # fails (e.g. a garbage -wal left by an unclean shutdown).
        # Drop the sidecars and try once more before giving up.
        print('Turso replica failed to open (%s); removing stale '
              '-wal/-shm and retrying...' % exc)
        _remove_stale_wal_files(db_path)
        try:
            raw = _open_raw(db_path, sync_url=sync_url,
                            auth_token=auth_token)
        except Exception as exc2:
            raise sqlite3.Error(
                'Could not open Turso database (sync failed?): %s'
                % exc2) from None
    conn = _Connection(raw, db_path, sync_url=sync_url,
                       auth_token=auth_token)
    try:
        _apply_pragmas(conn)
    except Exception:
        conn.close()
        raise
    global _WAL_PROBE_DONE
    if not _WAL_PROBE_DONE:
        with _WAL_PROBE_LOCK:
            if not _WAL_PROBE_DONE:
                conn = _wal_probe_or_recover(conn, db_path, sync_url,
                                             auth_token)
                _WAL_PROBE_DONE = True
    return conn


def _wal_probe_or_recover(conn, db_path, sync_url, auth_token):
    """Run the once-per-process WAL self-check, healing the replica
    if it fails. Returns a usable connection (possibly a fresh one)
    or raises sqlite3.Error if the replica cannot be healed."""
    try:
        _wal_self_check(conn)
        return conn
    except Exception as exc:
        if not _is_wal_error(exc):
            raise
        wal_exc = exc
    print('Turso replica WAL looks stale (%s); removing -wal/-shm '
          'and retrying...' % wal_exc)
    conn.close()
    _remove_stale_wal_files(db_path)
    try:
        raw = _open_raw(db_path, sync_url=sync_url, auth_token=auth_token)
    except Exception as exc:
        raise sqlite3.Error(
            'Could not open Turso database (sync failed?): %s' % exc) from None
    conn = _Connection(raw, db_path, sync_url=sync_url,
                       auth_token=auth_token)
    try:
        _apply_pragmas(conn)
        _wal_self_check(conn)
        print('Turso replica WAL recovered after removing stale sidecars.')
        return conn
    except Exception as exc:
        if not _is_wal_error(exc):
            conn.close()
            raise
        wal_exc = exc
    # Still broken, and the local file itself must be corrupt. Only
    # rebuild it when Turso can supply the data again — never in
    # local/dev mode, where the file is the only copy.
    if not (sync_url and sync_url.startswith('libsql://')):
        conn.close()
        raise sqlite3.Error(
            'Turso replica WAL is unusable and there is no cloud '
            'database to rebuild it from: %s' % wal_exc) from None
    print('Turso replica still unusable; rebuilding the local file '
          'from Turso (cloud copy is the source of truth)...')
    conn.close()
    for suffix in ('', '-wal', '-shm'):
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        raw = _open_raw(db_path, sync_url=sync_url, auth_token=auth_token)
    except Exception as exc:
        raise sqlite3.Error(
            'Could not open Turso database (sync failed?): %s' % exc) from None
    conn = _Connection(raw, db_path, sync_url=sync_url,
                       auth_token=auth_token)
    _apply_pragmas(conn)
    _wal_self_check(conn)
    print('Turso replica rebuilt from Turso; WAL is writable again.')
    return conn


def backup_turso(db_path, target_path, sync_url, auth_token):
    """Sync the replica, then take a consistent copy of the local file.

    Returns True on success, False on failure (caller prints/logs).
    Uses the sqlite3 backup API against the local replica file, so
    the copy is a consistent snapshot even while the app holds the
    file open — the same guarantee ``backup_database()`` gives for
    plain sqlite3. The Turso cloud copy is the real durable backup;
    this local file is only a convenience copy on the (ephemeral)
    Render disk.
    """
    try:
        if sync_url and sync_url != 'local':
            import libsql_experimental as libsql
            sync_conn = libsql.connect(str(db_path), sync_url=sync_url,
                                       auth_token=auth_token or '')
            try:
                sync_conn.sync()
            finally:
                sync_conn.close()
        # 'local' (dev-test sentinel) or no sync URL: no cloud to sync
        # with — the local file IS the database, so just copy it.
        src = sqlite3.connect(str(db_path), timeout=10)
        dst = sqlite3.connect(str(target_path))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
            src.close()
        return True
    except BaseException as exc:  # noqa: BLE001 - backup must never crash the app
        # BaseException on purpose: the libSQL driver is Rust-backed
        # and can raise pyo3 PanicException (not an Exception
        # subclass) on a bad sync URL. A backup failure must still
        # never take the server down.
        print('Turso backup failed:', type(exc).__name__, exc)
        return False

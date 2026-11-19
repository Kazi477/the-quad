"""Database backend for The Quad — local SQLite or Turso (libSQL).

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

# ---------------------------------------------------------------- env
TURSO_DATABASE_URL = os.environ.get('TURSO_DATABASE_URL', '').strip()
TURSO_AUTH_TOKEN = os.environ.get('TURSO_AUTH_TOKEN', '').strip()


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
def connect_turso(db_path, sync_url=None, auth_token=''):
    """Open the database as a libSQL embedded replica (or local file).

    ``sync_url``/``auth_token`` come from TURSO_DATABASE_URL /
    TURSO_AUTH_TOKEN. With no sync URL this is just a local libSQL
    file — same driver, same wrapper, no cloud sync (used by the dev
    tests). With a sync URL, libsql syncs the replica from the cloud
    primary on open; if that sync fails, it raises instead of
    silently running local-only.
    """
    try:
        import libsql_experimental as libsql
    except ImportError as exc:
        raise RuntimeError(
            'Turso mode needs the libsql driver: '
            'pip install libsql-experimental') from exc
    kwargs = {}
    if sync_url:
        kwargs['sync_url'] = sync_url
        kwargs['auth_token'] = auth_token or ''
        kwargs['sync_interval'] = 60
    try:
        raw = libsql.connect(str(db_path), **kwargs)
    except Exception as exc:
        raise sqlite3.Error(
            'Could not open Turso database (sync failed?): %s' % exc) from None
    conn = _Connection(raw, db_path, sync_url=sync_url,
                       auth_token=auth_token)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('PRAGMA synchronous=NORMAL')
    except Exception:
        conn.close()
        raise
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

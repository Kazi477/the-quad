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
import time
from pathlib import Path

# ---------------------------------------------------------------- env
TURSO_DATABASE_URL = os.environ.get('TURSO_DATABASE_URL', '').strip()
TURSO_AUTH_TOKEN = os.environ.get('TURSO_AUTH_TOKEN', '').strip()

# Markers for the replica errors that mean 'the local file cannot
# be written/read right now' (as opposed to a real SQL/programming
# error). Update 13 covered the WAL/lock half; Update 14 adds the
# corruption half — see _is_corrupt_error below.
_WAL_ERROR_MARKERS = ('wal_insert_begin', 'database is locked',
                      'database is busy', 'wal', 'busy', 'locked',
                      'cannot start a transaction within a transaction')
# A local replica file whose header/pages are torn by an unclean
# Render spin-down comes back as one of these. libSQL/SQLite raise
# them as plain errors (often ValueError from the Rust driver,
# translated to sqlite3.Error by _translate_error), so match on
# the message text, exactly as for the WAL markers.
_CORRUPT_ERROR_MARKERS = ('malformed', 'file is not a database',
                          'not a database',
                          'database disk image is malformed',
                          'database is malformed',
                          'disk image malformed', 'file is encrypted',
                          'database file is damaged')
_RECOVERABLE_DB_ERRORS = _WAL_ERROR_MARKERS + _CORRUPT_ERROR_MARKERS
# The WAL self-check (below) runs once per process, guarded by this
# lock so concurrent first requests cannot run recovery twice.
_WAL_PROBE_LOCK = threading.Lock()
_WAL_PROBE_DONE = False
# Update 15: recovery (quarantine + rebuild from Turso) is also
# serialised. Without this, several request threads hitting a
# corrupt replica at once each rebuild it — each one quarantining
# the file the previous thread just rebuilt — which is slow and
# noisy, and can leave a half-rebuilt file behind. The lock is
# only held while a rebuild is actually happening, never on the
# normal (healthy) path.
_RECOVERY_LOCK = threading.Lock()

# Update 16 (live flapping crash, 2026-11-19): SINGLE-WRITER lock
# for the Turso embedded replica.
#
# Proven in dev against a local sqld primary (libsql-experimental
# 0.0.55): 10 threads x 5 concurrent connect + BEGIN IMMEDIATE +
# INSERT + SELECT + commit took 220 seconds, only 11 of 50
# succeeded, 39 failed with 'Remote SQlite failure: 2:0:
# Transaction timed-out' (5s/15s), one op taking 115s — while the
# identical read-only workload took 0.1s with 0 errors, and the
# identical workload against plain sqlite3 took 0.1s. The replica
# forwards writes to the primary, the primary allows one write
# transaction at a time with a ~5s budget, and queued write
# transactions burn that budget while waiting — then fail, and
# the app retries them, re-queueing. Every page view is a write
# transaction (rate_limit() BEGIN IMMEDIATE + page_views INSERT,
# session touch UPDATEs), so even a couple of simultaneous
# visitors — or a visitor plus the startup backup thread's sync —
# wedge the site: requests hang past nginx's 60s read timeout
# and Render answers 502, while /healthz (read-only, fast) stays
# green, so start.sh and Render both think the app is healthy.
# That is the load-once-then-502-then-load-again flapping of
# 2026-11-19 22:57-23:12: the queue eventually drains, one
# request gets through, the next batch re-wedges it.
#
# The fix: at most ONE write transaction is ever in flight
# against the replica, enforced here in the wrapper (every write
# path in the app goes through _Connection.execute /
# executemany / executescript; the app never uses raw cursors).
# A write statement acquires this lock and it is held until the
# transaction ends (commit / rollback / close). Reads never take
# it, so reads stay concurrent and fast. A waiter gives up after
# _WRITE_LOCK_TIMEOUT_SECONDS and raises an error whose message
# contains 'locked', so rate_limit() treats it as transient and
# falls back to in-memory counters, and other writers fail
# honestly with a 500 instead of hanging for 115s and 502ing
# everyone. The timeout must stay short: a healthy write
# transaction is a few round trips, well under the primary's 5s
# budget, so 10s covers several queued writers; waiting longer
# just re-creates the wedge locally.
_WRITE_LOCK = threading.RLock()
_WRITE_LOCK_TIMEOUT_SECONDS = 10
_WRITE_KEYWORDS = ('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'BEGIN',
                   'CREATE', 'DROP', 'ALTER', 'TRUNCATE', 'REINDEX',
                   'VACUUM')


def _is_write_sql(sql):
    """True for statements that open or perform a write
    transaction on the replica (and so must hold the single-writer
    lock). Plain SELECTs and PRAGMAs are reads and never take it."""
    s = str(sql).lstrip().upper()
    while s.startswith('('):
        s = s[1:].lstrip()
    return s.startswith(_WRITE_KEYWORDS)


def _is_corrupt_error(exc):
    """True for 'the local database file itself is damaged'
    (torn header/pages from an unclean shutdown), as opposed to a
    transient WAL/lock failure. A corrupt local replica can be
    rebuilt from Turso; a corrupt local-only file cannot, so the
    distinction matters for the 'never delete the only copy' rule."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _CORRUPT_ERROR_MARKERS)


def _is_wal_error(exc):
    """True for the WAL/lock errors AND the corruption errors that
    mean 'the replica cannot be used right now' (as opposed to a
    real SQL/programming error). Both are recoverable the same way
    when a Turso cloud copy exists to rebuild from; the corruption
    check in _is_corrupt_error is what gates the rebuild itself."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _RECOVERABLE_DB_ERRORS)


def _quarantine_local_file(db_path):
    """Move a damaged local replica file (and its -wal/-shm
    sidecars) aside as ``<path>.corrupt-<timestamp>`` instead of
    deleting it. The quarantined copy is forensic evidence of
    what went wrong, and on the ephemeral Render disk it costs
    nothing — it is wiped with the rest of /tmp on the next
    restart anyway. Returns the paths moved."""
    stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    moved = []
    for suffix in ('', '-wal', '-shm'):
        src = Path(str(db_path) + suffix)
        if not src.exists():
            continue
        dst = Path(str(db_path) + suffix + '.corrupt-' + stamp)
        try:
            if dst.exists():
                dst = Path(str(dst) + '.1')
            src.rename(dst)
            moved.append(str(dst))
        except OSError:
            # Fall back to deleting only this sidecar file; the
            # main file is never silently deleted by this helper.
            if suffix:
                try:
                    src.unlink()
                    moved.append(str(src) + ' (deleted)')
                except OSError:
                    pass
    return moved


def _rebuild_replica(db_path, sync_url, auth_token):
    """Quarantine a damaged local replica and open a fresh one
    from Turso. Only ever called when ``sync_url`` is a real
    ``libsql://`` URL — the cloud primary is the source of truth,
    so a fresh sync rebuilds the data. Raises sqlite3.Error if
    there is no cloud copy to rebuild from, or the fresh open
    fails. Returns the raw libSQL connection (caller wraps it)."""
    if not (sync_url and sync_url.startswith('libsql://')):
        raise sqlite3.Error(
            'Local database file is damaged and there is no cloud '
            'database to rebuild it from')
    moved = _quarantine_local_file(db_path)
    print('Turso replica damaged; quarantined %s and rebuilding '
          'the local file from Turso...' % (moved or 'nothing',))
    return _open_raw(db_path, sync_url=sync_url, auth_token=auth_token)


def rebuild_replica(db_path, sync_url, auth_token):
    """Public rebuild used by the app's startup check (main):
    quarantine the damaged local file, re-sync a fresh replica
    from Turso, apply the pragmas and prove it is healthy with a
    WAL checkpoint + integrity check. Returns a usable wrapped
    connection. Raises sqlite3.Error if the rebuild is not
    possible (no cloud copy) or the fresh replica is still bad —
    the caller must then fail loudly, never serve on a broken DB."""
    raw = _rebuild_replica(db_path, sync_url, auth_token)
    conn = _Connection(raw, db_path, sync_url=sync_url,
                       auth_token=auth_token)
    try:
        _apply_pragmas(conn)
        _wal_self_check(conn)
    except Exception:
        conn.close()
        raise
    print('Turso replica rebuilt from Turso; database is healthy again.')
    return conn


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
        # Update 16: how many times this connection has acquired
        # the single-writer lock (one acquire per write statement,
        # so a multi-statement transaction holds it throughout).
        # Released in full by commit/rollback/close, in a finally,
        # so a connection dropped mid-transaction can never leave
        # the lock held and wedge every later writer.
        self._write_acquires = 0

    def _acquire_write_lock(self):
        """Take the single-writer lock for one write statement.
        RLock: the same thread re-acquiring (multi-statement
        transaction) succeeds at once; another thread waits up to
        the timeout and then fails loudly instead of queueing
        behind the primary's 5s transaction budget."""
        acquired = _WRITE_LOCK.acquire(timeout=_WRITE_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            raise sqlite3.Error(
                'database write lock timed out: another writer '
                'holds the replica write lock (database is locked)')
        self._write_acquires += 1

    def _release_write_lock(self):
        """Release every acquire this connection holds. Never
        raises: a release failure must not mask the commit /
        rollback result that triggered it."""
        acquires, self._write_acquires = self._write_acquires, 0
        for _ in range(acquires):
            try:
                _WRITE_LOCK.release()
            except RuntimeError:
                pass

    # -- execution ------------------------------------------------
    def execute(self, sql, params=()):
        if _is_write_sql(sql):
            self._acquire_write_lock()
        try:
            raw_cur = self._raw.execute(sql, params)
        except Exception as exc:
            raise _translate_error(exc) from None
        return _Cursor(raw_cur)

    def executemany(self, sql, seq):
        if _is_write_sql(sql):
            self._acquire_write_lock()
        try:
            raw_cur = self._raw.executemany(sql, seq)
        except Exception as exc:
            raise _translate_error(exc) from None
        return _Cursor(raw_cur)

    def executescript(self, sql):
        # A script mixes statements (init_db's CREATE TABLEs); treat
        # the whole script as a write so the transaction it opens
        # cannot interleave with another writer's.
        self._acquire_write_lock()
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
        finally:
            self._release_write_lock()

    def rollback(self):
        try:
            self._raw.rollback()
        except Exception:
            pass
        finally:
            self._release_write_lock()

    def close(self):
        try:
            self._raw.close()
        finally:
            self._release_write_lock()

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
        return
    # Update 14: a checkpoint can pass on a file whose pages are
    # already torn — the live 'malformed SQLite database' of
    # 2026-11-19 opened fine for reads and only failed later, in
    # main()'s quick_check, after which the process died before
    # binding its port (nginx: connection refused on 127.0.0.1:8081).
    # So the probe also runs quick_check here: any result other
    # than 'ok' is corruption and must trigger the same recovery
    # as a WAL failure. The DB is tiny on the free tier, so this
    # check is cheap; it runs once per process, in this probe.
    try:
        row = conn.execute('PRAGMA quick_check').fetchone()
        result = row[0] if row is not None else 'no result'
    except Exception as exc:
        if _is_corrupt_error(exc) or _is_wal_error(exc):
            raise
        print('quick_check skipped:', type(exc).__name__, exc)
        return
    if result != 'ok':
        raise sqlite3.Error(
            'database disk image is malformed: %s' % result)


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
            # Update 14: a corrupt main file (torn header/pages —
            # 'file is not a database' / 'malformed') is NOT healed
            # by removing sidecars. If Turso can supply the data
            # again, quarantine the damaged file and rebuild from
            # the cloud; otherwise fail loudly and keep the file
            # (local mode: it is the only copy).
            # Update 15: the same applies to a WAL failure on the
            # second open — removing sidecars did not heal it, so
            # a rebuild is the only remaining recovery, not a
            # plain raise. (Live, 2026-11-19: wal_insert_begin
            # kept recurring after the restart even though the
            # homepage loaded, so an open that still cannot write
            # must be healed, not just reported.)
            if _is_wal_error(exc2):
                try:
                    with _RECOVERY_LOCK:
                        raw = _rebuild_replica(db_path, sync_url,
                                               auth_token)
                except Exception as exc3:
                    raise sqlite3.Error(
                        'Could not open Turso database (local file '
                        'is damaged and could not be rebuilt): %s'
                        % exc3) from None
            else:
                raise sqlite3.Error(
                    'Could not open Turso database (sync failed?): %s'
                    % exc2) from None
    conn = _Connection(raw, db_path, sync_url=sync_url,
                       auth_token=auth_token)
    try:
        _apply_pragmas(conn)
    except Exception as exc:
        # Update 14: pragmas run against the file too, so a torn
        # replica can fail here instead of in the probe. Same
        # recovery as everywhere else: rebuild from Turso when a
        # cloud copy exists, otherwise fail loudly.
        # Update 15: serialised under _RECOVERY_LOCK, and a fresh
        # open is tried first inside the lock — another request
        # thread may already have healed the replica, in which
        # case rebuilding again would quarantine a healthy file
        # for no reason.
        conn.close()
        if _is_wal_error(exc) and sync_url and sync_url.startswith('libsql://'):
            with _RECOVERY_LOCK:
                try:
                    raw = _open_raw(db_path, sync_url=sync_url,
                                    auth_token=auth_token)
                    conn = _Connection(raw, db_path, sync_url=sync_url,
                                       auth_token=auth_token)
                    _apply_pragmas(conn)
                except Exception:
                    raw = _rebuild_replica(db_path, sync_url, auth_token)
                    conn = _Connection(raw, db_path, sync_url=sync_url,
                                       auth_token=auth_token)
                    _apply_pragmas(conn)
        else:
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
        # Update 15: if the file still will not open after the
        # sidecars are gone, the main file itself is damaged —
        # rebuild from Turso when a cloud copy exists instead of
        # giving up here (the probe's rebuild step below never
        # gets a chance otherwise).
        if _is_wal_error(exc) and sync_url and sync_url.startswith('libsql://'):
            with _RECOVERY_LOCK:
                raw = _rebuild_replica(db_path, sync_url, auth_token)
        else:
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
    # Update 14: quarantine the damaged file instead of deleting
    # it outright, so the torn copy survives for forensics.
    raw = _rebuild_replica(db_path, sync_url, auth_token)
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

"""The Quad (formerly The Network) — dev rename, 2026-11-19: single-file Python social app, Python 3.9+, standard library only.
Quad design merged in dev only: teal modern styling, Nods, Quad Radio music (Web Audio, no external files),
stories row, enhanced admin (Traffic Shield + Manage accounts), official /login and /register screens.
User-facing branding is now "The Quad". Technical identifiers that would break existing accounts/deployments
are deliberately unchanged: SQLite file network.db, session cookie network_session, Render service name in
render.yaml, and the admin/owner email list. No live deployment from this dev copy.

Run this file locally, then open http://127.0.0.1:8080 on the same device. The
server listens on all network interfaces; hosted Python services should provide PORT.
The first account is the admin. Accounts, posts, messages and profile data live in
network.db; uploaded media lives in network_uploads beside this script.
This local server is for learning/testing. It does not update or host the public Site.

To send password reset emails, configure SMTP_HOST, SMTP_FROM, SMTP_PORT (587 by
default), SMTP_USERNAME and SMTP_PASSWORD in the server environment. For implicit
TLS on port 465, also set SMTP_USE_SSL=1. Never put mail passwords in shared code.
"""
import hashlib, hmac, html, json, mimetypes, os, re, secrets, shutil, signal, smtplib, sqlite3, ssl, subprocess, threading, time, urllib.parse, uuid, webbrowser
from email.parser import BytesParser
from email.message import EmailMessage
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get('BACKEND_PORT') or os.environ.get('PORT','8080'))
HOST = os.environ.get('HOST','0.0.0.0')
HOSTED = bool(os.environ.get('PORT') or os.environ.get('RENDER'))
PRODUCTION = os.environ.get('PRODUCTION', '1' if HOSTED else '0').strip().lower() in ('1','true','yes','on')
TRUST_PROXY = os.environ.get('TRUST_PROXY', '1' if PRODUCTION else '0').strip().lower() in ('1','true','yes','on')
DEFAULT_ADMIN_EMAILS = (
    '5rjchzz6j9@privaterelay.appleid.com',
    'mitchellwhite018@gmail.com',
)
ADMIN_EMAILS = tuple(e.strip().lower() for e in os.environ.get('ADMIN_EMAILS', ','.join(DEFAULT_ADMIN_EMAILS)).split(',') if e.strip())
OWNER_NAME = os.environ.get('OWNER_NAME','Mitchell White').strip() or 'Mitchell White'
ACTIVE_SECONDS = 120
MAX_BODY = int(os.environ.get('MAX_BODY_BYTES','27000000'))
SESSION_MAX_AGE = int(os.environ.get('SESSION_MAX_AGE_SECONDS', str(7*86400)))
SESSION_IDLE_TIMEOUT = int(os.environ.get('SESSION_IDLE_TIMEOUT_SECONDS', str(24*3600)))
PBKDF2_ITERATIONS = int(os.environ.get('PBKDF2_ITERATIONS','600000'))
APP_SECRET = os.environ.get('APP_SECRET','').strip() or secrets.token_urlsafe(48)
try:
    HERE = Path(__file__).resolve().parent
except NameError:  # iOS Python apps that execute pasted code may omit __file__
    HERE = Path.cwd()
DATA_DIR = Path(os.environ.get('DATA_DIR', str(HERE))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'network.db'
UPLOAD_DIR = DATA_DIR / 'network_uploads'
BACKUP_DIR = DATA_DIR / 'backups'
REMOVED_DIR = DATA_DIR / 'removed_uploads'
REMOVED_RETENTION_DAYS = 30
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
REMOVED_DIR.mkdir(parents=True, exist_ok=True)
if PRODUCTION and not os.environ.get('APP_SECRET'):
    print('WARNING: APP_SECRET is not set. Set a stable random APP_SECRET in production.')


def connect():
    db = sqlite3.connect(str(DB_PATH), timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('PRAGMA synchronous=NORMAL')
    return db


def add_column(db, table, column, declaration):
    if column not in {r['name'] for r in db.execute('PRAGMA table_info(%s)' % table)}:
        db.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, column, declaration))


def init_db():
    with connect() as c:
        # Set WAL once during startup. Repeating this on every request can
        # cause needless locking and delays on a mobile SQLite database.
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,is_admin INTEGER NOT NULL DEFAULT 0,joined REAL NOT NULL,last_seen REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,body TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,body TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,csrf TEXT NOT NULL,expires REAL NOT NULL,last_seen REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS posts_created_idx ON posts(created);
        CREATE INDEX IF NOT EXISTS posts_user_created_idx ON posts(user_id,created DESC);
        CREATE INDEX IF NOT EXISTS comments_post_idx ON comments(post_id,created);
        CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
        ''')
        for col, decl in [('display_name','TEXT'),('bio','TEXT'),('interests','TEXT'),('avatar_key','TEXT'),('recovery_hash','TEXT'),('must_change_password','INTEGER NOT NULL DEFAULT 0')]: add_column(c,'users',col,decl)
        for col, decl in [('media_key','TEXT'),('media_type','TEXT'),('music_track','TEXT'),('spotify_url','TEXT'),('spotify_track_id','TEXT'),('original_post_id','INTEGER'),('group_id','INTEGER'),('is_pinned','INTEGER NOT NULL DEFAULT 0'),('is_hidden','INTEGER NOT NULL DEFAULT 0')]: add_column(c,'posts',col,decl)
        add_column(c,'comments','parent_id','INTEGER')
        # Top 5 (Update 6): hashtags, reactions, saved/pinned, groups, events,
        # polls, reports, notification prefs. FTS5 for post search, kept in
        # sync by triggers; if FTS5 is unavailable the search code falls
        # back to LIKE (see search_posts).
        c.executescript('''
        CREATE TABLE IF NOT EXISTS hashtags(id INTEGER PRIMARY KEY AUTOINCREMENT,tag TEXT NOT NULL UNIQUE,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS post_hashtags(post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,hashtag_id INTEGER NOT NULL REFERENCES hashtags(id) ON DELETE CASCADE,PRIMARY KEY(post_id,hashtag_id));
        CREATE TABLE IF NOT EXISTS reactions(post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,kind TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(post_id,user_id));
        CREATE TABLE IF NOT EXISTS comment_reactions(comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,kind TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(comment_id,user_id));
        CREATE TABLE IF NOT EXISTS saved_posts(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,created REAL NOT NULL,PRIMARY KEY(user_id,post_id));
        CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',visibility TEXT NOT NULL DEFAULT 'public',owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,role TEXT NOT NULL DEFAULT 'member',status TEXT NOT NULL DEFAULT 'accepted',created REAL NOT NULL,PRIMARY KEY(group_id,user_id));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,title TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',location TEXT NOT NULL DEFAULT '',starts_at REAL NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS event_rsvps(event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,status TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(event_id,user_id));
        CREATE TABLE IF NOT EXISTS polls(id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,question TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS poll_options(id INTEGER PRIMARY KEY AUTOINCREMENT,poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,text TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS poll_votes(poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,option_id INTEGER NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,created REAL NOT NULL,PRIMARY KEY(poll_id,user_id));
        CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT,reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,target_type TEXT NOT NULL,target_id TEXT NOT NULL,reason TEXT NOT NULL,details TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'open',created REAL NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS notification_prefs(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,kind TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(user_id,kind));
        CREATE INDEX IF NOT EXISTS reports_status_idx ON reports(status,created);
        CREATE INDEX IF NOT EXISTS groups_created_idx ON groups(created);
        CREATE INDEX IF NOT EXISTS events_starts_idx ON events(starts_at);
        CREATE INDEX IF NOT EXISTS posts_group_idx ON posts(group_id,created);
        CREATE INDEX IF NOT EXISTS posts_hidden_idx ON posts(is_hidden,created);
        ''')
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(body, content='posts', content_rowid='id')")
            c.executescript('''
            CREATE TRIGGER IF NOT EXISTS posts_fts_ai AFTER INSERT ON posts BEGIN INSERT INTO posts_fts(rowid, body) VALUES (new.id, new.body); END;
            CREATE TRIGGER IF NOT EXISTS posts_fts_ad AFTER DELETE ON posts BEGIN INSERT INTO posts_fts(posts_fts, rowid, body) VALUES('delete', old.id, old.body); END;
            CREATE TRIGGER IF NOT EXISTS posts_fts_au AFTER UPDATE ON posts BEGIN INSERT INTO posts_fts(posts_fts, rowid, body) VALUES('delete', old.id, old.body); INSERT INTO posts_fts(rowid, body) VALUES (new.id, new.body); END;
            ''')
            # Backfill any posts that predate the FTS table.
            c.execute("INSERT INTO posts_fts(rowid, body) SELECT id, body FROM posts WHERE id NOT IN (SELECT rowid FROM posts_fts)")
        except sqlite3.Error as exc:
            print('FTS5 post search unavailable, search will use LIKE:', exc)
        c.executescript('''
        CREATE TABLE IF NOT EXISTS uploads(key TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,content_type TEXT NOT NULL,size INTEGER NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS likes(post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,created REAL NOT NULL,PRIMARY KEY(post_id,user_id));
        CREATE TABLE IF NOT EXISTS friendships(id TEXT PRIMARY KEY,requester_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,addressee_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,status TEXT NOT NULL,created REAL NOT NULL,UNIQUE(requester_id,addressee_id));
        CREATE TABLE IF NOT EXISTS user_settings(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,private_account INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS blocks(blocker_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,blocked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,created REAL NOT NULL,PRIMARY KEY(blocker_id,blocked_id));
        CREATE TABLE IF NOT EXISTS password_reset_codes(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,code_hash TEXT NOT NULL,expires REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS password_reset_limits(email_hash TEXT PRIMARY KEY,window_start REAL NOT NULL,request_count INTEGER NOT NULL,last_sent REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS rate_limits(action TEXT NOT NULL,key_hash TEXT NOT NULL,window_start REAL NOT NULL,request_count INTEGER NOT NULL,last_seen REAL NOT NULL,PRIMARY KEY(action,key_hash));
        CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,action TEXT NOT NULL,target_type TEXT NOT NULL,target_id TEXT,metadata TEXT NOT NULL DEFAULT '{}',created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,body TEXT NOT NULL,created REAL NOT NULL,read_at REAL);
        CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,actor_id INTEGER REFERENCES users(id) ON DELETE CASCADE,kind TEXT NOT NULL,target_id TEXT,body TEXT NOT NULL,created REAL NOT NULL,read_at REAL);
        CREATE TABLE IF NOT EXISTS page_views(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,path TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS removed_users(id INTEGER PRIMARY KEY AUTOINCREMENT,original_user_id INTEGER NOT NULL,name TEXT NOT NULL,email TEXT NOT NULL,password_hash TEXT NOT NULL,is_admin INTEGER NOT NULL DEFAULT 0,joined REAL NOT NULL,data_json TEXT NOT NULL DEFAULT '{}',backup_file TEXT,removed_at REAL NOT NULL,removed_by INTEGER,restore_deadline REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS listening_notes(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,track_title TEXT NOT NULL,artist TEXT NOT NULL DEFAULT '',spotify_url TEXT,spotify_track_id TEXT,created REAL NOT NULL,expires_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS listening_notes_expires_idx ON listening_notes(expires_at);
        CREATE INDEX IF NOT EXISTS notification_user_idx ON notifications(user_id,created);
        CREATE INDEX IF NOT EXISTS notification_unread_idx ON notifications(user_id,read_at);
        CREATE INDEX IF NOT EXISTS message_pair_idx ON messages(sender_id,recipient_id,created);
        CREATE INDEX IF NOT EXISTS message_unread_idx ON messages(recipient_id,read_at);
        CREATE INDEX IF NOT EXISTS friendship_incoming_idx ON friendships(addressee_id,status,created);
        CREATE INDEX IF NOT EXISTS page_views_created_idx ON page_views(created);
        CREATE INDEX IF NOT EXISTS page_views_user_created_idx ON page_views(user_id,created);
        CREATE INDEX IF NOT EXISTS uploads_user_idx ON uploads(user_id,created);
        CREATE INDEX IF NOT EXISTS blocks_blocked_idx ON blocks(blocked_id,blocker_id);
        CREATE INDEX IF NOT EXISTS audit_created_idx ON audit_log(created);
        ''')
        # Keep operational tables bounded and remove expired credentials.
        now=time.time()
        c.execute('DELETE FROM page_views WHERE created<?',(now-90*86400,))
        c.execute('DELETE FROM sessions WHERE expires<=? OR last_seen<?',(now,now-SESSION_IDLE_TIMEOUT,))
        c.execute('DELETE FROM password_reset_codes WHERE expires<=?',(now,))
        c.execute('DELETE FROM rate_limits WHERE last_seen<?',(now-2*86400,))
        c.execute('DELETE FROM removed_users WHERE restore_deadline<?',(now,))
        c.execute('DELETE FROM listening_notes WHERE expires_at<=?',(now,))
    import_legacy_database()
    # Keep the named owner account as admin, including accounts imported from
    # the older database. This does not change the account's password.
    with connect() as c:
        placeholders = ','.join('?' for _ in ADMIN_EMAILS)
        c.execute('UPDATE users SET is_admin=1, name=?, display_name=? WHERE lower(email) IN (%s)' % placeholders,
                  (OWNER_NAME, OWNER_NAME, *ADMIN_EMAILS))


def import_legacy_database():
    """Bring accounts/posts across from the user's earlier facebook_clone.db once."""
    legacy = HERE / 'facebook_clone.db'
    if not legacy.exists() or legacy.resolve() == DB_PATH.resolve():
        return
    try:
        src = sqlite3.connect(str(legacy), timeout=10)
        src.row_factory = sqlite3.Row
        tables = {r['name'] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'users' not in tables:
            src.close()
            return
        cols = {r['name'] for r in src.execute('PRAGMA table_info(users)')}
        with connect() as dst:
            user_map = {}
            if {'first_name','last_name','email','password_hash'} <= cols:
                old_users = src.execute('SELECT id,first_name,last_name,email,password_hash FROM users').fetchall()
                for old in old_users:
                    name = ('%s %s' % (old['first_name'] or '',old['last_name'] or '')).strip() or 'Member'
                    dst.execute('INSERT OR IGNORE INTO users(id,name,email,password_hash,is_admin,joined,last_seen) VALUES(?,?,?,?,0,?,0)',(old['id'],name,old['email'].lower(),old['password_hash'],time.time()))
                    current=dst.execute('SELECT id FROM users WHERE email=?',(old['email'].lower(),)).fetchone()
                    if current:user_map[old['id']]=current['id']
            elif {'id','name','email','password_hash'} <= cols:
                for old in src.execute('SELECT id,name,email,password_hash FROM users').fetchall():
                    dst.execute('INSERT OR IGNORE INTO users(id,name,email,password_hash,is_admin,joined,last_seen) VALUES(?,?,?,?,0,?,0)',(old['id'],old['name'],old['email'].lower(),old['password_hash'],time.time()))
                    current=dst.execute('SELECT id FROM users WHERE email=?',(old['email'].lower(),)).fetchone()
                    if current:user_map[old['id']]=current['id']
            if 'posts' in tables:
                pcols={r['name'] for r in src.execute('PRAGMA table_info(posts)')}
                if {'id','user_id','body','created'} <= pcols:
                    for post in src.execute('SELECT id,user_id,body,created FROM posts').fetchall():
                        mapped=user_map.get(post['user_id'])
                        if mapped:dst.execute('INSERT OR IGNORE INTO posts(id,user_id,body,created) VALUES(?,?,?,?)',(post['id'],mapped,post['body'],post['created']))
        src.close()
    except (sqlite3.Error, KeyError) as exc:
        print('Legacy database import skipped; original file was left unchanged:',exc)


EMAIL_RE = re.compile(r"^[^\s@\x00-\x1f\x7f]+@[^\s@\x00-\x1f\x7f]+\.[^\s@\x00-\x1f\x7f]+$")


def normalize_email(value):
    email=(value or '').strip().lower()
    if len(email)>254 or not EMAIL_RE.fullmatch(email):
        return ''
    return email


def password_hash(password, salt=None, iterations=None):
    salt = salt or secrets.token_hex(16)
    iterations = int(iterations or PBKDF2_ITERATIONS)
    val = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), iterations).hex()
    return 'pbkdf2_sha256$%s$%s$%s' % (iterations, salt, val)


def check_password(password, encoded):
    try:
        if encoded.startswith('pbkdf2_sha256$'):
            _, iterations, salt, expected = encoded.split('$', 3)
            actual = password_hash(password, salt, int(iterations)).split('$', 3)[3]
            return secrets.compare_digest(actual, expected)
        if encoded.startswith('pbkdf2$'):
            # Compatibility with the previous build (260k iterations).
            _, salt, expected = encoded.split('$', 2)
            actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), 260000).hex()
            return secrets.compare_digest(actual, expected)
        if len(encoded) == 64:  # very old demo SHA-256; upgraded on successful login
            return secrets.compare_digest(hashlib.sha256(password.encode('utf-8')).hexdigest(), encoded)
    except (ValueError, TypeError):
        return False
    return False


def password_needs_rehash(encoded):
    if not encoded.startswith('pbkdf2_sha256$'):
        return True
    try:
        return int(encoded.split('$',3)[1]) < PBKDF2_ITERATIONS
    except (ValueError, IndexError):
        return True


DUMMY_PASSWORD_HASH = password_hash('dummy-login-value-'+APP_SECRET[:12])

# Common / weak passwords rejected on registration and password reset.
# Existing accounts are NOT forced to change: this check only runs when a
# password is newly set, so a previously-registered weak password still
# signs in until the user changes it (see MERGE_NOTES, residual risk).
COMMON_PASSWORDS = frozenset({
    'password','password1','password123','password1234','password12345',
    '123456','1234567','12345678','123456789','1234567890','12345678901','123456789012',
    'qwerty','qwerty123','qwerty1234','qwerty123456','letmein','letmein123','letmein12345',
    'welcome','welcome123','welcome12345','admin','admin123','admin1234','admin123456',
    'iloveyou','iloveyou123','monkey','monkey123','monkey123456','dragon','dragon123456',
    'sunshine','sunshine123','princess','princess123','football','football123',
    'baseball','baseball123','superman','superman123','michael','michael12345',
    '111111','111111111111','000000','000000000000','123123','123123123123',
    'abcdefghijkl','abcdefghijklmnopqrstuvwxyz','1234567890ab','1q2w3e4r5t6y',
})
def validate_password(pwd, name='', email=''):
    """Return an error string, or None when the password is acceptable.
    Policy: 12-256 chars, at least 2 of (lower, upper, digit, symbol),
    not a common password, not a repeated/sequential string, and not
    containing the user's own name or email local part."""
    if not 12 <= len(pwd) <= 256:
        return 'Use a password that is 12-256 characters long.'
    low = pwd.lower()
    if low in COMMON_PASSWORDS:
        return 'That password is too common. Choose a less predictable one.'
    if len(set(pwd)) == 1:
        return 'That password is too simple. Use varied characters.'
    seq = 'abcdefghijklmnopqrstuvwxyz0123456789'
    if low in seq or low in seq[::-1]:
        return 'That password is too simple. Use varied characters.'
    local = (email or '').split('@')[0].strip().lower()
    if len(local) >= 4 and local in low:
        return 'Do not use your email address in your password.'
    for token in (name or '').replace('.', ' ').split():
        if len(token) >= 4 and token.lower() in low:
            return 'Do not use your name in your password.'
    classes = sum((any(c.islower() for c in pwd), any(c.isupper() for c in pwd),
                   any(c.isdigit() for c in pwd), any(not c.isalnum() for c in pwd)))
    if classes < 2:
        return 'Use at least two kinds of characters (letters, numbers, symbols).'
    return None

def password_must_change(encoded, pwd, name='', email=''):
    """True when a password must be changed on next login.

    We can only judge a password's strength when the user types it (the
    stored value is a one-way hash), so this is checked at login time with
    the plaintext the user just supplied. It is also true for any stored
    hash that is not the current PBKDF2-SHA256 format / iteration count
    (legacy 260k hashes and very old plain SHA-256 demo hashes)."""
    if password_needs_rehash(encoded):
        return True
    return validate_password(pwd, name, email) is not None


def reset_token(purpose):
    """Stateless signed token for unauthenticated password-reset forms.

    HMAC-signed with APP_SECRET over purpose + timestamp; no server state,
    no session needed, expires after 15 minutes. A cross-site attacker
    cannot forge one without APP_SECRET."""
    ts = int(time.time())
    msg = '%s|%d' % (purpose, ts)
    sig = hmac.new(APP_SECRET.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()[:32]
    return '%d.%s' % (ts, sig)


def verify_reset_token(token, purpose, max_age=900):
    try:
        ts_str, sig = str(token or '').split('.', 1)
        ts = int(ts_str)
    except (ValueError, TypeError):
        return False
    if abs(time.time() - ts) > max_age:
        return False
    msg = '%s|%d' % (purpose, ts)
    expected = hmac.new(APP_SECRET.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def escape_like(term):
    """Escape LIKE wildcards so a search term cannot act as a wildcard."""
    return term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

# Quad Radio: built-in Web Audio tracks only — no external audio files, no third-party hosts.
MUSIC_TRACKS = {
    'quad-dawn': 'Quad Radio · Dawn Loop',
    'quad-night': 'Quad Radio · Night Loop',
    'quad-quad': 'Quad Radio · Quad Anthem',
}

# Spotify Listening Notes (Instagram Notes-style, manual only for now).
# There is NO Spotify developer app / client credentials in this codebase and
# no user's Spotify is connected, so there is NO automatic real-time sync.
# A note is set by hand by its owner; the Spotify link is only a link out
# to open.spotify.com (plus a no-auth embed player). True live now-playing
# would need a Spotify developer app + per-user OAuth with the
# user-read-currently-playing scope — see /notes/refresh and MERGE_NOTES.
SPOTIFY_TRACK_ID_RE = re.compile(r'^[A-Za-z0-9]{22}$')
LISTENING_NOTE_TTL = 24 * 3600
SPOTIFY_LINK_HELP = 'Paste a Spotify track link like https://open.spotify.com/track/AAAAAAAAAAAAAAAAAAAAAA (22 characters after /track/).'

def parse_spotify_link(raw):
    """Validate a Spotify track link/id. Returns (track_id, canonical_url, error).

    Accepts: a bare 22-char track id, spotify:track:<id>, or an
    open.spotify.com/track/<id> URL (with optional query/fragment).
    Anything else — other hosts, other Spotify paths, malformed ids —
    is rejected. No network call is made; the id is NOT verified to
    exist on Spotify, only that it is well-formed."""
    value = (raw or '').strip()
    if not value:
        return (None, None, None)
    if len(value) > 300:
        return (None, None, 'That Spotify link is too long.')
    if value.startswith('spotify:track:'):
        tid = value[len('spotify:track:'):]
        if SPOTIFY_TRACK_ID_RE.fullmatch(tid):
            return (tid, 'https://open.spotify.com/track/' + tid, None)
        return (None, None, 'That Spotify track link is not valid. ' + SPOTIFY_LINK_HELP)
    if SPOTIFY_TRACK_ID_RE.fullmatch(value):
        return (value, 'https://open.spotify.com/track/' + value, None)
    try:
        parsed = urllib.parse.urlparse(value)
    except Exception:
        return (None, None, 'That Spotify track link is not valid. ' + SPOTIFY_LINK_HELP)
    if parsed.scheme not in ('http', 'https') or parsed.netloc.lower() not in ('open.spotify.com', 'www.open.spotify.com'):
        return (None, None, 'Only Spotify track links are accepted. ' + SPOTIFY_LINK_HELP)
    parts = [p for p in parsed.path.split('/') if p]
    if len(parts) >= 2 and parts[0] == 'track' and SPOTIFY_TRACK_ID_RE.fullmatch(parts[1]):
        return (parts[1], 'https://open.spotify.com/track/' + parts[1], None)
    return (None, None, 'That Spotify track link is not valid. ' + SPOTIFY_LINK_HELP)

def active_listening_note(c, user_id, now=None):
    now = time.time() if now is None else now
    return c.execute('SELECT * FROM listening_notes WHERE user_id=? AND expires_at>? ORDER BY created DESC LIMIT 1', (user_id, now)).fetchone()

def visible_listening_note(c, viewer_id, owner_id):
    """A note is visible to its owner, to anyone who can view the owner's
    posts, and to nobody else. In particular a non-friend cannot see a
    private user's note, and a blocked user cannot see it either
    (can_view_post covers both). Expired notes are never visible."""
    if viewer_id == owner_id:
        return active_listening_note(c, owner_id)
    if not can_view_post(c, viewer_id, owner_id):
        return None
    return active_listening_note(c, owner_id)

def spotify_embed_html(track_id):
    if not track_id or not SPOTIFY_TRACK_ID_RE.fullmatch(track_id):
        return ''
    return '<iframe class="spotify-embed" src="https://open.spotify.com/embed/track/%s" loading="lazy" title="Spotify player"></iframe>' % esc(track_id)

def spotify_note_html(note, embed=False):
    """Escaped rendering of one Listening Note. Never auto-plays."""
    if not note:
        return ''
    title = esc(note['track_title'])
    artist = esc(note['artist']) if note['artist'] else ''
    by = ' — %s' % artist if artist else ''
    out = '<div class="spotify-note"><b>♫ %s%s</b>' % (title, by)
    if note['spotify_track_id']:
        out += ' · <a href="https://open.spotify.com/track/%s" target="_blank" rel="noopener">Listen on Spotify</a>' % esc(note['spotify_track_id'])
    out += '<br><small>Set %s · expires after 24 hours</small>' % esc(date(note['created']))
    if embed and note['spotify_track_id']:
        out += spotify_embed_html(note['spotify_track_id'])
    out += '</div>'
    return out

def listening_note_form(user, note):
    """Set/change/clear form for the signed-in user's own note."""
    title = esc(note['track_title']) if note else ''
    artist = esc(note['artist']) if note else ''
    link = esc(note['spotify_url']) if note and note['spotify_url'] else ''
    out = '<section class="card"><h2>Your Listening Note</h2><p class="small">Like an Instagram Note — pick a song to display for 24 hours, anytime. Friends can see it. If your account is private, strangers cannot.</p>'
    if note:
        out += spotify_note_html(note, embed=True)
    out += ('<form method="post" action="/listening-note">%s'
        '<label>Song title</label><input name="track_title" value="%s" required maxlength="120" placeholder="Song title">'
        '<label>Artist</label><input name="artist" value="%s" maxlength="120" placeholder="Artist (optional)">'
        '<label>Spotify track link (optional)</label><input name="spotify_link" value="%s" maxlength="300" placeholder="https://open.spotify.com/track/...">'
        '<button class="btn">%s</button></form>') % (form_csrf(user), title, artist, link, 'Update my note' if note else 'Set my note')
    if note:
        out += ('<form method="post" action="/listening-note" data-confirm="Clear your Listening Note?">%s'
            '<input type="hidden" name="clear" value="1"><button class="light">Clear my note</button></form>') % form_csrf(user)
    out += '<p class="small">Live Spotify sync is not set up yet — this note is set by hand. <a href="/notes/refresh">Why live sync needs Spotify to be linked</a></p></section>'
    return out



def rate_key(raw):
    return hashlib.sha256((APP_SECRET+'|'+str(raw)).encode('utf-8')).hexdigest()


def rate_limit(action, raw_key, limit, window_seconds):
    """Return True when allowed. Stores only a keyed one-way identifier, never raw IP/email data."""
    now=time.time();key_hash=rate_key(raw_key)
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT window_start,request_count FROM rate_limits WHERE action=? AND key_hash=?',(action,key_hash)).fetchone()
        if not row or now-row['window_start']>=window_seconds:
            c.execute('INSERT INTO rate_limits(action,key_hash,window_start,request_count,last_seen) VALUES(?,?,?,?,?) ON CONFLICT(action,key_hash) DO UPDATE SET window_start=excluded.window_start,request_count=1,last_seen=excluded.last_seen',(action,key_hash,now,1,now))
            return True
        if row['request_count']>=limit:
            c.execute('UPDATE rate_limits SET last_seen=? WHERE action=? AND key_hash=?',(now,action,key_hash))
            return False
        c.execute('UPDATE rate_limits SET request_count=request_count+1,last_seen=? WHERE action=? AND key_hash=?',(now,action,key_hash))
        return True


def audit_event(c, actor_user_id, action, target_type, target_id=None, metadata=None):
    payload=json.dumps(metadata or {},ensure_ascii=False,separators=(',',':'))[:8000]
    c.execute('INSERT INTO audit_log(actor_user_id,action,target_type,target_id,metadata,created) VALUES(?,?,?,?,?,?)',(actor_user_id,action,target_type,str(target_id) if target_id is not None else None,payload,time.time()))


def backup_database():
    """Create a consistent SQLite backup and keep the newest seven copies."""
    stamp=time.strftime('%Y%m%d-%H%M%S',time.gmtime())
    target=BACKUP_DIR/('network-%s.db'%stamp)
    try:
        src=connect();dst=sqlite3.connect(str(target))
        with dst:src.backup(dst)
        dst.close();src.close()
        backups=sorted(BACKUP_DIR.glob('network-*.db'),key=lambda x:x.stat().st_mtime,reverse=True)
        for old in backups[7:]:old.unlink(missing_ok=True)
        return target
    except Exception as exc:
        print('Database backup failed:',type(exc).__name__)
        target.unlink(missing_ok=True)
        return None


def backup_worker():
    while True:
        time.sleep(6*3600)
        backup_database()


def esc(v): return html.escape(str(v if v is not None else ''), quote=True)
def display(u): return (u['display_name'] or u['name'] or 'Member')
def avatar(u, size=42):
    name = display(u)
    if u['avatar_key']:
        return '<img class="avatar" width="%s" height="%s" src="/media/%s" alt="">' % (size,size,urllib.parse.quote(u['avatar_key']))
    return '<span class="avatar av%s">%s</span>' % (size,esc(name[:1].upper()))
def date(ts): return time.strftime('%d %b %Y · %H:%M', time.localtime(ts)) if ts else ''

CSS = '''
:root{color-scheme:light dark;--ink:#0f1f1e;--muted:#5b6b6a;--brand:#0d9488;--brand-dark:#0f766e;--accent:#14b8a6;--line:#dfe8e7;--surface:#ffffff;--soft:#f0f7f6;--shadow:0 12px 32px #0f766e14}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#f2faf9 0,#eef4f3 280px);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{position:sticky;top:0;background:#ffffffe6;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-bottom:1px solid var(--line);padding:11px max(calc((100% - 1160px)/2),18px);display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;z-index:5}.brand{font-size:23px;font-weight:800;letter-spacing:-.8px;color:var(--ink);text-decoration:none}.quad-pill{display:inline-block;vertical-align:middle;font-size:11px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:#fff;background:var(--brand);border-radius:999px;padding:4px 10px;margin-left:6px}nav{display:flex;align-items:center;gap:4px;flex-wrap:wrap}nav a{padding:8px 14px;border-radius:999px;font-weight:650;font-size:13px;color:#3b4a49;text-decoration:none}nav a:hover{background:var(--soft);color:var(--brand-dark);text-decoration:none}nav form{display:inline}nav form button{border-radius:999px}a{color:var(--brand);text-decoration:none}a:hover{text-decoration:underline}main{max-width:1160px;margin:26px auto;padding:0 18px}.layout{display:grid;grid-template-columns:170px minmax(0,1fr) 258px;gap:20px;align-items:start}.side,.rail{position:sticky;top:78px}.side a{display:block;padding:10px 14px;border-radius:999px;font-weight:650;color:#48566f;text-decoration:none}.side a:hover{background:#e6f4f2;color:var(--brand-dark);text-decoration:none}.card{background:var(--surface);border:1px solid var(--line);border-radius:20px;padding:20px;margin:14px 0;box-shadow:var(--shadow)}h1{font-size:30px;line-height:1.18;letter-spacing:-.7px;margin:8px 0 18px}h2{font-size:19px;letter-spacing:-.25px;margin:4px 0 12px}h3{font-size:15px;margin:8px 0}.eyebrow{font-size:11px;letter-spacing:1.4px;color:var(--brand);font-weight:800;text-transform:uppercase}p{color:var(--muted)}.avatar{display:inline-flex;vertical-align:middle;object-fit:cover;align-items:center;justify-content:center;flex:none;border-radius:50%;background:#d9f2ef;color:var(--brand-dark);font-weight:750;font-size:18px}.row{display:flex;align-items:center;gap:10px}.grow{flex:1}.muted,small,time{font-size:12px;color:var(--muted)}input,textarea,select{width:100%;padding:11px 12px;border:1px solid #d5dce9;border-radius:12px;font:inherit;background:white;color:var(--ink)}input:focus,textarea:focus,select:focus{outline:3px solid #0d948822;border-color:var(--brand)}input[type=checkbox],input[type=file]{width:auto}textarea{min-height:82px;resize:vertical}label{display:block;font-size:13px;font-weight:650;margin:12px 0 6px}button,.btn{display:inline-flex;align-items:center;justify-content:center;background:var(--brand);border:0;border-radius:999px;color:white;padding:10px 16px;font:inherit;font-weight:700;cursor:pointer;transition:background .15s,transform .15s;text-decoration:none}button:hover,.btn:hover{background:var(--brand-dark);text-decoration:none;transform:translateY(-1px)}button:disabled{opacity:.6;cursor:wait;transform:none}.btn{margin-top:10px}.light{background:#edf1f8;color:#34435d}.light:hover{background:#e1e8f5;color:#203254}.danger{background:#a83c49;padding:7px 12px;font-size:13px}.inline{display:inline}.post{white-space:pre-wrap;overflow-wrap:anywhere;margin:15px 0;color:#27344a}.post img.media,.post video.media{display:block;width:100%;max-height:560px;object-fit:contain;background:#f2f4f7;border-radius:14px;margin-top:10px}.post time{display:block}.actions{display:flex;gap:8px;border-top:1px solid var(--line);padding-top:10px}.actions button{flex:1;background:transparent;color:#596a81}.actions button:hover,.actions .liked{background:#e6f4f2;color:var(--brand-dark)}.notice{background:#fff9e8;border-color:#f3e6bd;padding:13px 16px;border-radius:14px}.unread{background:#eaf7f5}.person{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid #edf0f4}.person:last-child{border:0}.person p{margin:2px 0}.small{font-size:13px}.rail h2{font-size:16px;margin:2px 0 8px}.friend-row{display:flex;align-items:center;gap:8px;padding:6px 0}.friend-post{border-top:1px solid #edf0f4;padding:9px 0}.friend-post a{font-weight:bold}.friend-post p{margin:3px 0;overflow-wrap:anywhere}.rail .all-link{display:block;text-align:center;padding:9px;border-radius:999px;background:var(--soft);font-weight:700;font-size:13px}.message-list{height:360px;overflow:auto;display:flex;flex-direction:column;gap:8px;border-bottom:1px solid var(--line);padding:10px}.bubble{align-self:flex-start;background:#edf1f7;border-radius:14px;padding:9px 12px;max-width:85%;white-space:pre-wrap;overflow-wrap:anywhere}.bubble.mine{align-self:flex-end;background:var(--brand);color:#fff}.bubble time{display:block;color:inherit;opacity:.7}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.center{text-align:center}.file-row{font-size:13px;background:var(--soft);padding:8px;border-radius:8px}.error{background:#fff0f0;color:#8e2323;padding:12px;border-radius:10px}.footer{border-top:1px solid var(--line);color:#77849a;font-size:12px;padding:20px 4px;margin-top:30px}.badge{display:inline-flex;align-items:center;justify-content:center;min-width:19px;height:19px;padding:0 5px;margin-left:3px;border-radius:20px;background:#ccf2ee;color:var(--brand-dark);font-size:11px;font-weight:800}.comment{padding:10px 12px;background:#f6f8fc;border-radius:12px;margin:8px 0}.comment p{margin:3px 0;color:#29364c;white-space:pre-wrap;overflow-wrap:anywhere}.comment-form{display:flex;gap:8px;align-items:flex-end}.comment-form textarea{min-height:44px}.comment-form button{flex:none}.metric-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:15px 0}.metric{padding:15px;border:1px solid var(--line);border-radius:14px;background:linear-gradient(145deg,#fff,#f0f8f7)}.metric b{display:block;font-size:25px;letter-spacing:-.5px}.metric span{font-size:12px;color:var(--muted)}.traffic-chart{height:190px;display:flex;align-items:stretch;gap:5px;padding:12px 4px 0;border-bottom:1px solid var(--line)}.traffic-bar{flex:1;display:flex;min-width:0;flex-direction:column;justify-content:flex-end;align-items:center;gap:5px}.traffic-bar i{display:block;width:100%;min-height:3px;background:linear-gradient(180deg,#2dd4bf,var(--brand));border-radius:5px 5px 2px 2px}.traffic-bar small{font-size:9px}.scroll-panel{max-height:420px;overflow:auto}.table-row{display:flex;justify-content:space-between;gap:12px;padding:9px 2px;border-bottom:1px solid #edf0f4}.table-row:last-child{border:0}.quad-hero{background:linear-gradient(135deg,#0f766e,#14b8a6);color:#fff;border:0}.quad-hero h2{color:#fff;font-size:24px;margin:6px 0}.quad-hero p{color:#d7f5f1}.quad-hero .eyebrow{color:#a7f3d0}.story-row{display:flex;gap:12px;overflow-x:auto;padding:6px 2px 10px}.story{flex:0 0 auto;width:92px;text-align:center;text-decoration:none;color:inherit}.story .avatar{border:3px solid #fff;box-shadow:0 4px 14px #0f766e33;border-radius:16px;width:64px!important;height:64px!important}.story span{display:block;font-size:12px;font-weight:700;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.music-bar{display:flex;align-items:center;gap:10px;background:#f0f7f6;border:1px solid var(--line);border-radius:999px;padding:8px 12px;margin-top:10px}.music-bar button{flex:none;padding:7px 12px;font-size:13px}.shield{background:#f0fdfa;border:1px solid #99f6e4;border-radius:14px;padding:14px;margin:12px 0}
.note-bubble{display:block;font-size:11px;font-weight:700;background:#dcfce7;color:#14532d;border-radius:999px;padding:4px 8px;margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spotify-note{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:14px;padding:12px;margin:10px 0}
.spotify-embed{width:100%;height:80px;border:0;border-radius:12px;margin-top:8px}
.now-listening{margin-top:6px}
.reaction-bar{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.reaction-bar button{flex:1;background:transparent;color:#596a81;border:1px solid var(--line);border-radius:999px;padding:6px 8px;font-size:13px}.reaction-bar button.on{background:#e6f4f2;color:var(--brand-dark);border-color:#99f6e4}.thread-reply{margin-left:28px;border-left:3px solid var(--line);padding-left:10px}.tag-chip{display:inline-block;background:#e6f4f2;color:var(--brand-dark);border-radius:999px;padding:4px 10px;margin:2px 4px 2px 0;font-weight:700;font-size:13px}.repost-quote{border-left:3px solid var(--brand);background:var(--soft);border-radius:12px;padding:10px 12px;margin:10px 0}.poll-box{border:1px solid var(--line);border-radius:14px;padding:12px;margin:10px 0}.poll-bar{height:8px;background:#e5e7eb;border-radius:999px;overflow:hidden;margin:4px 0 10px}.poll-bar i{display:block;height:100%;background:var(--brand)}
.auth-wrap{display:grid;grid-template-columns:1.05fr .95fr;gap:22px;align-items:stretch;margin-top:8px}.auth-hero{background:linear-gradient(135deg,#0f766e,#14b8a6);color:#fff;border:0;padding:30px;display:flex;flex-direction:column;justify-content:center}.auth-hero h1{color:#fff;font-size:34px;margin:10px 0 12px}.auth-hero p{color:#d7f5f1}.auth-hero ul{list-style:none;margin:18px 0 0;padding:0;display:grid;gap:10px}.auth-hero li{font-weight:650;color:#fff}.auth-hero li span{color:#a7f3d0;margin-right:8px}.auth-card{padding:28px}.auth-card h2{font-size:24px}.auth-switch{text-align:center;margin:16px 0 0}.auth-switch a{font-weight:700}.password-wrap{position:relative}.password-wrap input{padding-right:64px}.password-toggle{position:absolute;right:6px;top:7px;background:transparent;color:var(--brand-dark);padding:6px 10px;font-size:12px;border-radius:999px}.password-toggle:hover{background:var(--soft);transform:none}.pw-meter{height:8px;background:#e5e7eb;border-radius:999px;overflow:hidden;margin-top:8px}.pw-meter i{display:block;height:100%;width:0;background:var(--brand);transition:width .2s}.pw-hint{font-size:12px;color:var(--muted);margin-top:6px}.auth-note{margin-top:14px}.brand-dot{color:var(--brand)}.rail-h2{margin-top:18px}.shield-h2{margin-top:14px}.confirm-email{width:220px;max-width:100%}.av30{width:30px;height:30px;font-size:13px}.av32{width:32px;height:32px;font-size:14px}.av34{width:34px;height:34px;font-size:14px}.av42{width:42px;height:42px;font-size:18px}.av54{width:54px;height:54px;font-size:20px}.av64{width:64px;height:64px;font-size:22px}.av80{width:80px;height:80px;font-size:26px}.av90{width:90px;height:90px;font-size:28px}.tb3{height:3%}.tb5{height:5%}.tb10{height:10%}.tb15{height:15%}.tb20{height:20%}.tb25{height:25%}.tb30{height:30%}.tb35{height:35%}.tb40{height:40%}.tb45{height:45%}.tb50{height:50%}.tb55{height:55%}.tb60{height:60%}.tb65{height:65%}.tb70{height:70%}.tb75{height:75%}.tb80{height:80%}.tb85{height:85%}.tb90{height:90%}.tb95{height:95%}.tb100{height:100%}
@media(max-width:400px){header{padding:8px 10px}.brand{font-size:19px}nav a{font-size:11px;padding:5px 7px}main{padding:0 8px}.card{padding:12px;border-radius:14px}h1{font-size:22px}.auth-hero{padding:16px}.auth-hero h1{font-size:22px}.auth-card{padding:16px}.metric-grid{grid-template-columns:1fr}.metric b{font-size:19px}.traffic-chart{height:120px}.traffic-bar small{display:none}.story{width:64px}.story .avatar{width:48px!important;height:48px!important}.message-list{height:300px}.comment-form{flex-wrap:wrap}.table-row{flex-direction:column;align-items:flex-start;gap:6px}.confirm-email{width:100%}.music-bar{padding:6px 10px}.music-bar button{padding:6px 10px;font-size:12px}}
@media(max-width:1000px){.layout{grid-template-columns:135px minmax(0,1fr)}.rail{position:static;grid-column:2}}@media(max-width:650px){header{position:relative;padding:10px 12px}.brand{font-size:21px}nav{gap:1px}nav a{font-size:12px;padding:6px 9px}main{margin:14px auto;padding:0 10px}.layout{display:block}.side{position:static;display:flex;overflow:auto;gap:4px;margin-bottom:10px}.side a{white-space:nowrap;font-size:13px;padding:8px 12px}.card{padding:14px;border-radius:16px}.grid{grid-template-columns:1fr}h1{font-size:25px}.person{flex-wrap:wrap}.person form{margin-left:4px}.comment-form{align-items:stretch}.comment-form button{padding:8px}.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.metric b{font-size:21px}.traffic-chart{gap:2px;height:150px}.traffic-bar small{font-size:8px}.auth-wrap{grid-template-columns:1fr}.auth-hero{padding:20px}.auth-hero h1{font-size:26px}.auth-card{padding:20px}.table-row{flex-wrap:wrap}.table-row span{overflow-wrap:anywhere}.music-bar{flex-wrap:wrap;border-radius:16px}.story{width:76px}.story .avatar{width:56px!important;height:56px!important}}}
@media(prefers-color-scheme:dark){:root{--ink:#e8f2f1;--muted:#9aa7ba;--brand:#2dd4bf;--brand-dark:#5eead4;--accent:#14b8a6;--line:#293449;--surface:#151b26;--soft:#1b2a29;--shadow:0 10px 30px #0003}body{background:linear-gradient(180deg,#0d1514 0,#0d1118 280px)}header{background:#111722e6}nav a,.side a{color:#b8c4d8}nav a:hover,.side a:hover{background:#1d2f2d;color:#5eead4}input,textarea,select{background:#111722;color:var(--ink);border-color:#334057}.light{background:#253047;color:#d7e0ef}.light:hover{background:#303d58;color:#fff}.post{color:#dce4f2}.post img.media,.post video.media{background:#0d1118}.actions{border-color:var(--line)}.actions button{color:#aab6ca}.actions button:hover,.actions .liked{background:#1d2f2d;color:#5eead4}.notice{background:#292514;border-color:#514825}.unread{background:#142a28}.person{border-color:#293449}.friend-post{border-color:#293449}.message-list{border-color:#293449}.bubble{background:#252e3e}.file-row,.comment{background:#202838}.comment p{color:#d7deeb}.error{background:#3a1d22;color:#ffb6bd}.metric{background:linear-gradient(145deg,#171e2a,#121823)}.table-row{border-color:#293449}.rail .all-link{background:#1d2f2d}.brand{color:#eef3fb}.music-bar{background:#142a28}.shield{background:#0f2a27;border-color:#134e4a}.quad-hero{background:linear-gradient(135deg,#115e59,#0d9488)}}
'''


def shell(title, content, user=None, error=''):
    if user:
        with connect() as c:
            counts=c.execute("SELECT (SELECT COUNT(*) FROM friendships WHERE addressee_id=? AND status='pending') friends,(SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL) alerts,(SELECT COUNT(*) FROM messages WHERE recipient_id=? AND read_at IS NULL) messages",(user['id'],user['id'],user['id'])).fetchone()
            friend_count,alert_count,message_count=counts['friends'],counts['alerts'],counts['messages']
        items=[('/', 'Feed', 0),('/explore','Explore',0),('/groups','Groups',0),('/people','People',0),('/friends','Friends',friend_count),('/messages','Messages',message_count),('/notifications','Alerts',alert_count),('/profile','Profile',0),('/settings','Settings',0)]
        links = ''.join('<a href="%s">%s%s</a>' % (path,text,('<span class="badge">%s</span>'%count) if count else '') for path,text,count in items)
        nav = links + ('<a href="/admin">Admin</a>' if user['is_admin'] else '') + '<form class="inline" method="post" action="/logout"><input type="hidden" name="csrf" value="%s"><button class="light">Sign out</button></form>' % esc(user['csrf'])
        footer = 'Posts and profile information are visible to signed-in members. Keep sensitive details private.'
        ping = '<script src="/app.js" defer></script>'
    else:
        nav = '<a href="/login">Log in</a><a href="/register">Create account</a>'
        footer = 'The Quad · An independent project. Not affiliated with Facebook or Meta.'
        ping = ''
    err = '<div class="error">%s</div>' % esc(error) if error else ''
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>%s · The Quad</title><link rel="stylesheet" href="/app.css"></head><body><header><a class="brand" href="/">the quad<span class="brand-dot">.</span></a><nav>%s</nav></header><main>%s%s<footer class="footer">%s</footer></main>%s</body></html>' % (esc(title), nav, err, content, footer, ping)



def login_page(error='', email=''):
    content = ('<div class="auth-wrap"><section class="card auth-hero"><span class="eyebrow">The Quad</span>'
        '<h1>Started in a dorm room. Now it\'s the whole quad.</h1>'
        '<p>Your people, your posts, your music — all in one place.</p>'
        '<ul><li><span>●</span> Post, Nod and comment with your circle</li>'
        '<li><span>●</span> Message friends and share Quad Radio tracks</li>'
        '<li><span>●</span> Your account, your posts — delete what\'s yours</li></ul></section>'
        '<section class="card auth-card"><h2>Log in to The Quad</h2>'
        '<form method="post" action="/login" data-lock-submit="1">'
        '<label for="login-email">Email</label><input id="login-email" type="email" name="email" required maxlength="254" autocomplete="email" value="%s">'
        '<label for="login-password">Password</label><div class="password-wrap"><input id="login-password" type="password" name="password" required autocomplete="current-password"><button type="button" class="password-toggle" data-toggle-password="login-password">Show</button></div>'
        '<button class="btn" data-wait="Signing in…">Log in</button></form>'
        '<p class="auth-switch"><a href="/forgot-password">Forgot password?</a></p>'
        '<p class="auth-switch">New to The Quad? <a href="/register">Create an account</a></p>'
        '<p class="small auth-note">Sessions last up to 7 days and sign you out after 24 hours idle.</p></section></div>') % esc(email)
    return shell('Log in', content, error=error)


def register_page(error='', name='', email=''):
    content = ('<div class="auth-wrap"><section class="card auth-hero"><span class="eyebrow">The Quad</span>'
        '<h1>Join The Quad.</h1>'
        '<p>Your people, your posts, your music — all in one place.</p>'
        '<ul><li><span>●</span> Post, Nod and comment with your circle</li>'
        '<li><span>●</span> Message friends and share Quad Radio tracks</li>'
        '<li><span>●</span> Private accounts, blocks and your own admin-protected space</li></ul></section>'
        '<section class="card auth-card"><h2>Create your account</h2>'
        '<form method="post" action="/register" data-lock-submit="1">'
        '<label for="register-name">Your name</label><input id="register-name" name="name" required maxlength="80" autocomplete="name" value="%s">'
        '<label for="register-email">Email</label><input id="register-email" type="email" name="email" required maxlength="254" autocomplete="email" value="%s">'
        '<label for="register-password">Password</label><div class="password-wrap"><input id="register-password" type="password" name="password" required minlength="12" maxlength="256" autocomplete="new-password"><button type="button" class="password-toggle" data-toggle-password="register-password">Show</button></div>'
        '<div class="pw-meter"><i id="pw-meter-bar"></i></div><p class="pw-hint" id="pw-hint">Use 12+ characters, at least two kinds of characters, and not a common password — a phrase you can remember works well.</p>'
        '<label for="register-confirm">Confirm password</label><div class="password-wrap"><input id="register-confirm" type="password" name="confirm_password" required minlength="12" maxlength="256" autocomplete="new-password"><button type="button" class="password-toggle" data-toggle-password="register-confirm">Show</button></div>'
        '<button class="btn" data-wait="Creating account…">Create account</button></form>'
        '<p class="auth-switch">Already have an account? <a href="/login">Log in</a></p>'
        '<p class="small auth-note">Administrator access is restricted to configured owner accounts.</p></section></div>') % (esc(name), esc(email))
    return shell('Create account', content, error=error)


def change_password_page(user, error='', forced=False):
    notice = ''
    if forced:
        notice = '<p class="notice"><b>Your password needs updating.</b> It was set under an older password rule, or it is too weak under the current rule. Choose a new password (12+ characters, at least two kinds of characters, not a common password) to keep using The Quad. You cannot use the rest of the site until this is done.</p>'
    content = ('<section class="card"><h1>Change your password</h1>%s'
        '<form method="post" action="/change-password">%s'
        '<label for="current-password">Current password</label><input id="current-password" type="password" name="current_password" required autocomplete="current-password">'
        '<label for="new-password">New password</label><input id="new-password" type="password" name="password" required minlength="12" maxlength="256" autocomplete="new-password">'
        '<div class="pw-meter"><i id="pw-meter-bar"></i></div><p class="pw-hint" id="pw-hint">Use 12+ characters, at least two kinds of characters, and not a common password.</p>'
        '<label for="confirm-password">Confirm new password</label><input id="confirm-password" type="password" name="confirm_password" required minlength="12" maxlength="256" autocomplete="new-password">'
        '<button class="btn">Change password</button></form>'
        '<p class="small auth-note">If you did not expect this, sign out and use Forgot password instead.</p></section>') % (notice, form_csrf(user))
    return shell('Change password', content, user=user, error=error)


def form_csrf(user): return '<input type="hidden" name="csrf" value="%s">' % esc(user['csrf'])
# Notification kinds (Update 6). Preferences default to on; a stored
# enabled=0 suppresses that kind for that user. Push is NOT supported:
# it would need a service worker + VAPID + a push sender, which this
# single-file server does not have.
NOTIF_KINDS = ('like','comment','friend_request','friend_accept','message','repost','reaction','group_invite','event_invite','event_reminder','report_update')
REACTION_KINDS = ('nod','love','laugh','support')
REACTION_LABELS = {'nod':'Nod','love':'Love','laugh':'Laugh','support':'Support'}
REACTION_EMOJI = {'nod':'♥','love':'❤️','laugh':'😂','support':'🙌'}
HASHTAG_RE = re.compile(r'#([A-Za-z0-9_]{1,50})')
def extract_hashtags(body):
    return sorted({t.lower() for t in HASHTAG_RE.findall(body or '')})
def store_hashtags(c, post_id, body):
    for tag in extract_hashtags(body):
        c.execute('INSERT OR IGNORE INTO hashtags(tag,created) VALUES(?,?)',(tag,time.time()))
        hid=c.execute('SELECT id FROM hashtags WHERE tag=?',(tag,)).fetchone()['id']
        c.execute('INSERT OR IGNORE INTO post_hashtags(post_id,hashtag_id) VALUES(?,?)',(post_id,hid))
def render_body(body):
    """Escape post text, then link #hashtags to their tag pages."""
    out=esc(body or '')
    return HASHTAG_RE.sub(lambda m:'<a href="/tag/%s">#%s</a>'%(esc(m.group(1).lower()),esc(m.group(1))), out)
def notification(c, target, actor, kind, text, target_id=None):
    pref=c.execute('SELECT enabled FROM notification_prefs WHERE user_id=? AND kind=?',(target,kind)).fetchone()
    if pref is not None and not pref['enabled']:
        return
    c.execute('INSERT INTO notifications(id,user_id,actor_id,kind,target_id,body,created) VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,target,actor,kind,target_id,text,time.time()))
def can_view_repost(c, viewer_id, repost):
    """A repost inherits the original post's audience: if the viewer
    cannot see the original (private/blocked), the repost is hidden
    even if they are friends with the reposter."""
    if not repost['original_post_id']:
        return can_view_post(c, viewer_id, repost['user_id'])
    orig=c.execute('SELECT user_id FROM posts WHERE id=?',(repost['original_post_id'],)).fetchone()
    if not orig:
        return can_view_post(c, viewer_id, repost['user_id'])
    return can_view_post(c, viewer_id, orig['user_id']) and can_view_post(c, viewer_id, repost['user_id'])
def group_role(c, group_id, user_id):
    row=c.execute('SELECT role,status FROM group_members WHERE group_id=? AND user_id=?',(group_id,user_id)).fetchone()
    return row
def can_view_group(c, group_id, user_id):
    g=c.execute('SELECT * FROM groups WHERE id=?',(group_id,)).fetchone()
    if not g:
        return None
    if g['visibility']=='public':
        return g
    m=group_role(c, group_id, user_id)
    return g if (m and m['status']=='accepted') or g['owner_id']==user_id else None
def search_posts(c, viewer_id, query, limit=50):
    """Post search, privacy-filtered in Python. FTS5 first, LIKE fallback.
    Returns visible posts (newest first). Private/blocked posts never leak."""
    q=(query or '').strip()[:120]
    if not q:
        return []
    rows=[]
    try:
        phrase='"'+q.replace('"','')+'"'
        rows=c.execute('SELECT p.* FROM posts_fts f JOIN posts p ON p.id=f.rowid WHERE posts_fts MATCH ? ORDER BY p.created DESC LIMIT ?',(phrase,limit*3)).fetchall()
    except Exception:
        rows=[]
    if not rows:
        term='%'+escape_like(q)+'%'
        rows=c.execute('SELECT * FROM posts WHERE body LIKE ? ORDER BY created DESC LIMIT ?',(term,limit*3)).fetchall()
    out=[]
    for p in rows:
        if p['is_hidden']:
            continue
        if p['group_id']:
            if not can_view_group(c, p['group_id'], viewer_id):
                continue
        elif not can_view_repost(c, viewer_id, p):
            continue
        out.append(p)
        if len(out)>=limit:
            break
    return out
def trending_posts(c, viewer_id, limit=20):
    """Simple time-decayed score: (nods*3 + comments*2) / (1 + age_hours/24).
    Computed over recent visible posts; no ranking service involved."""
    now=time.time()
    rows=c.execute('''SELECT p.*,(SELECT COUNT(*) FROM likes l WHERE l.post_id=p.id) likes,(SELECT COUNT(*) FROM comments cm WHERE cm.post_id=p.id) comments FROM posts p WHERE p.created>? AND p.is_hidden=0 AND p.group_id IS NULL ORDER BY p.created DESC LIMIT 200''',(now-14*86400,)).fetchall()
    scored=[]
    for p in rows:
        if not can_view_repost(c, viewer_id, p):
            continue
        age_h=max(0,(now-p['created'])/3600)
        score=(p['likes']*3+p['comments']*2)/(1+age_h/24.0)
        scored.append((score,p))
    scored.sort(key=lambda x:-x[0])
    return scored[:limit]
def relationship(c, a, b):
    return c.execute('SELECT * FROM friendships WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)',(a,b,b,a)).fetchone()
def are_friends(c,a,b):
    r=relationship(c,a,b)
    return bool(r and r['status']=='accepted')
def can_view_post(c,viewer_id,owner_id):
    if viewer_id==owner_id:return True
    blocked=c.execute('SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)',(viewer_id,owner_id,owner_id,viewer_id)).fetchone()
    if blocked:return False
    prefs=c.execute('SELECT private_account FROM user_settings WHERE user_id=?',(owner_id,)).fetchone()
    return not (prefs and prefs['private_account']) or are_friends(c,viewer_id,owner_id)
def user_by_id(c, uid):
    return c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
def valid_file(data, content_type):
    signatures={'image/jpeg':data.startswith(b'\xff\xd8\xff'),'image/png':data.startswith(b'\x89PNG\r\n\x1a\n'),'image/gif':data[:6] in (b'GIF87a',b'GIF89a'),'image/webp':data[:4]==b'RIFF' and data[8:12]==b'WEBP','video/mp4':len(data)>=12 and data[4:8]==b'ftyp','video/webm':data.startswith(b'\x1a\x45\xdf\xa3')}
    return signatures.get(content_type,False)


def can_view_upload(c, viewer_id, key):
    upload=c.execute('SELECT user_id FROM uploads WHERE key=?',(key,)).fetchone()
    if not upload:return False
    if upload['user_id']==viewer_id:return True
    # Avatars are visible to signed-in members unless either person has blocked the other.
    avatar_owner=c.execute('SELECT id FROM users WHERE avatar_key=?',(key,)).fetchone()
    if avatar_owner:
        blocked=c.execute('SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)',(viewer_id,avatar_owner['id'],avatar_owner['id'],viewer_id)).fetchone()
        return not bool(blocked)
    post=c.execute('SELECT user_id FROM posts WHERE media_key=?',(key,)).fetchone()
    return bool(post and can_view_post(c,viewer_id,post['user_id']))


def verified_video_duration(path):
    # Server-side duration validation; Docker image installs ffprobe.
    exe=shutil.which('ffprobe')
    if not exe:return None
    try:
        result=subprocess.run([exe,'-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],capture_output=True,text=True,timeout=8,check=True)
        value=float(result.stdout.strip())
        return value if value>0 else None
    except Exception:
        return None


def send_reset_email(recipient, code):
    """Send one recovery code using standard SMTP environment settings."""
    host=os.environ.get('SMTP_HOST','').strip()
    sender=os.environ.get('SMTP_FROM','').strip()
    if not host or not sender:return False
    try:
        port=int(os.environ.get('SMTP_PORT','587'))
        timeout=10
        message=EmailMessage()
        message['Subject']='Your The Quad password reset code'
        message['From']=sender
        message['To']=recipient
        message.set_content('Your The Quad password reset code is: %s\n\nIt expires in 10 minutes and can be used once. If you did not request it, ignore this email.'%code)
        use_ssl=os.environ.get('SMTP_USE_SSL','').strip().lower() in ('1','true','yes')
        client=smtplib.SMTP_SSL(host,port,timeout=timeout,context=ssl.create_default_context()) if use_ssl else smtplib.SMTP(host,port,timeout=timeout)
        with client:
            if not use_ssl:
                client.ehlo();client.starttls(context=ssl.create_default_context());client.ehlo()
            username=os.environ.get('SMTP_USERNAME','');password=os.environ.get('SMTP_PASSWORD','')
            if username:client.login(username,password)
            refused=client.send_message(message)
        return recipient not in refused
    except Exception as exc:
        print('Password reset email delivery failed:',type(exc).__name__)
        return False


def deliver_reset_email_async(user_id, recipient, code):
    """Keep slow SMTP connections from holding the password-reset page open."""
    def deliver():
        if not send_reset_email(recipient,code):
            with connect() as c:c.execute('DELETE FROM password_reset_codes WHERE user_id=?',(user_id,))
    worker=threading.Thread(target=deliver,name='password-reset-email',daemon=True)
    worker.start()


REGISTRATION_NOTICE = ('<section class="card"><h1>Thanks — check your next step</h1>'
    '<p>If this email is new here, your account is ready. If you already have an account with this email, no new account was created.</p>'
    '<p><a class="btn" href="/">Continue to The Quad</a></p>'
    '<p>Try <a href="/login">signing in</a>, or use <a href="/forgot-password">Forgot password</a> to reset it.</p></section>')


class Handler(BaseHTTPRequestHandler):
    server_version='TheQuad'
    sys_version=''
    def setup(self):
        super().setup()
        self.connection.settimeout(30)
    def version_string(self): return self.server_version
    def log_message(self, fmt, *args):
        # Do not persist visitor IP addresses in application logs.
        print(fmt % args)
    def security_headers(self, cache_control='no-store'):
        self.send_header('Cache-Control',cache_control)
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=(), payment=()')
        self.send_header('Cross-Origin-Opener-Policy','same-origin')
        self.send_header('Cross-Origin-Resource-Policy','same-origin')
        self.send_header('Content-Security-Policy',"default-src 'self'; base-uri 'none'; object-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; media-src 'self'; frame-src https://open.spotify.com; frame-ancestors 'none'; form-action 'self'")
        if PRODUCTION:self.send_header('Strict-Transport-Security','max-age=31536000; includeSubDomains')
    def html(self, text, status=200, cookie=None):
        body=text.encode('utf-8');self.send_response(status);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.security_headers()
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers();self.wfile.write(body)
    def go(self,path='/',cookie=None):
        self.send_response(303);self.send_header('Location',path);self.security_headers()
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers()
    def client_fingerprint(self):
        addr=self.client_address[0] if self.client_address else 'unknown'
        if TRUST_PROXY:
            forwarded=(self.headers.get('X-Forwarded-For','').split(',')[-1].strip())
            if forwarded and len(forwarded)<=64:addr=forwarded
        return rate_key('client:'+addr)
    def same_origin_request(self):
        site=self.headers.get('Sec-Fetch-Site','').strip().lower()
        if site=='cross-site':return False
        origin=self.headers.get('Origin','').strip()
        if origin:
            try:
                o=urllib.parse.urlparse(origin)
                host=self.headers.get('Host','').split(',',1)[0].strip().lower()
                if o.netloc.lower()!=host:return False
            except Exception:return False
        return True
    def session(self):
        token=''
        for p in self.headers.get('Cookie','').split(';'):
            k,_,v=p.strip().partition('=')
            if k=='network_session':token=v
        if not token:return None,None
        now=time.time()
        with connect() as c:
            row=c.execute('SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND s.last_seen>?',(token,now,now-SESSION_IDLE_TIMEOUT)).fetchone()
            if not row:c.execute('DELETE FROM sessions WHERE token=?',(token,))
        return row,token
    def read_form(self):
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError: raise ValueError('Invalid request size.')
        if length<0 or length>MAX_BODY: raise ValueError('Upload too large. Videos max 25 MB; images max 8 MB.')
        raw=self.rfile.read(length); ctype=self.headers.get('Content-Type','')
        if ctype.lower().startswith('multipart/form-data'):
            msg=BytesParser(policy=default).parsebytes(('Content-Type: %s\r\nMIME-Version: 1.0\r\n\r\n'%ctype).encode()+raw)
            fields={};files={}
            for part in msg.iter_parts():
                name=part.get_param('name',header='content-disposition')
                filename=part.get_filename(); data=part.get_payload(decode=True) or b''
                if filename is not None: files[name]=(filename,data,part.get_content_type().lower())
                elif name: fields[name]=data.decode('utf-8','replace')
            return fields,files
        parsed=urllib.parse.parse_qs(raw.decode('utf-8','replace'),keep_blank_values=True)
        return {k:v[-1] for k,v in parsed.items()},{}
    def authenticated(self,user,data):
        return bool(user and secrets.compare_digest(data.get('csrf',''),user['csrf']))
    def secure_cookie(self):
        proto=self.headers.get('X-Forwarded-Proto','').split(',',1)[0].strip().lower()
        return 'Secure; ' if (PRODUCTION or proto=='https') else ''
    def touch(self,user,token):
        now=time.time()
        with connect() as c:c.execute('UPDATE users SET last_seen=? WHERE id=?',(now,user['id']));c.execute('UPDATE sessions SET last_seen=? WHERE token=?',(now,token))
    def do_GET(self):
        path=urllib.parse.urlparse(self.path).path
        if path in ('/health','/healthz'):
            try:
                with connect() as c:c.execute('SELECT 1').fetchone()
                if not os.access(DATA_DIR,os.W_OK):raise OSError('data directory not writable')
                body=b'ok';status=200
            except Exception:
                body=b'unhealthy';status=503
            self.send_response(status);self.send_header('Content-Type','text/plain; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.security_headers();self.end_headers();self.wfile.write(body);return
        user,token=self.session()
        if path not in ('/app.js','/app.css','/activity.js','/favicon.ico') and not path.startswith('/media/'):
            # Count actual pages, not polling requests or media fetches. Do not collect IPs.
            # Logging itself is throttled (20/min per client) so a flood of GETs
            # cannot fill the database with page_views rows; admin traffic
            # figures are therefore approximate, not exact counts.
            now=time.time()
            log_view = rate_limit('view-log', self.client_fingerprint(), 20, 60)
            with connect() as c:
                if log_view:
                    c.execute('INSERT INTO page_views(user_id,path,created) VALUES(?,?,?)',(user['id'] if user else None,path[:120],now))
                if user:
                    c.execute('UPDATE users SET last_seen=? WHERE id=?',(now,user['id']))
                    c.execute('UPDATE sessions SET last_seen=? WHERE token=?',(now,token))
        if user and user['must_change_password'] and path not in ('/change-password','/logout','/app.js','/app.css','/activity.js','/favicon.ico','/health','/healthz'):
            return self.html(change_password_page(user, forced=True))
        if path == '/app.css':
            body = CSS.encode('utf-8'); self.send_response(200); self.send_header('Content-Type', 'text/css; charset=utf-8'); self.send_header('Content-Length', str(len(body))); self.security_headers('private, max-age=300'); self.end_headers(); self.wfile.write(body); return
        if path == '/change-password':
            if not user: return self.html(login_page('Sign in first, then change your password.'))
            return self.html(change_password_page(user, forced=bool(user['must_change_password'])))
        if path in ('/app.js','/activity.js'):
            js="""(()=>{const csrf=()=>document.querySelector('[name=csrf]')?.value||'';const ping=()=>{if(document.visibilityState==='visible'&&csrf())fetch('/activity',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'csrf='+encodeURIComponent(csrf())}).catch(()=>{});};if(csrf()){ping();setInterval(ping,30000);document.addEventListener('visibilitychange',ping)}document.addEventListener('submit',e=>{const f=e.target;if(!(f instanceof HTMLFormElement))return;const msg=f.dataset.confirm;if(msg&&!confirm(msg)){e.preventDefault();return}if(f.dataset.videoForm==='1'&&!f.dataset.videoChecked){const file=f.querySelector('input[type=file][name=media]')?.files?.[0];if(file&&file.type.startsWith('video/')){e.preventDefault();const v=document.createElement('video');v.preload='metadata';v.onloadedmetadata=()=>{URL.revokeObjectURL(v.src);if(!v.duration||v.duration>30){alert('Videos must be 30 seconds or shorter.');return}const d=f.querySelector('[name=duration_ms]');if(d)d.value=Math.round(v.duration*1000);f.dataset.videoChecked='1';f.requestSubmit()};v.onerror=()=>alert('Could not read this video.');v.src=URL.createObjectURL(file);return}}if(f.dataset.lockSubmit==='1'){const b=f.querySelector('button[type=submit],button:not([type])');if(b){b.disabled=true;b.textContent=b.dataset.wait||'Working…'}}})
(()=>{const TRACKS={'quad-dawn':{label:'Quad Radio · Dawn Loop',notes:[[523.25,.5],[587.33,.5],[659.25,.5],[783.99,1],[659.25,.5],[587.33,1]]},'quad-night':{label:'Quad Radio · Night Loop',notes:[[440,.5],[523.25,.5],[415.3,.5],[392,1],[329.63,.5],[440,1]]},'quad-quad':{label:'Quad Radio · Quad Anthem',notes:[[659.25,.25],[783.99,.25],[880,.5],[783.99,.25],[659.25,.25],[587.33,1]]}};let ctx=null,playing=null,timers=[];const stop=()=>{timers.forEach(clearTimeout);timers=[];if(ctx){ctx.close().catch(()=>{});ctx=null}if(playing){playing.textContent=playing.dataset.label||'▶ Play';playing=null}};const play=(name,btn)=>{if(playing===btn){stop();return}stop();const t=TRACKS[name];if(!t)return;ctx=new (window.AudioContext||window.webkitAudioContext)();playing=btn;btn.dataset.label=btn.textContent;btn.textContent='■ Stop';let when=ctx.currentTime+.05;t.notes.forEach(([f,d])=>{const o=ctx.createOscillator(),g=ctx.createGain();o.type='triangle';o.frequency.value=f;g.gain.setValueAtTime(0,when);g.gain.linearRampToValueAtTime(.12,when+.03);g.gain.exponentialRampToValueAtTime(.001,when+d*.9);o.connect(g);g.connect(ctx.destination);o.start(when);o.stop(when+d);when+=d});timers.push(setTimeout(stop,Math.max(1000,(when-ctx.currentTime)*1000)))};document.addEventListener('click',e=>{const b=e.target.closest('[data-quad-track]');if(!b)return;e.preventDefault();play(b.dataset.quadTrack,b)});})();(()=>{document.addEventListener('click',e=>{const b=e.target.closest('[data-toggle-password]');if(!b)return;const f=document.getElementById(b.dataset.togglePassword);if(!f)return;const show=f.type==='password';f.type=show?'text':'password';b.textContent=show?'Hide':'Show';f.focus()});const pw=document.getElementById('register-password')||document.getElementById('new-password');const bar=document.getElementById('pw-meter-bar');const hint=document.getElementById('pw-hint');if(pw&&bar){const upd=()=>{const v=pw.value;let s=0;if(v.length>=12)s+=40;else s+=Math.min(40,v.length*3);if(/[a-z]/.test(v)&&/[A-Z]/.test(v))s+=20;if(/\\d/.test(v))s+=20;if(/[^A-Za-z0-9]/.test(v))s+=20;bar.style.width=Math.min(100,s)+'%';if(hint)hint.textContent=v.length<12?'Use 12+ characters — a phrase you can remember works well.':s<60?'Good — longer or more varied is stronger.':s<90?'Strong password.':'Great — that looks strong.'};pw.addEventListener('input',upd);upd()}})();
})();"""
            body=js.encode('utf-8');self.send_response(200);self.send_header('Content-Type','application/javascript; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.security_headers('private, max-age=300');self.end_headers();self.wfile.write(body);return
        if path.startswith('/media/'):
            if not user:self.html('Sign in required.',401);return
            key=urllib.parse.unquote(path[len('/media/'):])
            if not key or '/' in key or '..' in key:self.html('Not found.',404);return
            with connect() as c:
                row=c.execute('SELECT content_type FROM uploads WHERE key=?',(key,)).fetchone()
                allowed=bool(row and can_view_upload(c,user['id'],key))
            target=UPLOAD_DIR/key
            if not row or not allowed or not target.is_file():self.html('Not found.',404);return
            size=target.stat().st_size;self.send_response(200);self.send_header('Content-Type',row['content_type']);self.send_header('Content-Length',str(size));self.send_header('Content-Disposition','inline');self.security_headers('private, max-age=3600');self.end_headers()
            with target.open('rb') as media_file:
                while True:
                    chunk=media_file.read(65536)
                    if not chunk:break
                    self.wfile.write(chunk)
            return
        if path=='/login':
            if user:return self.go('/')
            return self.html(login_page())
        if path=='/register':
            if user:return self.go('/')
            return self.html(register_page())
        if path=='/forgot-password':return self.forgot_password_page()
        if not user:
            content='''<div class="auth-wrap"><section class="card auth-hero"><span class="eyebrow">The Quad</span><h1>Started in a dorm room. Now it\'s the whole quad.</h1><p>Your people, your posts, your music — all in one place.</p><ul><li><span>●</span> Post, Nod and comment with your circle</li><li><span>●</span> Message friends and share Quad Radio tracks</li><li><span>●</span> Your account, your posts — delete what\'s yours</li></ul></section><section class="card auth-card"><h2>Welcome to The Quad</h2><p>Log in to your account, or create one to join.</p><a class="btn" href="/login">Log in</a> <a class="btn light" href="/register">Create account</a><p class="auth-switch"><a href="/forgot-password">Forgot password?</a></p></section></div>'''
            self.html(shell('Welcome',content));return
        # last_seen was updated alongside page-view logging above.
        if path=='/':
            query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:page=max(0,min(1000,int(query.get('page',['0'])[0])))
            except ValueError:page=0
            return self.feed(user,query.get('sort',['newest'])[0],page,query.get('feed',['all'])[0])
        if path=='/explore':return self.explore(user)
        if path=='/search':return self.search_page(user,urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('q',[''])[0])
        if path=='/groups':return self.groups_page(user)
        if path.startswith('/group/'):
            try:gid=int(path.split('/')[-1])
            except ValueError:return self.html(shell('Group','<section class="card">Group not found.</section>',user),404)
            return self.group_page(user,gid)
        if path=='/events':return self.events_page(user)
        if path=='/saved':return self.saved_page(user)
        if path.startswith('/tag/'):
            return self.tag_page(user,urllib.parse.unquote(path[len('/tag/'):]))
        if path=='/admin/moderation':return self.moderation_page(user)
        if path=='/export-data':return self.export_data(user)
        if path=='/profile':return self.profile(user)
        if path=='/notes/refresh':return self.listening_refresh(user)
        if path=='/people':return self.people(user,urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('q',[''])[0])
        if path=='/friends':return self.friends(user)
        if path=='/settings':return self.settings(user)
        if path=='/messages':return self.messages(user,urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('with',[''])[0])
        if path=='/notifications':return self.notifications(user)
        if path.startswith('/post/'):
            try:pid=int(path.split('/')[-1])
            except ValueError:return self.html(shell('Post','<section class="card">Post not found.</section>',user),404)
            return self.post_detail(user,pid)
        if path=='/admin':return self.admin(user)
        if path.startswith('/member/'):return self.public_profile(user,path.split('/')[-1])
        self.html(shell('Not found','<section class="card"><h1>Page not found</h1><a href="/">Back to feed</a></section>',user),404)
    def forgot_password_page(self,message=''):
        notice='<p class="notice">%s</p>'%esc(message) if message else ''
        content='<section class="card"><h1>Forgot password?</h1><p>We will email a six-digit code if this address belongs to an account and email delivery is configured.</p>%s<form method="post" action="/send-reset-code"><input type="hidden" name="reset_token" value="%s"><label>Email</label><input type="email" name="email" required maxlength="254" autocomplete="email"><button class="btn">Email me a code</button></form><p class="small"><a href="/">Back to sign in</a></p></section>'%(notice, reset_token('send-reset'))
        return self.html(shell('Reset password',content))
    def reset_password_form(self,email,message=''):
        content = ('<section class="card"><h1>Enter your code</h1><p>Check your email. The code expires in 10 minutes. You have up to five attempts.</p>'
            '<p class="notice">%s</p>'
            '<form method="post" action="/reset-password"><input type="hidden" name="reset_token" value="%s">'
            '<label>Email</label><input type="email" name="email" value="%s" required maxlength="254" autocomplete="email">'
            '<label>Six-digit code</label><input name="code" inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" required autocomplete="one-time-code">'
            '<label>New password (12+ characters)</label><input type="password" name="password" required minlength="12" maxlength="256" autocomplete="new-password">'
            '<label>Confirm new password</label><input type="password" name="confirm_password" required minlength="12" maxlength="256" autocomplete="new-password">'
            '<button class="btn">Reset password</button></form>'
            '<form method="post" action="/send-reset-code"><input type="hidden" name="reset_token" value="%s">'
            '<input type="hidden" name="email" value="%s"><button class="light">Send another code</button></form></section>'
            ) % (esc(message), reset_token('reset-password'), esc(email), reset_token('send-reset'), esc(email))
        return self.html(shell('Reset password',content))
    def feed(self,user,sort='newest',page=0,feed_mode='all'):
        sort=sort if sort in ('newest','oldest','liked') else 'newest'
        feed_mode=feed_mode if feed_mode in ('all','following') else 'all'
        page=max(0,min(1000,int(page)));page_size=20
        order={'newest':'p.created DESC,p.id DESC','oldest':'p.created ASC,p.id ASC','liked':'likes DESC,p.created DESC,p.id DESC'}[sort]
        with connect() as c:
            posts=c.execute('''SELECT p.*,u.name,u.display_name,u.avatar_key,(SELECT COUNT(*) FROM likes l WHERE l.post_id=p.id) likes,(SELECT COUNT(*) FROM likes l WHERE l.post_id=p.id AND l.user_id=?) liked,(SELECT COUNT(*) FROM comments cm WHERE cm.post_id=p.id) comments FROM posts p JOIN users u ON u.id=p.user_id WHERE p.is_hidden=0 AND p.group_id IS NULL AND (p.user_id=? OR COALESCE((SELECT private_account FROM user_settings s WHERE s.user_id=p.user_id),0)=0 OR EXISTS (SELECT 1 FROM friendships f WHERE f.status='accepted' AND ((f.requester_id=p.user_id AND f.addressee_id=?) OR (f.addressee_id=p.user_id AND f.requester_id=?)))) AND NOT EXISTS (SELECT 1 FROM blocks b WHERE (b.blocker_id=? AND b.blocked_id=p.user_id) OR (b.blocker_id=p.user_id AND b.blocked_id=?)) ORDER BY '''+order+''' LIMIT 21 OFFSET ?''',(user['id'],user['id'],user['id'],user['id'],user['id'],user['id'],page*page_size)).fetchall()
            # Privacy inheritance for reposts + following-only filter, in Python.
            visible=[]
            for p in posts:
                if not can_view_repost(c,user['id'],p):
                    continue
                if feed_mode=='following' and p['user_id']!=user['id'] and not are_friends(c,user['id'],p['user_id']):
                    continue
                visible.append(p)
            raw_full=len(posts)>page_size
            posts=visible[:page_size];has_more=raw_full or len(visible)>page_size
            # Reaction counts by type, and the viewer's own reaction, per post.
            react_counts={};my_react={}
            if posts:
                ids=','.join(str(p['id']) for p in posts)
                for r in c.execute('SELECT post_id,kind,COUNT(*) n FROM reactions WHERE post_id IN (%s) GROUP BY post_id,kind'%ids).fetchall():
                    react_counts.setdefault(r['post_id'],{})[r['kind']]=r['n']
                for r in c.execute('SELECT post_id,kind FROM reactions WHERE post_id IN (%s) AND user_id=?'%(ids),(user['id'],)).fetchall():
                    my_react[r['post_id']]=r['kind']
                saved_ids={r['post_id'] for r in c.execute('SELECT post_id FROM saved_posts WHERE user_id=? AND post_id IN (%s)'%(ids),(user['id'],)).fetchall()}
            else:
                saved_ids=set()
            friends=c.execute("SELECT u.* FROM friendships f JOIN users u ON u.id=CASE WHEN f.requester_id=? THEN f.addressee_id ELSE f.requester_id END WHERE (f.requester_id=? OR f.addressee_id=?) AND f.status='accepted' ORDER BY COALESCE(u.display_name,u.name) LIMIT 6",(user['id'],user['id'],user['id'])).fetchall()
            friend_posts=c.execute("SELECT p.id,p.user_id,p.body,p.created,p.media_key,p.media_type,u.name,u.display_name,u.avatar_key FROM posts p JOIN users u ON u.id=p.user_id WHERE p.is_hidden=0 AND p.user_id IN (SELECT CASE WHEN f.requester_id=? THEN f.addressee_id ELSE f.requester_id END FROM friendships f WHERE (f.requester_id=? OR f.addressee_id=?) AND f.status='accepted') AND NOT EXISTS (SELECT 1 FROM blocks b WHERE (b.blocker_id=? AND b.blocked_id=p.user_id) OR (b.blocker_id=p.user_id AND b.blocked_id=?)) ORDER BY p.created DESC,p.id DESC LIMIT 4",(user['id'],user['id'],user['id'],user['id'],user['id'])).fetchall()
            incoming=c.execute("SELECT f.requester_id AS user_id,u.* FROM friendships f JOIN users u ON u.id=f.requester_id WHERE f.addressee_id=? AND f.status='pending' ORDER BY f.created DESC",(user['id'],)).fetchall()
            stories=c.execute("SELECT u.*,MAX(p.created) mc FROM users u JOIN posts p ON p.user_id=u.id WHERE u.id=? OR u.id IN (SELECT CASE WHEN f.requester_id=? THEN f.addressee_id ELSE f.requester_id END FROM friendships f WHERE (f.requester_id=? OR f.addressee_id=?) AND f.status='accepted') GROUP BY u.id ORDER BY mc DESC LIMIT 8",(user['id'],user['id'],user['id'],user['id'])).fetchall()
            note_rows=c.execute("SELECT * FROM listening_notes WHERE expires_at>? AND (user_id=? OR user_id IN (SELECT CASE WHEN f.requester_id=? THEN f.addressee_id ELSE f.requester_id END FROM friendships f WHERE (f.requester_id=? OR f.addressee_id=?) AND f.status='accepted'))",(time.time(),user['id'],user['id'],user['id'],user['id'])).fetchall()
            notes_by_user={r['user_id']:r for r in note_rows}
            own_note=notes_by_user.get(user['id'])
        content='<section class="card quad-hero"><span class="eyebrow">The Quad</span><h2>Started in a dorm room. Now it\'s the whole quad.</h2><p>Your people, your posts, your music — all in one place on The Quad.</p></section><h1>Welcome, %s</h1>'%esc(display(user))
        if stories:
            content+='<section class="card"><h2>Quad moments</h2><div class="story-row">'
            for s in stories:
                snote=notes_by_user.get(s['id'])
                bubble='<span class="note-bubble">♫ %s</span>' % esc(snote['track_title']) if snote else ''
                content+='<a class="story" href="/member/%s">%s%s<span>%s</span></a>'%(s['id'],bubble,avatar(s,64),esc(display(s)))
            content+='</div></section>'
        else:
            content+='<section class="card"><h2>Quad moments</h2><p class="small">No moments yet — share the first post and it will show here.</p></section>'
        if incoming:
            content+='<section class="card notice"><h2>Friend requests · %s</h2><p>These people want to connect with you.</p>'%len(incoming)
            for m in incoming:
                content+='<div class="person">%s<b class="grow">%s</b><form method="post" action="/friend">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="accept"><button>Accept</button></form><form method="post" action="/friend">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="decline"><button class="light">Decline</button></form></div>'%(avatar(m),esc(display(m)),form_csrf(user),m['user_id'],form_csrf(user),m['user_id'])
            content+='</section>'
        content+=listening_note_form(user, own_note)
        content+='<section class="card"><h2>Share a post</h2><form method="post" action="/posts" enctype="multipart/form-data" data-video-form="1">%s<textarea name="body" maxlength="2000" placeholder="What’s on your mind? #hashtags welcome"></textarea><label>Add a photo or video (videos up to 30 seconds)</label><input type="file" name="media" accept="image/jpeg,image/png,image/gif,image/webp,video/mp4,video/webm"><label>Attach Quad Radio music (optional) — built-in synth loops, not real songs</label><select name="music_track"><option value="">No music</option><option value="quad-dawn">Quad Radio · Dawn Loop</option><option value="quad-night">Quad Radio · Night Loop</option><option value="quad-quad">Quad Radio · Quad Anthem</option></select><label>Attach a Spotify track (optional) — paste a Spotify track link to show a link and preview player on your post</label><input name="spotify_link" maxlength="300" placeholder="https://open.spotify.com/track/..."><div class="music-bar"><span class="small grow">Preview Quad Radio:</span><button type="button" data-quad-track="quad-dawn">▶ Dawn</button><button type="button" data-quad-track="quad-night">▶ Night</button><button type="button" data-quad-track="quad-quad">▶ Anthem</button></div><input type="hidden" name="duration_ms" value=""><button class="btn">Share post</button></form></section><div class="row"><h2 class="grow">Your feed</h2><form method="get"><select name="feed"><option value="all" %s>Everyone</option><option value="following" %s>Friends only</option></select><select name="sort"><option value="newest" %s>Newest</option><option value="oldest" %s>Oldest</option><option value="liked" %s>Most nodded</option></select><button class="light">Sort</button></form></div><p class="small"><a href="/explore">Explore trending &amp; hashtags</a> · <a href="/saved">Saved posts</a></p>'%(form_csrf(user),'selected' if feed_mode=='all' else '','selected' if feed_mode=='following' else '','selected' if sort=='newest' else '','selected' if sort=='oldest' else '','selected' if sort=='liked' else '')
        if not posts and page==0: content+='<section class="card">No posts yet. Find people or write the first post.</section>'
        for p in posts:
            content+='<article class="card"><div class="row">%s<div class="grow"><a href="/member/%s"><b>%s</b></a><time>%s</time></div>'%(avatar(p),p['user_id'],esc(display(p)),esc(date(p['created'])))
            if p['user_id']==user['id'] or user['is_admin']:content+='<form method="post" action="/delete" data-confirm="Remove this post?">%s<input type="hidden" name="post_id" value="%s"><button class="danger">Remove</button></form>'%(form_csrf(user),p['id'])
            content+='</div>'
            if p['original_post_id']:
                with connect() as _c:
                    _orig=_c.execute('SELECT p.body,u.name,u.display_name FROM posts p JOIN users u ON u.id=p.user_id WHERE p.id=?',(p['original_post_id'],)).fetchone()
                if _orig:
                    content+='<div class="repost-quote"><b>Reposted from %s</b><div class="post">%s</div></div>'%(esc(display(_orig)),render_body(_orig['body']))
                else:
                    content+='<div class="repost-quote"><b>Reposted post</b><p class="small">The original post is no longer available.</p></div>'
            if p['body']:content+='<div class="post">%s</div>'%render_body(p['body'])
            if p['media_key']:
                tag='video' if (p['media_type'] or '').startswith('video/') else 'img'
                if tag=='video':content+='<video class="media" controls playsinline preload="none" src="/media/%s"></video>'%urllib.parse.quote(p['media_key'])
                else:content+='<img class="media" loading="lazy" decoding="async" src="/media/%s" alt="Post photo">'%urllib.parse.quote(p['media_key'])
            if p['music_track'] and p['music_track'] in MUSIC_TRACKS:content+='<div class="music-bar"><button type="button" data-quad-track="%s">▶ Play</button><span class="small grow">%s · built-in loop</span></div>'%(esc(p['music_track']),esc(MUSIC_TRACKS[p['music_track']]))
            if p['spotify_track_id']:content+='<div class="spotify-note"><a href="https://open.spotify.com/track/%s" target="_blank" rel="noopener">♫ Listen on Spotify</a>%s</div>'%(esc(p['spotify_track_id']),spotify_embed_html(p['spotify_track_id']))
            _rc=react_counts.get(p['id'],{})
            _my=my_react.get(p['id'])
            content+='<div class="reaction-bar">'
            for _k in REACTION_KINDS:
                _n=_rc.get(_k,0)
                if _k=='nod':
                    _n+=p['likes']
                content+='<form method="post" action="/react">%s<input type="hidden" name="post_id" value="%s"><input type="hidden" name="kind" value="%s"><button class="%s">%s %s%s</button></form>'%(form_csrf(user),p['id'],_k,'on' if _my==_k or (_k=='nod' and p['liked']) else '',REACTION_EMOJI[_k],REACTION_LABELS[_k],' %s'%_n if _n else '')
            content+='</div>'
            content+='<p class="muted">%s nod(s) · <a href="/post/%s">%s comments</a></p><div class="actions"><a class="btn light" href="/post/%s">Comment</a><form method="post" action="/repost">%s<input type="hidden" name="post_id" value="%s"><input name="comment" maxlength="500" placeholder="Add a comment (optional)"><button class="light">Repost</button></form><form method="post" action="/save">%s<input type="hidden" name="post_id" value="%s"><button class="light">%s</button></form>%s</div></article>'%(p['likes'],p['id'],p['comments'],p['id'],form_csrf(user),p['id'],form_csrf(user),p['id'],'Saved ✓' if p['id'] in saved_ids else 'Save',('<form method="post" action="/pin">%s<input type="hidden" name="post_id" value="%s"><button class="light">%s</button></form>'%(form_csrf(user),p['id'],'Unpin' if p['is_pinned'] else 'Pin') if p['user_id']==user['id'] else ''))
        if page or has_more:
            content+='<div class="row">'
            if page:content+='<a class="btn light" href="/?sort=%s&amp;feed=%s&amp;page=%s">← Newer posts</a>'%(sort,feed_mode,max(0,page-1))
            if has_more:content+='<a class="btn grow" href="/?sort=%s&amp;feed=%s&amp;page=%s">Older posts →</a>'%(sort,feed_mode,page+1)
            content+='</div>'
        friends_panel='<aside class="rail card"><h2>Friends</h2>'
        if friends:
            for friend in friends:friends_panel+='<div class="friend-row">%s<a href="/member/%s">%s</a></div>'%(avatar(friend,32),friend['id'],esc(display(friend)))
            friends_panel+='<a class="all-link" href="/friends">See all friends</a>'
        else:friends_panel+='<p class="small">No friends yet. Find people to connect.</p><a class="all-link" href="/people">Find people</a>'
        friends_panel+='<h2 class="rail-h2">Friends’ posts</h2>'
        if friend_posts:
            for fp in friend_posts:
                preview=(fp['body'] or ('Shared a photo' if (fp['media_type'] or '').startswith('image/') else 'Shared a video'))
                if len(preview)>105:preview=preview[:102].rstrip()+'…'
                friends_panel+='<div class="friend-post"><div class="row">%s<a href="/member/%s">%s</a></div><p class="small">%s · %s</p></div>'%(avatar(fp,30),fp['user_id'],esc(display(fp)),esc(preview),esc(date(fp['created'])))
        else:friends_panel+='<p class="small">Your friends’ new posts will show here.</p>'
        friends_panel+='<h2 class="rail-h2">Now listening</h2><div class="now-listening">'
        friend_notes=[notes_by_user[f['id']] for f in friends if f['id'] in notes_by_user]
        if friend_notes:
            for n in friend_notes:
                owner_row=next((f for f in friends if f['id']==n['user_id']), None)
                friends_panel+='<div class="friend-post"><div class="row">%s<a href="/member/%s">%s</a></div>%s</div>'%(avatar(owner_row,30) if owner_row else '',n['user_id'],esc(display(owner_row)) if owner_row else 'Friend',spotify_note_html(n, embed=False))
        else:
            friends_panel+='<p class="small">No friends are sharing a Listening Note right now. Set yours from the feed — it lasts 24 hours.</p>'
        friends_panel+='</div>'
        layout='<div class="layout"><aside class="side"><a href="/">⌂ Feed</a><a href="/people">♧ People</a><a href="/friends">♧ Friends</a><a href="/messages">✉ Messages</a><a href="/notifications">♢ Notifications</a><a href="/profile">◉ Profile</a></aside><section>%s</section>%s</div>'%(content,friends_panel+'</aside>')
        return self.html(shell('Feed',layout,user))
    def poll_html(self,user,post_id):
        with connect() as c:
            poll=c.execute('SELECT * FROM polls WHERE post_id=?',(post_id,)).fetchone()
            if not poll:
                return ''
            opts=c.execute('SELECT o.*, (SELECT COUNT(*) FROM poll_votes v WHERE v.option_id=o.id) votes FROM poll_options o WHERE o.poll_id=? ORDER BY o.id',(poll['id'],)).fetchall()
            total=sum(o['votes'] for o in opts)
            my=c.execute('SELECT option_id FROM poll_votes WHERE poll_id=? AND user_id=?',(poll['id'],user['id'])).fetchone()
        out='<div class="poll-box"><b>Poll: %s</b>'%(esc(poll['question']))
        if my:
            out+='<br><small>by the post author · %s vote(s)</small>'%total
        if my:
            for o in opts:
                pct=round(o['votes']/total*100) if total else 0
                out+='<div>%s%s — %s vote(s) (%s%%)</div><div class="poll-bar"><i style="width:%s%%"></i></div>'%('✓ ' if my['option_id']==o['id'] else '',esc(o['text']),o['votes'],pct,pct)
            out+='<form method="post" action="/poll-vote">%s<input type="hidden" name="poll_id" value="%s"><label>Change your vote</label><select name="option_id">%s</select><button class="light">Change vote</button></form>'%(form_csrf(user),poll['id'],''.join('<option value="%s">%s</option>'%(o['id'],esc(o['text'])) for o in opts))
        else:
            out+='<form method="post" action="/poll-vote">%s<input type="hidden" name="poll_id" value="%s">'%(form_csrf(user),poll['id'])
            for o in opts:
                out+='<label class="row"><input type="radio" name="option_id" value="%s" required> %s</label>'%(o['id'],esc(o['text']))
            out+='<button class="btn">Vote</button></form><p class="small">Results show after you vote.</p>'
        out+='</div>'
        return out
    def post_card(self,user,p):
        with connect() as c:
            owner=user_by_id(c,p['user_id'])
            likes=c.execute('SELECT COUNT(*) FROM likes WHERE post_id=?',(p['id'],)).fetchone()[0]
        out='<article class="card"><div class="row">%s<div class="grow"><a href="/member/%s"><b>%s</b></a><time>%s</time></div></div>'%(avatar(owner),p['user_id'],esc(display(owner)),esc(date(p['created'])))
        if p['original_post_id']:
            with connect() as _c:
                _orig=_c.execute('SELECT p.body,u.name,u.display_name FROM posts p JOIN users u ON u.id=p.user_id WHERE p.id=?',(p['original_post_id'],)).fetchone()
            if _orig:
                out+='<div class="repost-quote"><b>Reposted from %s</b><div class="post">%s</div></div>'%(esc(display(_orig)),render_body(_orig['body']))
        if p['body']:out+='<div class="post">%s</div>'%render_body(p['body'])
        if p['media_key']:
            if (p['media_type'] or '').startswith('video/'):out+='<video class="media" controls playsinline src="/media/%s"></video>'%urllib.parse.quote(p['media_key'])
            else:out+='<img class="media" loading="lazy" src="/media/%s" alt="Post photo">'%urllib.parse.quote(p['media_key'])
        if p['spotify_track_id']:out+='<div class="spotify-note"><a href="https://open.spotify.com/track/%s" target="_blank" rel="noopener">♫ Listen on Spotify</a>%s</div>'%(esc(p['spotify_track_id']),spotify_embed_html(p['spotify_track_id']))
        out+=self.poll_html(user,p['id'])
        out+='<p class="muted">%s nod(s)</p><p><a class="btn light" href="/post/%s">Open post</a></p></article>'%(likes,p['id'])
        return out
    def explore(self,user):
        with connect() as c:
            trending=trending_posts(c,user['id'],20)
            tags=c.execute('''SELECT h.tag,COUNT(*) n FROM post_hashtags ph JOIN hashtags h ON h.id=ph.hashtag_id JOIN posts p ON p.id=ph.post_id WHERE p.created>? GROUP BY h.tag ORDER BY n DESC LIMIT 10''',(time.time()-7*86400,)).fetchall()
            suggested=c.execute('''SELECT u.* FROM users u WHERE u.id<>? AND u.id NOT IN (SELECT CASE WHEN f.requester_id=? THEN f.addressee_id ELSE f.requester_id END FROM friendships f WHERE (f.requester_id=? OR f.addressee_id=?) AND f.status='accepted') AND NOT EXISTS (SELECT 1 FROM blocks b WHERE (b.blocker_id=? AND b.blocked_id=u.id) OR (b.blocker_id=u.id AND b.blocked_id=?)) ORDER BY u.last_seen DESC LIMIT 6''',(user['id'],user['id'],user['id'],user['id'],user['id'],user['id'])).fetchall()
            new_groups=c.execute('SELECT * FROM groups ORDER BY created DESC LIMIT 6').fetchall()
        content='<section class="card quad-hero"><span class="eyebrow">Explore</span><h2>Around the Quad</h2><p>What is trending, what people are talking about, and who is new here.</p></section>'
        content+='<section class="card"><h2>Search posts &amp; people</h2><form method="get" action="/search"><input name="q" placeholder="Search posts, hashtags, people"><button class="btn">Search</button></form><p class="small">Search respects privacy — private posts never show to non-friends.</p></section>'
        content+='<section class="card"><h2>Trending hashtags</h2>'
        if tags:
            for t in tags:content+='<a class="tag-chip" href="/tag/%s">#%s · %s</a>'%(esc(t['tag']),esc(t['tag']),t['n'])
        else:content+='<p class="small">No hashtags yet — be the first to add one to a post.</p>'
        content+='</section><section class="card"><h2>Suggested people</h2>'
        if suggested:
            for m in suggested:content+='<div class="person">%s<a class="grow" href="/member/%s"><b>%s</b></a><form method="post" action="/friend">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="request"><button>Add friend</button></form></div>'%(avatar(m),m['id'],esc(display(m)),form_csrf(user),m['id'])
        else:content+='<p class="small">No suggestions right now.</p>'
        content+='</section><section class="card"><h2>New groups</h2>'
        if new_groups:
            for g in new_groups:content+='<div class="person"><div class="grow"><a href="/group/%s"><b>%s</b></a><p class="small">%s · %s</p></div></div>'%(g['id'],esc(g['name']),esc(g['visibility']),esc(g['description'][:120]))
        else:content+='<p class="small">No groups yet. <a href="/groups">Create the first one</a>.</p>'
        content+='</section><h2>Trending posts</h2>'
        if not trending:content+='<section class="card">Nothing trending yet — post something and get the conversation going.</section>'
        for _score,p in trending:content+=self.post_card(user,p)
        return self.html(shell('Explore',content,user))
    def search_page(self,user,q=''):
        q=(q or '').strip()[:120]
        content='<section class="card"><h1>Search</h1><form method="get" action="/search"><input name="q" value="%s" placeholder="Search posts, hashtags, people"><button class="btn">Search</button></form></section>'%esc(q)
        if q:
            with connect() as c:
                posts=search_posts(c,user['id'],q,50)
                term='%'+escape_like(q)+'%'
                people=c.execute("SELECT * FROM users WHERE id<>? AND (name LIKE ? OR COALESCE(display_name,'') LIKE ?) AND NOT EXISTS (SELECT 1 FROM blocks b WHERE (b.blocker_id=? AND b.blocked_id=id) OR (b.blocker_id=id AND b.blocked_id=?)) ORDER BY last_seen DESC LIMIT 20",(user['id'],term,term,user['id'],user['id'])).fetchall()
            content+='<h2>Posts matching “%s” · %s</h2>'%(esc(q),len(posts))
            if not posts:content+='<section class="card">No posts matched that search.</section>'
            for p in posts:content+=self.post_card(user,p)
            content+='<h2>People matching “%s”</h2>'%esc(q)
            if not people:content+='<section class="card">No people matched that search.</section>'
            for m in people:content+='<div class="person">%s<a class="grow" href="/member/%s"><b>%s</b></a></div>'%(avatar(m),m['id'],esc(display(m)))
        return self.html(shell('Search',content,user))
    def tag_page(self,user,tag=''):
        tag=(tag or '').strip().lower()[:50]
        with connect() as c:
            rows=c.execute('''SELECT p.* FROM post_hashtags ph JOIN hashtags h ON h.id=ph.hashtag_id JOIN posts p ON p.id=ph.post_id WHERE h.tag=? AND p.is_hidden=0 AND p.group_id IS NULL ORDER BY p.created DESC LIMIT 50''',(tag,)).fetchall()
            posts=[p for p in rows if can_view_repost(c,user['id'],p)]
        content='<section class="card"><h1>#%s</h1><p>%s post(s) with this hashtag.</p><p><a href="/explore">← Back to Explore</a></p></section>'%(esc(tag),len(posts))
        if not posts:content+='<section class="card">No posts with this hashtag yet.</section>'
        for p in posts:content+=self.post_card(user,p)
        return self.html(shell('#'+tag,content,user))
    def saved_page(self,user):
        with connect() as c:
            rows=c.execute('''SELECT p.* FROM saved_posts s JOIN posts p ON p.id=s.post_id WHERE s.user_id=? AND p.is_hidden=0 ORDER BY s.created DESC LIMIT 50''',(user['id'],)).fetchall()
            posts=[p for p in rows if can_view_repost(c,user['id'],p)]
        content='<section class="card"><h1>Saved posts</h1><p>Posts you saved for later.</p></section>'
        if not posts:content+='<section class="card">Nothing saved yet. Tap Save on a post to keep it here.</section>'
        for p in posts:content+=self.post_card(user,p)
        return self.html(shell('Saved posts',content,user))
    def groups_page(self,user):
        with connect() as c:
            groups=c.execute('SELECT g.*,(SELECT COUNT(*) FROM group_members m WHERE m.group_id=g.id AND m.status=\'accepted\') members FROM groups g ORDER BY g.created DESC').fetchall()
            mine={r['group_id']:r for r in c.execute('SELECT * FROM group_members WHERE user_id=?',(user['id'],)).fetchall()}
        content='<section class="card"><h1>Groups</h1><p>A group is a Quad — a place for a specific circle of people. Public groups are open to everyone; private groups need a member to approve you.</p></section>'
        content+='<section class="card"><h2>Create a group</h2><form method="post" action="/group">%s<label>Group name</label><input name="name" required maxlength="80" placeholder="e.g. Dorm 4B"><label>Description</label><textarea name="description" maxlength="500" placeholder="What is this group for?"></textarea><label>Visibility</label><select name="visibility"><option value="public">Public — anyone can join and see posts</option><option value="private">Private — members only, join by request</option></select><button class="btn">Create group</button></form></section>'%form_csrf(user)
        for g in groups:
            if g['visibility']=='private' and g['id'] not in mine and g['owner_id']!=user['id']:
                content+='<article class="card"><h2>%s 🔒</h2><p class="small">Private group · %s member(s)</p><p>This group is private. You need to be a member to see its posts.</p></article>'%(esc(g['name']),g['members'])
                continue
            mem=mine.get(g['id'])
            btn=''
            if mem and mem['status']=='accepted':btn='<a class="btn light" href="/group/%s">Open group</a>'%g['id']
            elif mem and mem['status']=='pending':btn='<span class="muted">Join request pending</span>'
            elif g['visibility']=='public':btn='<form method="post" action="/group-action">%s<input type="hidden" name="group_id" value="%s"><input type="hidden" name="action" value="join"><button>Join group</button></form>'%(form_csrf(user),g['id'])
            else:btn='<form method="post" action="/group-action">%s<input type="hidden" name="group_id" value="%s"><input type="hidden" name="action" value="request"><button>Request to join</button></form>'%(form_csrf(user),g['id'])
            content+='<article class="card"><h2><a href="/group/%s">%s</a></h2><p>%s</p><p class="small">%s · %s member(s) · created %s</p>%s</article>'%(g['id'],esc(g['name']),esc(g['description']),esc(g['visibility']),g['members'],esc(date(g['created'])),btn)
        if not groups:content+='<section class="card">No groups yet — create the first one above.</section>'
        return self.html(shell('Groups',content,user))
    def group_page(self,user,gid):
        with connect() as c:
            g=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone()
            if not g:return self.html(shell('Group','<section class="card">Group not found.</section>',user),404)
            mem=group_role(c,gid,user['id'])
            is_member=bool(mem and mem['status']=='accepted') or g['owner_id']==user['id']
            if g['visibility']=='private' and not is_member:
                content='<section class="card"><h1>%s 🔒</h1><p>This group is private.</p>'%esc(g['name'])
                if mem and mem['status']=='pending':content+='<p class="muted">Your join request is pending.</p>'
                else:content+='<form method="post" action="/group-action">%s<input type="hidden" name="group_id" value="%s"><input type="hidden" name="action" value="request"><button>Request to join</button></form>'%(form_csrf(user),gid)
                return self.html(shell('Group',content+'</section>',user))
            members=c.execute('SELECT u.*,m.role FROM group_members m JOIN users u ON u.id=m.user_id WHERE m.group_id=? AND m.status=\'accepted\' ORDER BY m.role,m.created',(gid,)).fetchall()
            pending=c.execute('SELECT u.*,m.created FROM group_members m JOIN users u ON u.id=m.user_id WHERE m.group_id=? AND m.status=\'pending\' ORDER BY m.created',(gid,)).fetchall()
            posts=c.execute('SELECT * FROM posts WHERE group_id=? AND is_hidden=0 ORDER BY created DESC LIMIT 30',(gid,)).fetchall()
            events=c.execute('SELECT * FROM events WHERE group_id=? AND starts_at>? ORDER BY starts_at LIMIT 10',(gid,time.time())).fetchall()
            owner=user_by_id(c,g['owner_id'])
        is_admin=is_member and (g['owner_id']==user['id'] or (mem and mem['role'] in ('owner','admin')))
        content='<section class="card"><h1>%s</h1><p>%s</p><p class="small">%s group · %s member(s) · owner %s</p>'%(esc(g['name']),esc(g['description']),esc(g['visibility']),len(members),esc(display(owner)))
        if is_member:
            content+='<form method="post" action="/group-action">%s<input type="hidden" name="group_id" value="%s"><input type="hidden" name="action" value="leave"><button class="light">Leave group</button></form>'%(form_csrf(user),gid)
        content+='</section>'
        if is_admin and pending:
            content+='<section class="card"><h2>Join requests</h2>'
            for m in pending:
                content+='<div class="person">%s<b class="grow">%s</b><form method="post" action="/group-action">%s<input type="hidden" name="group_id" value="%s"><input type="hidden" name="action" value="approve"><input type="hidden" name="user_id" value="%s"><button>Approve</button></form><form method="post" action="/group-action">%s<input type="hidden" name="group_id" value="%s"><input type="hidden" name="action" value="reject"><input type="hidden" name="user_id" value="%s"><button class="light">Reject</button></form></div>'%(avatar(m),esc(display(m)),form_csrf(user),gid,m['id'],form_csrf(user),gid,m['id'])
            content+='</section>'
        content+='<section class="card"><h2>Members</h2>'
        for m in members:
            role_btn=''
            if is_admin and m['id']!=g['owner_id'] and m['id']!=user['id']:
                role_btn='<form method="post" action="/group-action">%s<input type="hidden" name="group_id" value="%s"><input type="hidden" name="action" value="%s"><input type="hidden" name="user_id" value="%s"><button class="light">%s</button></form>'%(form_csrf(user),gid,'demote' if m['role']=='admin' else 'promote',m['id'],'Demote to member' if m['role']=='admin' else 'Make admin')
            content+='<div class="person">%s<b class="grow">%s · %s</b>%s</div>'%(avatar(m),esc(display(m)),esc(m['role']),role_btn)
        content+='</section>'
        if events:
            content+='<section class="card"><h2>Upcoming events</h2>'
            for e in events:content+='<div class="person"><div class="grow"><b>%s</b><p class="small">%s · %s</p></div><a class="btn light" href="/events">RSVP</a></div>'%(esc(e['title']),esc(date(e['starts_at'])),esc(e['location']))
            content+='</section>'
        if is_member:
            content+='<section class="card"><h2>Post in this group</h2><form method="post" action="/group-post" enctype="multipart/form-data">%s<input type="hidden" name="group_id" value="%s"><textarea name="body" maxlength="2000" required placeholder="Share something with %s"></textarea><button class="btn">Post to group</button></form><p class="small">Group posts only show inside this group — never on the main feed.</p></section>'%(form_csrf(user),gid,esc(g['name']))
        content+='<h2>Group discussion</h2>'
        if not posts:content+='<section class="card">No posts in this group yet.</section>'
        for p in posts:content+=self.post_card(user,p)
        return self.html(shell('Group',content,user))
    def events_page(self,user):
        with connect() as c:
            events=c.execute('SELECT e.*,u.name,u.display_name FROM events e JOIN users u ON u.id=e.user_id WHERE e.starts_at>? ORDER BY e.starts_at LIMIT 30',(time.time(),)).fetchall()
            groups=c.execute('SELECT * FROM groups ORDER BY name').fetchall()
            friends=c.execute("SELECT u.* FROM friendships f JOIN users u ON u.id=CASE WHEN f.requester_id=? THEN f.addressee_id ELSE f.requester_id END WHERE (f.requester_id=? OR f.addressee_id=?) AND f.status='accepted' ORDER BY u.display_name,u.name",(user['id'],user['id'],user['id'])).fetchall()
            rsvps={r['event_id']:r['status'] for r in c.execute('SELECT * FROM event_rsvps WHERE user_id=?',(user['id'],)).fetchall()}
        content='<section class="card"><h1>Events</h1><p>Upcoming events, with RSVPs and invitations to friends.</p></section>'
        content+='<section class="card"><h2>Create an event</h2><form method="post" action="/event">%s<label>Title</label><input name="title" required maxlength="120"><label>Description</label><textarea name="description" maxlength="500"></textarea><label>When</label><input type="datetime-local" name="starts_at" required><label>Where</label><input name="location" maxlength="120" placeholder="e.g. The Quad, Dorm 4B"><label>Group (optional)</label><select name="group_id"><option value="">No group — open event</option>%s</select><button class="btn">Create event</button></form></section>'%(form_csrf(user),''.join('<option value="%s">%s</option>'%(g['id'],esc(g['name'])) for g in groups))
        for e in events:
            my=rsvps.get(e['id'],'')
            content+='<article class="card"><h2>%s</h2><p>%s</p><p class="small">%s · %s · by %s</p><p class="small">Your RSVP: <b>%s</b></p><form method="post" action="/event-rsvp">%s<input type="hidden" name="event_id" value="%s"><select name="status"><option value="going" %s>Going</option><option value="maybe" %s>Maybe</option><option value="not_going" %s>Not going</option></select><button class="light">Save RSVP</button></form>'%(esc(e['title']),esc(e['description']),esc(date(e['starts_at'])),esc(e['location']),esc(display(e)),esc(my or 'not set'),form_csrf(user),e['id'],'selected' if my=='going' else '','selected' if my=='maybe' else '','selected' if my=='not_going' else '')
            if friends:
                content+='<form method="post" action="/event-invite">%s<input type="hidden" name="event_id" value="%s"><select name="friend_id">%s</select><button class="light">Invite friend</button></form>'%(form_csrf(user),e['id'],''.join('<option value="%s">%s</option>'%(f['id'],esc(display(f))) for f in friends))
            content+='</article>'
        if not events:content+='<section class="card">No upcoming events — create the first one above.</section>'
        return self.html(shell('Events',content,user))
    def moderation_page(self,user):
        if not user['is_admin']:return self.html(shell('Moderation','<section class="card"><h1>Admin access only</h1></section>',user),403)
        with connect() as c:
            reports=c.execute('SELECT r.*,u.name,u.display_name FROM reports r JOIN users u ON u.id=r.reporter_id ORDER BY r.created DESC LIMIT 100').fetchall()
        content='<section class="card"><h1>Moderation queue</h1><p>Reports from members, newest first. Hiding removes a post from feeds and search; removing deletes it; warning notifies the person who posted it. Every action is audit-logged and the reporter is notified of the outcome.</p></section>'
        if not reports:content+='<section class="card">No reports. The queue is clear.</section>'
        for r in reports:
            content+='<article class="card"><h2>Report #%s · %s · %s</h2><p><b>Reason:</b> %s</p><p>%s</p><p class="small">Reported by %s · %s · status: <b>%s</b></p>'%(r['id'],esc(r['target_type']),esc(r['target_id']),esc(r['reason']),esc(r['details']),esc(display(r)),esc(date(r['created'])),esc(r['status']))
            if r['status']=='open':
                content+='<form method="post" action="/admin/report-action">%s<input type="hidden" name="report_id" value="%s"><select name="action"><option value="hide">Hide target</option><option value="remove">Remove target</option><option value="warn">Warn the person</option><option value="dismiss">Dismiss (no violation)</option></select><button class="danger">Take action</button></form>'%(form_csrf(user),r['id'])
            content+='</article>'
        return self.html(shell('Moderation',content,user))
    def export_data(self,user):
        with connect() as c:
            u=user_by_id(c,user['id'])
            data={
                'exported_at':time.time(),'user':{'id':u['id'],'name':u['name'],'display_name':u['display_name'],'email':u['email'],'bio':u['bio'],'interests':u['interests'],'joined':u['joined']},
                'posts':[dict(r) for r in c.execute('SELECT * FROM posts WHERE user_id=? ORDER BY created',(user['id'],)).fetchall()],
                'comments':[dict(r) for r in c.execute('SELECT * FROM comments WHERE user_id=? ORDER BY created',(user['id'],)).fetchall()],
                'messages':[dict(r) for r in c.execute('SELECT * FROM messages WHERE sender_id=? OR recipient_id=? ORDER BY created',(user['id'],user['id'])).fetchall()],
                'likes':[dict(r) for r in c.execute('SELECT * FROM likes WHERE user_id=?',(user['id'],)).fetchall()],
                'reactions':[dict(r) for r in c.execute('SELECT * FROM reactions WHERE user_id=?',(user['id'],)).fetchall()],
                'saved_posts':[dict(r) for r in c.execute('SELECT * FROM saved_posts WHERE user_id=?',(user['id'],)).fetchall()],
                'listening_notes':[dict(r) for r in c.execute('SELECT * FROM listening_notes WHERE user_id=?',(user['id'],)).fetchall()],
                'groups':[dict(r) for r in c.execute('SELECT g.* FROM groups g JOIN group_members m ON m.group_id=g.id WHERE m.user_id=?',(user['id'],)).fetchall()],
            }
        body=json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8')
        self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Disposition','attachment; filename="the-quad-export.json"');self.send_header('Content-Length',str(len(body)));self.security_headers();self.end_headers();self.wfile.write(body)
    def profile(self,user):
        with connect() as c:
            u=user_by_id(c,user['id'])
            my_note=active_listening_note(c,user['id'])
        content='<section class="card"><h1>Your profile</h1><div class="row">%s <b>%s</b></div><form method="post" action="/profile" enctype="multipart/form-data">%s<label>Display name</label><input name="display_name" value="%s" required maxlength="60"><label>Bio</label><textarea name="bio" maxlength="240">%s</textarea><label>Interests</label><input name="interests" value="%s" maxlength="240" placeholder="Music, hiking, cooking"><label>Profile photo (JPEG, PNG, GIF or WebP; 8 MB max)</label><input type="file" name="avatar" accept="image/jpeg,image/png,image/gif,image/webp"><button class="btn">Save profile</button></form></section>'%(avatar(u,80),esc(display(u)),form_csrf(user),esc(display(u)),esc(u['bio']),esc(u['interests']))
        content+=listening_note_form(user, my_note)
        return self.html(shell('Profile',content,user),cookie=None)
    def post_detail(self,user,pid):
        with connect() as c:
            p=c.execute('SELECT p.*,u.name,u.display_name,u.avatar_key,(SELECT COUNT(*) FROM likes l WHERE l.post_id=p.id) likes,(SELECT COUNT(*) FROM likes l WHERE l.post_id=p.id AND l.user_id=?) liked FROM posts p JOIN users u ON u.id=p.user_id WHERE p.id=?',(user['id'],pid)).fetchone()
            if not p:return self.html(shell('Post','<section class="card">Post not found.</section>',user),404)
            if not can_view_post(c,user['id'],p['user_id']):return self.html(shell('Post','<section class="card">This post is private or unavailable.</section>',user),403)
            comments=c.execute('SELECT cm.*,u.name,u.display_name,u.avatar_key FROM comments cm JOIN users u ON u.id=cm.user_id WHERE cm.post_id=? ORDER BY cm.created,cm.id',(pid,)).fetchall()
        content='<article class="card"><div class="row">%s<div class="grow"><a href="/member/%s"><b>%s</b></a><time>%s</time></div></div>'%(avatar(p),p['user_id'],esc(display(p)),esc(date(p['created'])))
        if p['body']:content+='<div class="post">%s</div>'%esc(p['body'])
        if p['media_key']:
            if (p['media_type'] or '').startswith('video/'):content+='<video class="media" controls playsinline src="/media/%s"></video>'%urllib.parse.quote(p['media_key'])
            else:content+='<img class="media" src="/media/%s" alt="Post photo">'%urllib.parse.quote(p['media_key'])
        if p['music_track'] and p['music_track'] in MUSIC_TRACKS:content+='<div class="music-bar"><button type="button" data-quad-track="%s">▶ Play</button><span class="small grow">%s · built-in loop</span></div>'%(esc(p['music_track']),esc(MUSIC_TRACKS[p['music_track']]))
        if p['spotify_track_id']:content+='<div class="spotify-note"><a href="https://open.spotify.com/track/%s" target="_blank" rel="noopener">♫ Listen on Spotify</a>%s</div>'%(esc(p['spotify_track_id']),spotify_embed_html(p['spotify_track_id']))
        content+='<p class="muted">%s nod(s) · %s comment(s)</p><div class="actions"><form method="post" action="/like">%s<input type="hidden" name="post_id" value="%s"><input type="hidden" name="action" value="%s"><button class="%s">%s %s</button></form></div></article>'%(p['likes'],len(comments),form_csrf(user),pid,'unlike' if p['liked'] else 'like','liked' if p['liked'] else '','♥' if p['liked'] else '♡','Nodded' if p['liked'] else 'Nod')
        content+=self.poll_html(user,pid)
        if p['user_id']==user['id']:
            with connect() as _c:
                _has_poll=_c.execute('SELECT 1 FROM polls WHERE post_id=?',(pid,)).fetchone()
            if not _has_poll:
                content+='<section class="card"><h2>Add a poll to this post</h2><form method="post" action="/poll">%s<input type="hidden" name="post_id" value="%s"><label>Question</label><input name="question" required maxlength="200" placeholder="What should we do?"><label>Options — one per line, 2–5</label><textarea name="options" required placeholder="Option one\nOption two"></textarea><button class="btn">Add poll</button></form></section>'%(form_csrf(user),pid)
        content+='<section class="card"><h2>Report this post</h2><form method="post" action="/report">%s<input type="hidden" name="target_type" value="post"><input type="hidden" name="target_id" value="%s"><label>Reason</label><input name="reason" required maxlength="80" placeholder="e.g. Spam, harassment, inappropriate"><label>Details (optional)</label><input name="details" maxlength="500"><button class="light">Report post</button></form></section>'%(form_csrf(user),pid)
        content+='<section class="card"><h2>Discussion</h2>'
        # Threaded replies, depth capped at 2: a reply's parent must be a
        # top-level comment. Render replies indented under their parent.
        tops=[cm for cm in comments if not cm['parent_id']]
        replies={}
        for cm in comments:
            if cm['parent_id']:
                replies.setdefault(cm['parent_id'],[]).append(cm)
        with connect() as _c:
            _cr={r['comment_id']:r for r in _c.execute('SELECT comment_id,kind,COUNT(*) n FROM comment_reactions WHERE comment_id IN (SELECT id FROM comments WHERE post_id=?) GROUP BY comment_id,kind',(pid,)).fetchall()} if comments else {}
            _my_cr={r['comment_id']:r['kind'] for r in _c.execute('SELECT comment_id,kind FROM comment_reactions WHERE user_id=? AND comment_id IN (SELECT id FROM comments WHERE post_id=?)',(user['id'],pid)).fetchall()} if comments else {}
        def _comment_html(cm, indented=False):
            _cls='comment thread-reply' if indented else 'comment'
            _h='<div class="%s"><div class="row">%s<a class="grow" href="/member/%s"><b>%s</b></a><time>%s</time>'%(_cls,avatar(cm,32),cm['user_id'],esc(display(cm)),esc(date(cm['created'])))
            if cm['user_id']==user['id'] or p['user_id']==user['id'] or user['is_admin']:
                _h+='<form method="post" action="/delete-comment">%s<input type="hidden" name="comment_id" value="%s"><button class="danger">Remove</button></form>'%(form_csrf(user),cm['id'])
            _h+='</div><p>%s</p>'%esc(cm['body'])
            _h+='<div class="reaction-bar">'
            for _k in REACTION_KINDS:
                _n=0
                _h+='<form method="post" action="/comment-react">%s<input type="hidden" name="comment_id" value="%s"><input type="hidden" name="kind" value="%s"><button class="%s">%s %s</button></form>'%(form_csrf(user),cm['id'],_k,'on' if _my_cr.get(cm['id'])==_k else '',REACTION_EMOJI[_k],REACTION_LABELS[_k])
            _h+='</div>'
            if cm['user_id']==user['id']:
                _h+='<form method="post" action="/edit-comment">%s<input type="hidden" name="comment_id" value="%s"><input name="body" value="%s" maxlength="1000" required><button class="light">Edit comment</button></form>'%(form_csrf(user),cm['id'],esc(cm['body']))
            if not indented:
                _h+='<form method="post" action="/comment">%s<input type="hidden" name="post_id" value="%s"><input type="hidden" name="parent_id" value="%s"><input name="body" required maxlength="1000" placeholder="Reply to %s"><button class="light">Reply</button></form>'%(form_csrf(user),pid,cm['id'],esc(display(cm)))
            _h+='</div>'
            return _h
        for cm in tops:
            content+=_comment_html(cm)
            for rep in replies.get(cm['id'],[]):
                content+=_comment_html(rep, indented=True)
        content+='<form class="comment-form" method="post" action="/comment">%s<input type="hidden" name="post_id" value="%s"><textarea name="body" required maxlength="1000" placeholder="Add to the conversation"></textarea><button>Comment</button></form></section><p><a href="/">← Back to feed</a></p>'%(form_csrf(user),pid)
        return self.html(shell('Discussion',content,user))
    def people(self,user,q=''):
        q=q.strip()[:80];term='%'+escape_like(q)+'%'
        with connect() as c:
            rows=c.execute("""SELECT u.*,COALESCE((SELECT private_account FROM user_settings s WHERE s.user_id=u.id),0) AS private_account
                FROM users u WHERE u.id<>?
                AND NOT EXISTS (SELECT 1 FROM blocks b WHERE (b.blocker_id=? AND b.blocked_id=u.id) OR (b.blocker_id=u.id AND b.blocked_id=?))
                AND (u.name LIKE ? OR COALESCE(u.display_name,'') LIKE ? OR
                    ((COALESCE((SELECT private_account FROM user_settings s2 WHERE s2.user_id=u.id),0)=0 OR EXISTS (
                        SELECT 1 FROM friendships f WHERE f.status='accepted' AND ((f.requester_id=u.id AND f.addressee_id=?) OR (f.addressee_id=u.id AND f.requester_id=?))))
                     AND (COALESCE(u.bio,'') LIKE ? OR COALESCE(u.interests,'') LIKE ?)))
                ORDER BY u.last_seen DESC LIMIT 60""",(user['id'],user['id'],user['id'],term,term,user['id'],user['id'],term,term)).fetchall()
            rels={r['id']:relationship(c,user['id'],r['id']) for r in rows}
        content='<section class="card"><h1>Find people · Your Circle</h1><p>Add friends by name — send a request, accept, or remove them here in The Quad.</p><form method="get"><input name="q" value="%s" placeholder="Search names, bios or interests"><button class="btn">Search</button></form></section>'%esc(q)
        for m in rows:
            r=rels[m['id']];visible=not bool(m['private_account']) or bool(r and r['status']=='accepted')
            btn='<form method="post" action="/friend">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="request"><button>Add friend</button></form>'%(form_csrf(user),m['id'])
            if r:
                if r['status']=='accepted': btn='<a class="btn light" href="/messages?with=%s">Message</a>'%m['id']
                elif r['addressee_id']==user['id']:
                    btn='<form method="post" action="/friend">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="accept"><button>Accept request</button></form>'%(form_csrf(user),m['id'])
                else:
                    btn='<span class="muted">Request sent</span><form method="post" action="/friend">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="cancel"><button class="light">Cancel</button></form>'%(form_csrf(user),m['id'])
            btn += '<form method="post" action="/block">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="block"><button class="light">Block</button></form>'%(form_csrf(user),m['id'])
            bio=m['bio'] if visible else 'Private account';interests=m['interests'] if visible else 'Hidden until you are friends'
            content+='<article class="card"><div class="row">%s<div class="grow"><a href="/member/%s"><b>%s</b></a><p>%s</p><small>Interests: %s</small></div>%s</div></article>'%(avatar(m,54),m['id'],esc(display(m)),esc(bio),esc(interests),btn)
        if not rows:content+='<section class="card">No members found.</section>'
        return self.html(shell('People',content,user))
    def friends(self,user):
        with connect() as c:
            incoming=c.execute("SELECT f.id,f.requester_id,u.* FROM friendships f JOIN users u ON u.id=f.requester_id WHERE f.addressee_id=? AND f.status='pending'",(user['id'],)).fetchall()
            friends=c.execute("SELECT u.* FROM friendships f JOIN users u ON u.id=CASE WHEN f.requester_id=? THEN f.addressee_id ELSE f.requester_id END WHERE (f.requester_id=? OR f.addressee_id=?) AND f.status='accepted' ORDER BY u.display_name,u.name",(user['id'],user['id'],user['id'])).fetchall()
            outgoing=c.execute("SELECT u.* FROM friendships f JOIN users u ON u.id=f.addressee_id WHERE f.requester_id=? AND f.status='pending'",(user['id'],)).fetchall()
        content='<section class="card"><h1>Friends · Your Circle</h1><h2>Requests for you</h2>'
        if not incoming:content+='<p class="muted">No new requests.</p>'
        for m in incoming:content+='<div class="person">%s<b class="grow">%s</b><form method="post" action="/friend">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="accept"><button>Accept</button></form><form method="post" action="/friend">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="decline"><button class="light">Decline</button></form></div>'%(avatar(m),esc(display(m)),form_csrf(user),m['id'],form_csrf(user),m['id'])
        content+='<h2>Your friends</h2>'
        for m in friends:content+='<div class="person">%s<a class="grow" href="/messages?with=%s"><b>%s</b></a><form method="post" action="/friend">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="remove"><button class="light">Remove</button></form></div>'%(avatar(m),m['id'],esc(display(m)),form_csrf(user),m['id'])
        if not friends:content+='<p class="muted">Find members and send a friend request.</p>'
        content+='<h2>Sent requests</h2>'
        for m in outgoing:content+='<div class="person">%s <span class="grow">%s <small>Pending</small></span><form method="post" action="/friend">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="cancel"><button class="light">Cancel</button></form></div>'%(avatar(m),esc(display(m)),form_csrf(user),m['id'])
        return self.html(shell('Friends',content+'</section>',user))
    def messages(self,user,other):
        with connect() as c:
            fs=c.execute("SELECT u.* FROM friendships f JOIN users u ON u.id=CASE WHEN f.requester_id=? THEN f.addressee_id ELSE f.requester_id END WHERE (f.requester_id=? OR f.addressee_id=?) AND f.status='accepted'",(user['id'],user['id'],user['id'])).fetchall()
            selected=user_by_id(c,int(other)) if other.isdigit() else None
            valid=selected and any(f['id']==selected['id'] for f in fs)
            rows=[]
            if valid:
                rows=c.execute('SELECT * FROM messages WHERE (sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?) ORDER BY created DESC LIMIT 100',(user['id'],selected['id'],selected['id'],user['id'])).fetchall()[::-1]
                c.execute('UPDATE messages SET read_at=? WHERE sender_id=? AND recipient_id=? AND read_at IS NULL',(time.time(),selected['id'],user['id']))
        content='<section class="card"><h1>Messages</h1><div class="grid"><div><h3>Friends</h3>'
        for f in fs:content+='<div class="person">%s<a href="/messages?with=%s">%s</a></div>'%(avatar(f,34),f['id'],esc(display(f)))
        content+='</div><div>'
        if valid:
            content+='<h3>Conversation with %s</h3><div class="message-list">'%esc(display(selected))
            for m in rows:content+='<div class="bubble %s">%s<time>%s</time></div>'%('mine' if m['sender_id']==user['id'] else '',esc(m['body']),esc(date(m['created'])))
            content+='</div><form method="post" action="/message">%s<input type="hidden" name="to" value="%s"><textarea name="body" required maxlength="2000" placeholder="Write a message"></textarea><button>Send</button></form>'%(form_csrf(user),selected['id'])
        else:content+='<p class="muted">Choose an accepted friend to start messaging.</p>'
        return self.html(shell('Messages',content+'</div></div></section>',user))
    def notifications(self,user):
        with connect() as c:
            rows=c.execute('SELECT n.*,u.name,u.display_name,u.avatar_key FROM notifications n LEFT JOIN users u ON u.id=n.actor_id WHERE n.user_id=? ORDER BY n.created DESC LIMIT 100',(user['id'],)).fetchall()
        content='<section class="card"><h1>Notifications</h1><form method="post" action="/notifications/read-all">%s<button class="light">Mark all as read</button></form><p class="small">Notifications from the same person about the same thing within an hour are grouped together. Choose which kinds you get in <a href="/settings">Settings</a>.</p>'%form_csrf(user)
        if not rows:content+='<p>You are all caught up.</p>'
        # Group by (actor, kind, target) within the last hour.
        grouped=[]
        for n in rows:
            if grouped:
                g=grouped[-1]
                if g['actor_id']==n['actor_id'] and g['kind']==n['kind'] and g['target_id']==n['target_id'] and g['created']-n['created']<=3600:
                    g['count']+=1;g['unread']=g['unread'] or not n['read_at'];continue
            grouped.append(dict(n));grouped[-1]['count']=1;grouped[-1]['unread']=not n['read_at']
        for n in grouped:
            dest='/friends' if n['kind'] in ('friend_request','friend_accept') else ('/messages?with=%s'%n['actor_id'] if n['kind']=='message' else ('/post/%s'%esc(n['target_id']) if n['kind'] in ('like','comment','repost','reaction') and n['target_id'] and str(n['target_id']).isdigit() else ('/groups' if n['kind']=='group_invite' else ('/events' if n['kind'] in ('event_invite','event_reminder') else '/'))))
            label='%s %s'%(esc(n['display_name'] or n['name'] or 'The Quad'),esc(n['body']))
            if n['count']>1:
                label+=' <span class="badge">×%s</span>'%n['count']
            content+='<a class="person %s" href="%s">%s<div><b>%s</b><time>%s</time></div></a>'%('unread' if n['unread'] else '',dest,avatar(n),label,esc(date(n['created'])))
        return self.html(shell('Notifications',content+'</section>',user))
    def listening_refresh(self,user):
        # Stub, deliberately unimplemented: there are no Spotify credentials
        # in this codebase and no user's Spotify is connected, so this
        # endpoint must NOT pretend to sync anything. It only explains
        # what live sync would need. No state is changed and no tokens
        # are read, stored or sent anywhere.
        content=('<section class="card"><h1>Live Spotify sync — not set up</h1>'
            '<p>Live now-playing is <b>not connected</b>. Nothing on this page has been synced from Spotify, and no song shown here is a live feed.</p>'
            '<p>Your Listening Note (set by hand on the feed or your profile) still works — it lasts 24 hours and friends can see it.</p>'
            '<h2>What live sync would need</h2>'
            '<ul><li>A Spotify developer app configured on the server (client ID and secret — none are stored in this code).</li>'
            '<li>Each person linking their own Spotify account through Spotify OAuth.</li>'
            '<li>Permission to read what they are currently playing (<code>user-read-currently-playing</code> scope).</li></ul>'
            '<p>Until those are set up with Mitchell\'s approval, this page will keep saying not connected — it will not fake a live status.</p>'
            '<p><a class="btn" href="/">Back to feed</a> <a class="btn light" href="/profile">Set a Listening Note instead</a></p></section>')
        return self.html(shell('Spotify live sync',content,user))
    def settings(self,user):
        with connect() as c:
            row=c.execute('SELECT private_account FROM user_settings WHERE user_id=?',(user['id'],)).fetchone()
            private=bool(row['private_account']) if row else False
            blocked=c.execute('SELECT u.* FROM blocks b JOIN users u ON u.id=b.blocked_id WHERE b.blocker_id=? ORDER BY u.display_name,u.name',(user['id'],)).fetchall()
        checked=' checked' if private else ''
        content='<section class="card"><h1>Settings &amp; privacy</h1><p>Manage who can see your profile and the people you have blocked.</p><form method="post" action="/settings">%s<label class="row"><input type="checkbox" name="private_account"%s><span><b>Private account</b><br><small>Hides your bio, interests and posts from people who are not accepted friends.</small></span></label><button class="btn">Save settings</button></form></section><section class="card"><h2>Blocked people</h2>'%(form_csrf(user),checked)
        if not blocked: content+='<p class="muted">You have not blocked anyone.</p>'
        for m in blocked: content+='<div class="person">%s<b class="grow">%s</b><form method="post" action="/block">%s<input type="hidden" name="user_id" value="%s"><input type="hidden" name="action" value="unblock"><button class="light">Unblock</button></form></div>'%(avatar(m),esc(display(m)),form_csrf(user),m['id'])
        content+='<section class="card"><h2>Change password</h2><p><a class="btn light" href="/change-password">Change your password</a></p></section><section class="card"><h2>Password recovery</h2><p>If you forget your password, the app can email a six-digit reset code. SMTP email delivery must be configured on the server for this feature to send mail.</p></section>'
        # Notification preferences (Update 6): per-kind on/off, in-app only.
        # Push notifications are NOT offered — they need a service worker,
        # VAPID keys and a push sender this server does not have.
        with connect() as _c:
            _prefs={r['kind']:r['enabled'] for r in _c.execute('SELECT kind,enabled FROM notification_prefs WHERE user_id=?',(user['id'],)).fetchall()}
        content+='<section class="card"><h2>Notification preferences</h2><p class="small">Choose which in-app notifications you get. Push notifications are not available — The Quad does not have a push service set up.</p><form method="post" action="/notification-prefs">%s'%form_csrf(user)
        for _k in NOTIF_KINDS:
            _on=_prefs.get(_k,1)
            content+='<label class="row"><input type="checkbox" name="pref_%s"%s><span>%s</span></label>'%(_k,' checked' if _on else '',esc(_k.replace('_',' ')))
        content+='<button class="btn">Save notification preferences</button></form></section>'
        content+='<section class="card"><h2>Your data</h2><p>Download everything you have posted, commented and messaged as JSON. It only includes your own messages — never anyone else\'s private messages.</p><p><a class="btn light" href="/export-data">Download my data (JSON)</a></p></section>'
        if not user['is_admin']:
            content+='<section class="card"><h2>Delete my account</h2><p>Deleting your account removes your profile, posts, comments and messages. It is reversible for 30 days — an admin can restore it from the admin panel in that window. After 30 days the saved copy is deleted. To delete, type your email address and enter your current password.</p><form method="post" action="/account/delete" data-confirm="Delete your account? This can be restored by an admin for 30 days.">%s<label>Type your email to confirm</label><input type="email" name="confirm_email" required maxlength="254"><label>Current password</label><input type="password" name="password" required><button class="danger">Delete my account</button></form></section>'%form_csrf(user)
        else:
            content+='<section class="card"><h2>Delete my account</h2><p class="small">Admin accounts cannot be deleted here — another admin would need to remove the account from the admin panel.</p></section>'
        content+='<section class="card"><h2>Spotify — live now-playing</h2><p><b>Live Spotify sync is not set up yet.</b> Your Listening Note is set by hand and lasts 24 hours — that works now, no Spotify account needed.</p><p>To show what you are actually listening to in real time, The Quad would need: a Spotify developer app configured on the server, each person linking their own Spotify account, and permission to read what they are currently playing (<code>user-read-currently-playing</code>). No Spotify tokens are stored by The Quad today, and nothing here claims to be connected.</p><p><a class="btn light" href="/notes/refresh">Check live now-playing status</a></p></section>'
        content+='<section class="card"><h2>Block someone</h2><form method="get" action="/people"><label>Find a member</label><input name="q" placeholder="Search by name"><button class="light">Open People</button></form></section>'
        return self.html(shell('Settings',content,user))
    def public_profile(self,user,key):
        try:uid=int(key)
        except ValueError:return self.html(shell('Profile','<section class="card">Member not found.</section>',user),404)
        with connect() as c:
            m=user_by_id(c,uid)
            if m and not can_view_post(c,user['id'],uid):return self.html(shell('Member','<section class="card">This profile is private or unavailable.</section>',user),403)
            posts=c.execute('SELECT * FROM posts WHERE user_id=? AND is_hidden=0 ORDER BY created DESC LIMIT 20',(uid,)).fetchall() if m else []
            member_note=visible_listening_note(c,user['id'],uid) if m else None
        if not m:return self.html(shell('Profile','<section class="card">Member not found.</section>',user),404)
        content='<section class="card">%s<h1>%s</h1><p>%s</p><p><b>Interests:</b> %s</p></section>'%(avatar(m,90),esc(display(m)),esc(m['bio']),esc(m['interests']))
        if member_note:content+='<section class="card"><h2>Listening Note</h2>%s</section>'%spotify_note_html(member_note, embed=True)
        pinned=[p for p in posts if p['is_pinned']]
        if pinned:
            content+='<section class="card"><h2>Pinned posts</h2></section>'
            for p in pinned:content+=self.post_card(user,p)
        for p in posts:
            if p['is_pinned']:continue
            content+='<article class="card"><time>%s</time><p>%s</p></article>'%(esc(date(p['created'])),render_body(p['body']))
        return self.html(shell('Member',content,user))
    def admin(self,user):
        if not user['is_admin']:return self.html(shell('Admin','<section class="card"><h1>Admin access only</h1></section>',user),403)
        now=time.time();day=now-86400;week=now-7*86400
        with connect() as c:
            totals=c.execute('''SELECT
                (SELECT COUNT(*) FROM users) users,
                (SELECT COUNT(*) FROM users WHERE last_seen>?) active,
                (SELECT COUNT(*) FROM posts) posts,
                (SELECT COUNT(*) FROM users WHERE joined>?) signups_week,
                (SELECT COUNT(*) FROM page_views WHERE created>?) views_day,
                (SELECT COUNT(*) FROM page_views WHERE created>?) views_week,
                (SELECT COUNT(DISTINCT user_id) FROM page_views WHERE created>? AND user_id IS NOT NULL) visitors_day,
                (SELECT COUNT(*) FROM friendships WHERE status='pending') pending_friends,
                (SELECT COUNT(*) FROM messages WHERE created>?) messages_day''',(now-ACTIVE_SECONDS,week,day,week,day,day)).fetchone()
            hourly=c.execute('SELECT CAST((?-created)/3600 AS INTEGER) hour_ago,COUNT(*) amount FROM page_views WHERE created>? GROUP BY hour_ago',(now,now-24*3600)).fetchall()
            top_paths=c.execute('SELECT path,COUNT(*) amount FROM page_views WHERE created>? GROUP BY path ORDER BY amount DESC,path LIMIT 8',(week,)).fetchall()
            people=c.execute('SELECT * FROM users WHERE last_seen>? ORDER BY last_seen DESC LIMIT 50',(now-ACTIVE_SECONDS,)).fetchall()
            audit=c.execute('SELECT a.*,u.name,u.display_name FROM audit_log a LEFT JOIN users u ON u.id=a.actor_user_id ORDER BY a.created DESC LIMIT 40').fetchall()
            shield=c.execute('SELECT action,COUNT(*) entries,SUM(request_count) total FROM rate_limits GROUP BY action ORDER BY total DESC LIMIT 10').fetchall()
            all_users=c.execute('SELECT id,name,display_name,email,is_admin,joined,last_seen FROM users ORDER BY joined LIMIT 100').fetchall()
        counts={r['hour_ago']:r['amount'] for r in hourly};peak=max(counts.values(),default=0)
        content='<section class="card"><span class="eyebrow">PRIVATE ADMIN VIEW</span><h1>Admin dashboard</h1><p><a class="btn" href="/admin/moderation">Open moderation queue</a> <a class="btn light" href="/export-data">Download my data</a></p><p>Traffic is recorded as page and time only; the app does not retain raw visitor IP addresses. Active means seen in the last two minutes. Page-view logging is throttled to 20/minute per visitor, so traffic figures are approximate.</p><div class="shield"><h2>Traffic Shield — app level (measured by The Quad)</h2><p class="small">Rate limits are enforced in the app (posts 30/hour, comments 120/hour, messages 300/hour, logins 20/10 min, registrations 5/hour). Flooding is slowed or blocked before it can take the site down. Recent rate-limit windows below are keyed one-way identifiers only — no raw IPs are stored.</p><h2 class="shield-h2">Traffic Shield — Nginx level (configured, not measured)</h2><p class="small">Nginx sits in front of the app and blocks traffic before Python ever sees it, so its counts are <b>not visible in this panel</b>. These are the limits configured in nginx.conf.template, not live measurements: 20 requests/second per IP (burst 60), max 20 simultaneous connections per IP, max request body 27 MB, body timeout 30s, read/send timeout 60s, keep-alive 15s. If Nginx blocks someone, the app records nothing for that request.</p>'
        if shield:
            for row in shield:content+='<div class="table-row"><code>%s</code><span>%s windows · %s requests</span></div>'%(esc(row['action']),row['entries'],row['total'])
        else:content+='<p class="muted">No rate-limit activity recorded in the last 2 days.</p>'
        content+='</div><div class="metric-grid">'
        cards=[(totals['views_day'],'Page views · 24h'),(totals['visitors_day'],'Signed-in visitors · 24h'),(totals['views_week'],'Page views · 7d'),(totals['active'],'Active users'),(totals['signups_week'],'New accounts · 7d'),(totals['posts'],'Posts'),(totals['pending_friends'],'Pending requests'),(totals['messages_day'],'Messages · 24h'),(totals['users'],'Total accounts')]
        for value,label in cards:content+='<div class="metric"><b>%s</b><span>%s</span></div>'%(value,esc(label))
        content+='</div><section class="card"><h2>Traffic flow · past 24 hours</h2><div class="traffic-chart">'
        for ago in range(23,-1,-1):
            amount=counts.get(ago,0);height=max(3,round(amount/peak*100)) if peak else 3
            label=time.strftime('%H',time.localtime(now-ago*3600))
            content+='<div class="traffic-bar" title="%s:00 · %s page views"><i class="tb%s"></i><small>%s</small></div>'%(label,amount,max(3,int(round(height/5.0)*5)) if height>3 else 3,label if ago%4==0 else '')
        content+='</div></section><div class="grid"><section class="card"><h2>Most visited pages · 7d</h2>'
        if top_paths:
            for row in top_paths:content+='<div class="table-row"><code>%s</code><b>%s</b></div>'%(esc(row['path']),row['amount'])
        else:content+='<p class="muted">No page visits recorded yet.</p>'
        content+='</section><section class="card"><h2>Active now · %s</h2><p class="small">Updates when this page reloads.</p><div class="scroll-panel">'%len(people)
        if people:
            for person in people:content+='<div class="person">%s<div>%s<br><small>%s · seen %s</small></div></div>'%(avatar(person),esc(display(person)),esc(person['email']),esc(date(person['last_seen'])))
        else:content+='<p class="muted">No members active in the last two minutes.</p>'
        content+='</div></section></div><section class="card"><h2>Audit log</h2><p class="small">Private to administrators. Deletion records remain after the content itself is removed.</p><div class="scroll-panel">'
        if audit:
            for event in audit:
                try:meta=json.loads(event['metadata'] or '{}')
                except Exception:meta={}
                summary=''
                if meta.get('body'):summary=' · '+str(meta.get('body'))[:180]
                actor=event['display_name'] or event['name'] or ('User '+str(event['actor_user_id']) if event['actor_user_id'] else 'System')
                content+='<div class="table-row"><span><b>%s</b> · %s %s #%s%s<br><small>%s</small></span></div>'%(esc(actor),esc(event['action']),esc(event['target_type']),esc(event['target_id'] or ''),esc(summary),esc(date(event['created'])))
        else:content+='<p class="muted">No audit events yet.</p>'
        content+='</div></section><section class="card"><h2>Manage accounts</h2><p class="small">Remove an account only when needed. Removal is <b>reversible for 30 days</b>: before anything is deleted, the app takes a full database backup and saves that account (profile, posts, comments, messages, likes, friendships, notifications and upload files) so it can be restored from the panel below. Upload files are moved to a removed-uploads store, not deleted. You cannot remove yourself or another admin. To remove someone, type their exact email address in the box — this is the typed confirmation.</p><div class="scroll-panel">'
        for person in all_users:
            if person['id']==user['id'] or person['is_admin']:
                content+='<div class="table-row"><span>%s · %s</span><span class="muted">Admin — protected</span></div>'%(esc(display(person)),esc(person['email']))
            else:
                content+='<div class="table-row"><span>%s · %s</span><form method="post" action="/admin/remove-user" data-confirm="Remove %s? A backup is taken first and the account can be restored for 30 days.">%s<input type="hidden" name="user_id" value="%s"><label class="small">Type their email to confirm</label><input name="confirm_email" type="email" required maxlength="254" placeholder="%s" class="confirm-email"><button class="danger">Remove</button></form></div>'%(esc(display(person)),esc(person['email']),esc(display(person)),form_csrf(user),person['id'],esc(person['email']))
        content+='</div></section><section class="card"><h2>Recently removed — restore within 30 days</h2><p class="small">Removed accounts are kept here with their data until the restore deadline. After that, the saved copy is deleted and only the database backup remains.</p><div class="scroll-panel">'
        removed=c.execute('SELECT * FROM removed_users ORDER BY removed_at DESC LIMIT 20').fetchall()
        if removed:
            for r in removed:
                content+='<div class="table-row"><span>%s · %s<br><small>Removed %s · restore by %s · backup %s</small></span><form method="post" action="/admin/restore-user" data-confirm="Restore %s and their saved content?">%s<input type="hidden" name="removed_id" value="%s"><button>Restore</button></form></div>'%(esc(r['name']),esc(r['email']),esc(date(r['removed_at'])),esc(date(r['restore_deadline'])),esc(r['backup_file'] or '—'),esc(r['name']),form_csrf(user),r['id'])
        else:
            content+='<p class="muted">No accounts removed recently.</p>'
        content+='</div></section></section>'
        return self.html(shell('Admin',content,user))
    def save_upload(self,user,fields,files,name,kind):
        if name not in files:return None
        filename,data,ctype=files[name]
        is_img=ctype in ('image/jpeg','image/png','image/gif','image/webp');is_vid=ctype in ('video/mp4','video/webm')
        if kind=='avatar' and not is_img:raise ValueError('Profile pictures must be JPEG, PNG, GIF or WebP.')
        if kind=='post' and not (is_img or is_vid):raise ValueError('Posts support JPEG, PNG, GIF, WebP, MP4 and WebM.')
        if not valid_file(data,ctype):raise ValueError('The file contents do not match its type.')
        limit=25_000_000 if is_vid else 8_000_000
        if len(data)>limit:raise ValueError('Videos must be under 25 MB; photos under 8 MB.')
        ext=mimetypes.guess_extension(ctype) or '.bin';key=uuid.uuid4().hex+ext
        target=UPLOAD_DIR/key
        with open(target,'wb') as f:f.write(data)
        if is_vid:
            duration=verified_video_duration(target)
            if duration is None:
                # No client-supplied fallback: a duration the server cannot
                # verify itself must not be trusted, in dev or production.
                # The browser-side duration_ms field is ignored entirely.
                target.unlink(missing_ok=True);raise ValueError('Video validation is unavailable on the server. Try again later.')
            if not duration or duration>30.25:
                target.unlink(missing_ok=True);raise ValueError('Videos must be 30 seconds or shorter.')
        try:
            with connect() as c:c.execute('INSERT INTO uploads(key,user_id,content_type,size,created) VALUES(?,?,?,?,?)',(key,user['id'],ctype,len(data),time.time()))
        except Exception:
            target.unlink(missing_ok=True);raise
        return key,ctype
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path;user,token=self.session()
        if not self.same_origin_request():
            return self.html(shell('Request rejected','<section class="card">Cross-site request rejected.</section>',user),403)
        client_fp=self.client_fingerprint()
        try:data,files=self.read_form()
        except ValueError as e:return self.html(shell('Request',str(e),user),413)
        if path=='/send-reset-code':
            if not verify_reset_token(data.get('reset_token',''), 'send-reset'):
                return self.html(shell('Reset password','<section class="card"><p>That reset request expired or was invalid. Go back and request a new code.</p><p><a class="btn" href="/forgot-password">Request a code</a></p></section>'),403)
            email=normalize_email(data.get('email',''));now=time.time()
            mail_ready=bool(os.environ.get('SMTP_HOST')) and bool(os.environ.get('SMTP_FROM'))
            allowed=rate_limit('reset-client-hour',client_fp,10,3600)
            if email:
                allowed=allowed and rate_limit('reset-email-hour',email,5,3600) and rate_limit('reset-email-minute',email,1,60)
            if email and allowed and mail_ready:
                with connect() as c:account=c.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()
                if account:
                    code='%06d'%secrets.randbelow(1000000)
                    with connect() as c:c.execute('INSERT INTO password_reset_codes(user_id,code_hash,expires,attempts,created) VALUES(?,?,?,0,?) ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash,expires=excluded.expires,attempts=0,created=excluded.created',(account['id'],password_hash(code),now+600,now))
                    deliver_reset_email_async(account['id'],email,code)
            if mail_ready:
                message='If this email belongs to an account, a code is being sent automatically. Check your inbox and spam folder.'
            else:
                message='Email delivery is not configured on this server yet, so codes cannot be sent. Ask the administrator to configure mail.'
            return self.reset_password_form(email,message)
        if path=='/reset-password':
            email=normalize_email(data.get('email',''));code=data.get('code','').strip()
            if not verify_reset_token(data.get('reset_token',''), 'reset-password'):
                return self.reset_password_form(email,'That reset request expired or was invalid. Request a new code.')
            pwd=data.get('password','');confirm=data.get('confirm_password','')
            if not rate_limit('reset-verify-client',client_fp,30,600) or (email and not rate_limit('reset-verify-email',email,10,600)):
                return self.reset_password_form(email,'Too many attempts. Wait a few minutes and request a new code.')
            if pwd!=confirm:
                return self.reset_password_form(email,'Passwords do not match. Enter the same password twice.')
            pw_err=validate_password(pwd,'',email)
            if pw_err:
                return self.reset_password_form(email,pw_err)
            reset=False
            if email and len(code)==6 and code.isdigit():
                with connect() as c:
                    row=c.execute('SELECT r.user_id,r.code_hash,r.expires,r.attempts FROM password_reset_codes r JOIN users u ON u.id=r.user_id WHERE u.email=?',(email,)).fetchone()
                    if row:
                        if row['expires']<=time.time() or row['attempts']>=5:
                            c.execute('DELETE FROM password_reset_codes WHERE user_id=?',(row['user_id'],))
                        elif check_password(code,row['code_hash']):
                            c.execute('UPDATE users SET password_hash=?,recovery_hash=NULL,must_change_password=0 WHERE id=?',(password_hash(pwd),row['user_id']))
                            c.execute('DELETE FROM sessions WHERE user_id=?',(row['user_id'],))
                            c.execute('DELETE FROM password_reset_codes WHERE user_id=?',(row['user_id'],))
                            audit_event(c,row['user_id'],'password_reset','user',row['user_id'])
                            reset=True
                        else:
                            attempts=row['attempts']+1
                            if attempts>=5:c.execute('DELETE FROM password_reset_codes WHERE user_id=?',(row['user_id'],))
                            else:c.execute('UPDATE password_reset_codes SET attempts=? WHERE user_id=?',(attempts,row['user_id']))
            if reset:
                content='<section class="card"><p>Password reset. Sign in with your new password. The code has expired and cannot be reused.</p><a class="btn" href="/">Return to sign in</a></section>'
                return self.html(shell('Reset password',content))
            return self.reset_password_form(email,'The code could not be verified or has expired. Request a new code and try again.')
        if path=='/register':
            name=data.get('name','').strip();email=normalize_email(data.get('email',''));pwd=data.get('password','');confirm=data.get('confirm_password','')
            if not rate_limit('register-client',client_fp,5,3600):return self.html(register_page('Too many account attempts. Try again later.',name,email),429)
            if not name or len(name)>80 or any(ord(ch)<32 and ch not in '\t\n\r' for ch in name) or not email:return self.html(register_page('Enter your name and a valid email address.',name,email),400)
            pw_err=validate_password(pwd,name,email)
            if pw_err:return self.html(register_page(pw_err,name,email),400)
            if pwd!=confirm:return self.html(register_page('Passwords do not match. Enter the same password twice.',name,email),400)
            try:
                with connect() as c:
                    first_local=(not PRODUCTION and c.execute('SELECT COUNT(*) FROM users').fetchone()[0]==0)
                    admin=1 if email in ADMIN_EMAILS or first_local else 0
                    cur=c.execute('INSERT INTO users(name,email,password_hash,is_admin,joined) VALUES(?,?,?,?,?)',(name,email,password_hash(pwd),admin,time.time()))
                    new_id=cur.lastrowid
                    c.execute('INSERT OR IGNORE INTO user_settings(user_id,private_account) VALUES(?,0)',(new_id,))
                    audit_event(c,new_id,'register','user',new_id,{'admin':bool(admin)})
                new=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);now=time.time()
                with connect() as c:
                    c.execute('UPDATE users SET last_seen=? WHERE id=?',(now,new_id))
                    c.execute('INSERT INTO sessions(token,user_id,csrf,expires,last_seen) VALUES(?,?,?,?,?)',(new,new_id,csrf,now+SESSION_MAX_AGE,now))
                # Identical observable response to the duplicate case below:
                # same status (200) and same page. A genuinely new account
                # IS signed in (session cookie set), so Continue leads to
                # the feed; a duplicate gets no cookie, so Continue leads
                # to the sign-in landing. The Set-Cookie header itself is
                # the one unavoidable difference — see MERGE_NOTES.
                content=REGISTRATION_NOTICE
                return self.html(shell('Create account',content),200,'network_session=%s; HttpOnly; %sSameSite=Lax; Path=/; Max-Age=%s; Priority=High'%(new,self.secure_cookie(),SESSION_MAX_AGE))
            except sqlite3.IntegrityError:
                # Generic response on purpose: confirming "that email already
                # exists" would let anyone probe which emails have accounts.
                # Same status (200) and same page as a successful
                # registration, and no session cookie is set.
                return self.html(shell('Create account',REGISTRATION_NOTICE),200)
        if path=='/login':
            email=normalize_email(data.get('email',''));pwd=data.get('password','')
            if not rate_limit('login-client',client_fp,20,600) or (email and not rate_limit('login-email',email,10,600)):
                return self.html(login_page('Too many sign-in attempts. Wait a few minutes and try again.',email),429)
            with connect() as c:account=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone() if email else None
            encoded=account['password_hash'] if account else DUMMY_PASSWORD_HASH
            valid=check_password(pwd,encoded)
            if not account or not valid:return self.html(login_page('Email or password is incorrect. Check both and try again.',email),401)
            ph=password_hash(pwd) if password_needs_rehash(account['password_hash']) else account['password_hash'];new=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);now=time.time()
            # Forced change: the user just proved the password, so this is
            # the one safe moment to judge it. Old hashes (legacy format /
            # low iterations) and passwords failing the current policy both
            # force a change before normal use. Admins are not exempt.
            must_change = 1 if password_must_change(account['password_hash'], pwd, account['display_name'] or account['name'], account['email']) else 0
            with connect() as c:
                c.execute('UPDATE users SET password_hash=?,last_seen=?,must_change_password=? WHERE id=?',(ph,now,must_change,account['id']))
                c.execute('DELETE FROM sessions WHERE user_id=? AND token IN (SELECT token FROM sessions WHERE user_id=? ORDER BY last_seen DESC LIMIT -1 OFFSET 4)',(account['id'],account['id']))
                c.execute('INSERT INTO sessions(token,user_id,csrf,expires,last_seen) VALUES(?,?,?,?,?)',(new,account['id'],csrf,now+SESSION_MAX_AGE,now))
                if must_change:
                    audit_event(c,account['id'],'password_change_required','user',account['id'])
            dest='/change-password' if must_change else '/'
            return self.go(dest,'network_session=%s; HttpOnly; %sSameSite=Lax; Path=/; Max-Age=%s; Priority=High'%(new,self.secure_cookie(),SESSION_MAX_AGE))
        if not self.authenticated(user,data):return self.html(shell('Sign in required','<section class="card">Session expired or request rejected. <a href="/">Return to sign in</a></section>',user),403)
        if user['must_change_password'] and path not in ('/change-password','/logout'):
            return self.html(change_password_page(user, 'Your password needs updating before you can use the rest of The Quad.', forced=True),403)
        if path=='/change-password':
            current=data.get('current_password','');newpwd=data.get('password','');confirm=data.get('confirm_password','')
            if not check_password(current, user['password_hash']):
                return self.html(change_password_page(user, 'Your current password was incorrect.', forced=bool(user['must_change_password'])),401)
            if newpwd!=confirm:
                return self.html(change_password_page(user, 'Passwords do not match. Enter the same password twice.', forced=bool(user['must_change_password'])),400)
            pw_err=validate_password(newpwd, user['display_name'] or user['name'], user['email'])
            if pw_err:
                return self.html(change_password_page(user, pw_err, forced=bool(user['must_change_password'])),400)
            if check_password(newpwd, user['password_hash']):
                return self.html(change_password_page(user, 'Choose a new password that is different from your current one.', forced=bool(user['must_change_password'])),400)
            with connect() as c:
                c.execute('UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?',(password_hash(newpwd),user['id']))
                c.execute('DELETE FROM sessions WHERE user_id=? AND token<>?',(user['id'],token))
                audit_event(c,user['id'],'password_change','user',user['id'],{'forced':bool(user['must_change_password'])})
            return self.go('/')
        write_limits={'/posts':(30,3600),'/comment':(120,3600),'/message':(300,3600),'/friend':(120,3600),'/block':(60,3600),'/like':(300,3600),'/react':(300,3600),'/comment-react':(300,3600),'/edit-comment':(120,3600),'/repost':(30,3600),'/save':(120,3600),'/pin':(60,3600),'/profile':(30,3600),'/settings':(30,3600),'/delete':(120,3600),'/delete-comment':(120,3600),'/listening-note':(10,3600),'/admin/remove-user':(10,3600),'/admin/restore-user':(10,3600),'/group':(30,3600),'/group-action':(120,3600),'/group-post':(30,3600),'/event':(30,3600),'/event-rsvp':(120,3600),'/event-invite':(60,3600),'/poll':(30,3600),'/poll-vote':(120,3600),'/report':(20,3600),'/admin/report-action':(60,3600),'/notification-prefs':(30,3600),'/notifications/read-all':(60,3600),'/account/delete':(5,3600)}
        if path in write_limits:
            limit,window=write_limits[path]
            if not rate_limit('write:'+path,'%s:%s'%(user['id'],client_fp),limit,window):return self.html(shell('Slow down','<section class="card">Too many requests. Try again shortly.</section>',user),429)
        if path=='/admin/remove-user':
            if not user['is_admin']:return self.html(shell('Admin','<section class="card"><h1>Admin access only</h1></section>',user),403)
            try:target=int(data.get('user_id',''))
            except ValueError:return self.go('/admin')
            if target==user['id']:return self.html(shell('Admin','<section class="card">You cannot remove your own account.</section>',user),400)
            # Typed confirmation: the admin must type the victim's exact email.
            confirm_email=normalize_email(data.get('confirm_email',''))
            with connect() as c:
                victim=user_by_id(c,target)
                if not victim:return self.go('/admin')
                if victim['is_admin'] or victim['email'].lower() in ADMIN_EMAILS:return self.html(shell('Admin','<section class="card">Admin accounts are protected and cannot be removed here.</section>',user),403)
                if not confirm_email or confirm_email!=victim['email'].lower():
                    return self.html(shell('Admin','<section class="card">Type the member\'s exact email address to confirm removal.</section>',user),400)
                # Safety net: full DB backup first, then save the account's
                # data for a 30-day restore. Upload files are MOVED to the
                # removed-uploads store, not deleted.
                backup_path=backup_database()
                def rows(table, where, args):
                    return [dict(r) for r in c.execute('SELECT * FROM %s WHERE %s' % (table, where), args).fetchall()]
                saved={
                    'user': dict(victim),
                    'settings': rows('user_settings','user_id=?',(target,)),
                    'posts': rows('posts','user_id=?',(target,)),
                    'comments': rows('comments','user_id=?',(target,)),
                    'likes': rows('likes','user_id=?',(target,)),
                    'friendships': rows('friendships','requester_id=? OR addressee_id=?',(target,target)),
                    'messages': rows('messages','sender_id=? OR recipient_id=?',(target,target)),
                    'notifications': rows('notifications','user_id=? OR actor_id=?',(target,target)),
                    'uploads': rows('uploads','user_id=?',(target,)),
                    'blocks': rows('blocks','blocker_id=? OR blocked_id=?',(target,target)),
                }
                keys=[r['key'] for r in c.execute('SELECT key FROM uploads WHERE user_id=?',(target,)).fetchall()]
                now=time.time()
                cur=c.execute('INSERT INTO removed_users(original_user_id,name,email,password_hash,is_admin,joined,data_json,backup_file,removed_at,removed_by,restore_deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(target,display(victim),victim['email'],victim['password_hash'],victim['is_admin'],victim['joined'],json.dumps(saved,ensure_ascii=False),backup_path.name if backup_path else None,now,user['id'],now+REMOVED_RETENTION_DAYS*86400))
                audit_event(c,user['id'],'remove_user','user',target,{'name':display(victim),'email':victim['email'],'body':display(victim)+' <'+victim['email']+'>','restore_id':cur.lastrowid,'backup':backup_path.name if backup_path else None})
                c.execute('DELETE FROM users WHERE id=?',(target,))
            for key in keys:
                src=UPLOAD_DIR/key
                if src.is_file():shutil.move(str(src),str(REMOVED_DIR/key))
            return self.go('/admin')
        if path=='/admin/restore-user':
            if not user['is_admin']:return self.html(shell('Admin','<section class="card"><h1>Admin access only</h1></section>',user),403)
            try:rid=int(data.get('removed_id',''))
            except ValueError:return self.go('/admin')
            with connect() as c:
                rec=c.execute('SELECT * FROM removed_users WHERE id=?',(rid,)).fetchone()
                if not rec:return self.go('/admin')
                if rec['restore_deadline']<=time.time():return self.html(shell('Admin','<section class="card">The 30-day restore window for this account has expired. Restore from a database backup instead.</section>',user),400)
                if c.execute('SELECT 1 FROM users WHERE lower(email)=?',(rec['email'].lower(),)).fetchone():
                    return self.html(shell('Admin','<section class="card">An account now uses that email address, so this account cannot be restored as-is.</section>',user),409)
                try:saved=json.loads(rec['data_json'] or '{}')
                except Exception:return self.html(shell('Admin','<section class="card">The saved copy of this account is unreadable and cannot be restored.</section>',user),500)
                u=saved.get('user') or {}
                # Restore the user under their original id when it is free,
                # otherwise under a new id (foreign keys are remapped below).
                uid=rec['original_user_id']
                if c.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone():
                    uid=c.execute('INSERT INTO users(name,email,password_hash,is_admin,joined,last_seen) VALUES(?,?,?,?,?,?)',(u.get('name'),rec['email'],rec['password_hash'],rec['is_admin'],rec['joined'],time.time())).lastrowid
                else:
                    c.execute('INSERT INTO users(id,name,email,password_hash,is_admin,joined,last_seen) VALUES(?,?,?,?,?,?,?)',(uid,u.get('name'),rec['email'],rec['password_hash'],rec['is_admin'],rec['joined'],time.time()))
                idmap={rec['original_user_id']:uid}
                def remap_user(v):
                    return idmap.get(v, v)
                def insert_with_id(table, row, cols, id_col=None):
                    if id_col and c.execute('SELECT 1 FROM %s WHERE %s=?' % (table, id_col), (row.get(id_col),)).fetchone():
                        keep = {k: v for k, v in row.items() if k in cols and k != id_col}
                        cur = c.execute('INSERT INTO %s(%s) VALUES(%s)' % (table, ','.join(keep), ','.join('?'*len(keep))), [keep[k] for k in keep])
                        return cur.lastrowid
                    vals = [row.get(col) for col in cols]
                    c.execute('INSERT INTO %s(%s) VALUES(%s)' % (table, ','.join(cols), ','.join('?'*len(cols))), vals)
                    return row.get(id_col) if id_col else None
                for row in saved.get('settings',[]):
                    c.execute('INSERT OR IGNORE INTO user_settings(user_id,private_account) VALUES(?,?)',(remap_user(row.get('user_id')), row.get('private_account')))
                post_map = {}
                for row in saved.get('posts',[]):
                    new_pid = insert_with_id('posts', {**row, 'user_id': remap_user(row.get('user_id'))}, ['id','user_id','body','created','media_key','media_type','music_track','spotify_url','spotify_track_id'], 'id')
                    post_map[row.get('id')] = new_pid
                for row in saved.get('comments',[]):
                    insert_with_id('comments', {**row, 'post_id': post_map.get(row.get('post_id'), row.get('post_id')), 'user_id': remap_user(row.get('user_id'))}, ['id','post_id','user_id','body','created'], 'id')
                for row in saved.get('likes',[]):
                    c.execute('INSERT OR IGNORE INTO likes(post_id,user_id,created) VALUES(?,?,?)',(post_map.get(row.get('post_id'), row.get('post_id')), remap_user(row.get('user_id')), row.get('created')))
                for row in saved.get('friendships',[]):
                    c.execute('INSERT OR IGNORE INTO friendships(id,requester_id,addressee_id,status,created) VALUES(?,?,?,?,?)',(row.get('id'), remap_user(row.get('requester_id')), remap_user(row.get('addressee_id')), row.get('status'), row.get('created')))
                for row in saved.get('messages',[]):
                    c.execute('INSERT OR IGNORE INTO messages(id,sender_id,recipient_id,body,created,read_at) VALUES(?,?,?,?,?,?)',(row.get('id'), remap_user(row.get('sender_id')), remap_user(row.get('recipient_id')), row.get('body'), row.get('created'), row.get('read_at')))
                for row in saved.get('notifications',[]):
                    tgt = row.get('target_id')
                    if isinstance(tgt, str) and tgt.isdigit() and int(tgt) in post_map:
                        tgt = str(post_map[int(tgt)])
                    c.execute('INSERT OR IGNORE INTO notifications(id,user_id,actor_id,kind,target_id,body,created,read_at) VALUES(?,?,?,?,?,?,?,?)',(row.get('id'), remap_user(row.get('user_id')), remap_user(row.get('actor_id')), row.get('kind'), tgt, row.get('body'), row.get('created'), row.get('read_at')))
                for row in saved.get('uploads',[]):
                    c.execute('INSERT OR IGNORE INTO uploads(key,user_id,content_type,size,created) VALUES(?,?,?,?,?)',(row.get('key'), remap_user(row.get('user_id')), row.get('content_type'), row.get('size'), row.get('created')))
                for row in saved.get('blocks',[]): 
                    c.execute('INSERT OR IGNORE INTO blocks(blocker_id,blocked_id,created) VALUES(?,?,?)',(remap_user(row.get('blocker_id')), remap_user(row.get('blocked_id')), row.get('created')))
                # Restore display name / bio / interests / avatar from the saved user row.
                c.execute('UPDATE users SET display_name=?,bio=?,interests=?,avatar_key=?,must_change_password=? WHERE id=?',(u.get('display_name'),u.get('bio'),u.get('interests'),u.get('avatar_key'),1 if u.get('must_change_password') else 0,uid))
                audit_event(c,user['id'],'restore_user','user',uid,{'name':rec['name'],'email':rec['email'],'body':rec['name']+' <'+rec['email']+'>'})
                c.execute('DELETE FROM removed_users WHERE id=?',(rid,))
            for row in saved.get('uploads',[]):
                src=REMOVED_DIR/row['key']
                if src.is_file():shutil.move(str(src),str(UPLOAD_DIR/row['key']))
            return self.go('/admin')
        if path=='/logout':
            with connect() as c:c.execute('DELETE FROM sessions WHERE token=?',(token,))
            return self.go('/','network_session=; HttpOnly; %sSameSite=Lax; Path=/; Max-Age=0; Priority=High'%self.secure_cookie())
        if path=='/activity':self.touch(user,token);self.send_response(204);self.security_headers();self.end_headers();return
        if path=='/listening-note':
            dest='/profile' if '/profile' in (self.headers.get('Referer') or '') else '/'
            if data.get('clear')=='1':
                with connect() as c:
                    c.execute('DELETE FROM listening_notes WHERE user_id=?',(user['id'],))
                    audit_event(c,user['id'],'listening_note_clear','user',user['id'])
                return self.go(dest)
            title=data.get('track_title','').strip()
            artist=data.get('artist','').strip()
            if not title or len(title)>120 or len(artist)>120:
                return self.html(shell('Listening Note','<section class="card">Give your note a song title (1–120 characters). Artist up to 120 characters.</section>',user),400)
            spotify_id, spotify_url, spotify_err=parse_spotify_link(data.get('spotify_link',''))
            if spotify_err:
                return self.html(shell('Listening Note','<section class="card">%s</section>'%esc(spotify_err),user),400)
            now=time.time()
            with connect() as c:
                c.execute('INSERT INTO listening_notes(user_id,track_title,artist,spotify_url,spotify_track_id,created,expires_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET track_title=excluded.track_title,artist=excluded.artist,spotify_url=excluded.spotify_url,spotify_track_id=excluded.spotify_track_id,created=excluded.created,expires_at=excluded.expires_at',(user['id'],title,artist,spotify_url,spotify_id,now,now+LISTENING_NOTE_TTL))
                audit_event(c,user['id'],'listening_note_set','user',user['id'],{'track_title':title,'artist':artist,'spotify_track_id':spotify_id})
            return self.go(dest)
        if path=='/posts':
            body=data.get('body','').strip()
            if len(body)>2000:return self.html(shell('Post','Post text must be 2,000 characters or less.',user),400)
            try:up=self.save_upload(user,fields=data,files=files,name='media',kind='post')
            except ValueError as e:return self.html(shell('Upload',esc(e),user),400)
            track=data.get('music_track','').strip()
            if track not in MUSIC_TRACKS:track=''
            post_spotify_id, post_spotify_url, post_spotify_err=parse_spotify_link(data.get('spotify_link',''))
            if post_spotify_err:return self.html(shell('Post',esc(post_spotify_err),user),400)
            if not body and not up and not post_spotify_id:return self.html(shell('Post','Write text, attach a photo/video, or add a Spotify track.',user),400)
            with connect() as c:
                # Duplicate-spam filter: the same user posting identical text
                # (or an identical comment) within 60 seconds is rejected.
                if body and c.execute('SELECT 1 FROM posts WHERE user_id=? AND body=? AND created>?',(user['id'],body,time.time()-60)).fetchone():
                    return self.html(shell('Post','You just posted the same thing. Wait a minute before posting it again.',user),429)
                cur=c.execute('INSERT INTO posts(user_id,body,created,media_key,media_type,music_track,spotify_url,spotify_track_id) VALUES(?,?,?,?,?,?,?,?)',(user['id'],body,time.time(),up[0] if up else None,up[1] if up else None,track or None,post_spotify_url,post_spotify_id))
                store_hashtags(c,cur.lastrowid,body)
            return self.go('/')
        if path=='/delete':
            try:pid=int(data.get('post_id',''))
            except ValueError:return self.html(shell('Post','Invalid post.',user),400)
            with connect() as c:
                p=c.execute('SELECT * FROM posts WHERE id=?',(pid,)).fetchone()
                if not p:return self.html(shell('Post','Post not found.',user),404)
                if p['user_id']!=user['id'] and not user['is_admin']:return self.html(shell('Post','You can remove only your own posts.',user),403)
                audit_event(c,user['id'],'delete_post','post',pid,{'owner_user_id':p['user_id'],'body':(p['body'] or '')[:2000],'media_key':p['media_key'],'admin_action':bool(user['is_admin'] and p['user_id']!=user['id'])})
                c.execute('DELETE FROM posts WHERE id=?',(pid,))
                if p['media_key'] and not c.execute('SELECT 1 FROM users WHERE avatar_key=? UNION SELECT 1 FROM posts WHERE media_key=?',(p['media_key'],p['media_key'])).fetchone():c.execute('DELETE FROM uploads WHERE key=?',(p['media_key'],));(UPLOAD_DIR/p['media_key']).unlink(missing_ok=True)
            return self.go('/')
        if path=='/like':
            try:pid=int(data.get('post_id',''))
            except ValueError:return self.go('/')
            with connect() as c:
                p=c.execute('SELECT user_id FROM posts WHERE id=?',(pid,)).fetchone()
                if p and can_view_post(c,user['id'],p['user_id']):
                    if data.get('action')=='unlike':c.execute('DELETE FROM likes WHERE post_id=? AND user_id=?',(pid,user['id']))
                    else:
                        added=c.execute('INSERT OR IGNORE INTO likes(post_id,user_id,created) VALUES(?,?,?)',(pid,user['id'],time.time()))
                        if added.rowcount and p['user_id']!=user['id']:notification(c,p['user_id'],user['id'],'like','liked your post',str(pid))
            return self.go('/')
        if path=='/comment':
            try:pid=int(data.get('post_id',''))
            except ValueError:return self.go('/')
            body=data.get('body','').strip()
            if not body or len(body)>1000:return self.html(shell('Comment','Comments must be 1–1,000 characters.',user),400)
            parent_id=None
            if data.get('parent_id'):
                try:parent_id=int(data.get('parent_id'))
                except ValueError:parent_id=None
            with connect() as c:
                post=c.execute('SELECT user_id FROM posts WHERE id=?',(pid,)).fetchone()
                if not post:return self.html(shell('Comment','Post not found.',user),404)
                if not can_view_post(c,user['id'],post['user_id']):return self.html(shell('Comment','This post is private or unavailable.',user),403)
                if parent_id:
                    parent=c.execute('SELECT * FROM comments WHERE id=? AND post_id=?',(parent_id,pid)).fetchone()
                    if not parent:return self.html(shell('Comment','Reply target not found.',user),404)
                    if parent['parent_id'] is not None:
                        return self.html(shell('Comment','Replies are only one level deep — reply to the top-level comment instead.',user),400)
                if c.execute('SELECT 1 FROM comments WHERE post_id=? AND user_id=? AND body=? AND created>?',(pid,user['id'],body,time.time()-60)).fetchone():
                    return self.html(shell('Comment','You just posted the same comment. Wait a minute before posting it again.',user),429)
                cur=c.execute('INSERT INTO comments(post_id,user_id,body,created,parent_id) VALUES(?,?,?,?,?)',(pid,user['id'],body,time.time(),parent_id))
                if post['user_id']!=user['id']:notification(c,post['user_id'],user['id'],'comment','commented on your post',str(pid))
                if parent_id:
                    prow=c.execute('SELECT user_id FROM comments WHERE id=?',(parent_id,)).fetchone()
                    if prow and prow['user_id']!=user['id'] and prow['user_id']!=post['user_id']:notification(c,prow['user_id'],user['id'],'comment','replied to your comment',str(pid))
            return self.go('/post/%s'%pid)
        if path=='/delete-comment':
            try:cid=int(data.get('comment_id',''))
            except ValueError:return self.go('/')
            with connect() as c:
                comment=c.execute('SELECT cm.id,cm.post_id,cm.user_id,cm.body,p.user_id AS post_owner FROM comments cm JOIN posts p ON p.id=cm.post_id WHERE cm.id=?',(cid,)).fetchone()
                if not comment:return self.go('/')
                if comment['user_id']!=user['id'] and comment['post_owner']!=user['id'] and not user['is_admin']:return self.html(shell('Comment','You cannot remove this comment.',user),403)
                audit_event(c,user['id'],'delete_comment','comment',cid,{'author_user_id':comment['user_id'],'post_id':comment['post_id'],'body':(comment['body'] or '')[:1000],'admin_action':bool(user['is_admin'] and comment['user_id']!=user['id'])})
                c.execute('DELETE FROM comments WHERE id=?',(cid,))
            return self.go('/post/%s'%comment['post_id'])
        if path=='/settings':
            private = 1 if data.get('private_account') == 'on' else 0
            with connect() as c:
                c.execute('INSERT INTO user_settings(user_id,private_account) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET private_account=excluded.private_account',(user['id'],private))
            return self.go('/settings')
        if path=='/block':
            try: target=int(data.get('user_id',''))
            except ValueError: return self.go('/settings')
            if target==user['id']: return self.html(shell('Block member','You cannot block yourself.',user),400)
            with connect() as c:
                if not user_by_id(c,target): return self.go('/settings')
                if data.get('action')=='unblock':
                    c.execute('DELETE FROM blocks WHERE blocker_id=? AND blocked_id=?',(user['id'],target))
                else:
                    c.execute('INSERT OR IGNORE INTO blocks(blocker_id,blocked_id,created) VALUES(?,?,?)',(user['id'],target,time.time()))
                    c.execute('DELETE FROM friendships WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)',(user['id'],target,target,user['id']))
            return self.go('/settings')
        if path=='/profile':
            dn=data.get('display_name','').strip();bio=data.get('bio','').strip();interests=data.get('interests','').strip()
            if not 1<=len(dn)<=60 or len(bio)>240 or len(interests)>240:return self.html(shell('Profile','Name 1–60 characters; bio and interests up to 240.',user),400)
            try:up=self.save_upload(user,data,files,'avatar','avatar')
            except ValueError as e:return self.html(shell('Profile',esc(e),user),400)
            with connect() as c:
                old=c.execute('SELECT avatar_key FROM users WHERE id=?',(user['id'],)).fetchone()['avatar_key'];key=up[0] if up else old
                c.execute('UPDATE users SET display_name=?,bio=?,interests=?,avatar_key=? WHERE id=?',(dn,bio,interests,key,user['id']))
                if old and key!=old and not c.execute('SELECT 1 FROM posts WHERE media_key=?',(old,)).fetchone():c.execute('DELETE FROM uploads WHERE key=?',(old,));(UPLOAD_DIR/old).unlink(missing_ok=True)
            return self.go('/profile')
        if path=='/friend':
            try:target=int(data.get('user_id',''))
            except ValueError:return self.go('/people')
            action=data.get('action')
            if target==user['id']:return self.html(shell('Friend request','Cannot send a request to yourself.',user),400)
            with connect() as c:
                if not user_by_id(c,target):return self.html(shell('Friend request','Member not found.',user),404)
                rel=relationship(c,user['id'],target);now=time.time()
                blocked=c.execute('SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)',(user['id'],target,target,user['id'])).fetchone()
                if action=='request' and not rel and not blocked:
                    rid=uuid.uuid4().hex;c.execute("INSERT INTO friendships(id,requester_id,addressee_id,status,created) VALUES(?,?,?,'pending',?)",(rid,user['id'],target,now));notification(c,target,user['id'],'friend_request','sent you a friend request',rid)
                elif action=='accept' and rel and rel['status']=='pending' and rel['addressee_id']==user['id'] and not blocked:
                    c.execute("UPDATE friendships SET status='accepted' WHERE id=?",(rel['id'],));notification(c,rel['requester_id'],user['id'],'friend_accept','accepted your friend request',rel['id'])
                elif action=='decline' and rel and rel['status']=='pending' and rel['addressee_id']==user['id']:
                    c.execute('DELETE FROM friendships WHERE id=?',(rel['id'],))
                elif action=='cancel' and rel and rel['status']=='pending' and rel['requester_id']==user['id']:
                    c.execute('DELETE FROM friendships WHERE id=?',(rel['id'],))
                elif action=='remove' and rel:c.execute('DELETE FROM friendships WHERE id=?',(rel['id'],))
            return self.go('/friends')
        if path=='/message':
            try:target=int(data.get('to',''))
            except ValueError:return self.go('/messages')
            body=data.get('body','').strip()
            if not body or len(body)>2000:return self.html(shell('Message','Message must be 1–2,000 characters.',user),400)
            with connect() as c:
                if not are_friends(c,user['id'],target):return self.html(shell('Message','You can message accepted friends only.',user),403)
                mid=uuid.uuid4().hex;c.execute('INSERT INTO messages(id,sender_id,recipient_id,body,created) VALUES(?,?,?,?,?)',(mid,user['id'],target,body,time.time()));notification(c,target,user['id'],'message','sent you a message',mid)
            return self.go('/messages?with=%s'%target)
        if path=='/react':
            try:pid=int(data.get('post_id',''))
            except ValueError:return self.go('/')
            kind=data.get('kind','')
            if kind not in REACTION_KINDS:return self.go('/')
            with connect() as c:
                p=c.execute('SELECT user_id FROM posts WHERE id=?',(pid,)).fetchone()
                if not p:return self.go('/')
                if not can_view_post(c,user['id'],p['user_id']):return self.html(shell('Reaction','This post is private or unavailable.',user),403)
                if kind=='nod':
                    # Nod stays on the legacy likes table (backward compatible).
                    row=c.execute('SELECT 1 FROM likes WHERE post_id=? AND user_id=?',(pid,user['id'])).fetchone()
                    if row:c.execute('DELETE FROM likes WHERE post_id=? AND user_id=?',(pid,user['id']))
                    else:
                        c.execute('INSERT OR IGNORE INTO likes(post_id,user_id,created) VALUES(?,?,?)',(pid,user['id'],time.time()))
                        if p['user_id']!=user['id']:notification(c,p['user_id'],user['id'],'like','liked your post',str(pid))
                    c.execute('DELETE FROM reactions WHERE post_id=? AND user_id=?',(pid,user['id']))
                else:
                    existing=c.execute('SELECT kind FROM reactions WHERE post_id=? AND user_id=?',(pid,user['id'])).fetchone()
                    c.execute('DELETE FROM likes WHERE post_id=? AND user_id=?',(pid,user['id']))
                    if existing and existing['kind']==kind:
                        c.execute('DELETE FROM reactions WHERE post_id=? AND user_id=?',(pid,user['id']))
                    else:
                        c.execute('INSERT INTO reactions(post_id,user_id,kind,created) VALUES(?,?,?,?) ON CONFLICT(post_id,user_id) DO UPDATE SET kind=excluded.kind,created=excluded.created',(pid,user['id'],kind,time.time()))
                        if p['user_id']!=user['id']:notification(c,p['user_id'],user['id'],'reaction','reacted %s to your post'%REACTION_LABELS[kind],str(pid))
            return self.go('/')
        if path=='/comment-react':
            try:cid=int(data.get('comment_id',''))
            except ValueError:return self.go('/')
            kind=data.get('kind','')
            if kind not in REACTION_KINDS:return self.go('/')
            with connect() as c:
                cm=c.execute('SELECT cm.*,p.user_id AS post_owner FROM comments cm JOIN posts p ON p.id=cm.post_id WHERE cm.id=?',(cid,)).fetchone()
                if not cm:return self.go('/')
                if not can_view_post(c,user['id'],cm['post_owner']):return self.html(shell('Reaction','This post is private or unavailable.',user),403)
                existing=c.execute('SELECT kind FROM comment_reactions WHERE comment_id=? AND user_id=?',(cid,user['id'])).fetchone()
                if existing and existing['kind']==kind:
                    c.execute('DELETE FROM comment_reactions WHERE comment_id=? AND user_id=?',(cid,user['id']))
                else:
                    c.execute('INSERT INTO comment_reactions(comment_id,user_id,kind,created) VALUES(?,?,?,?) ON CONFLICT(comment_id,user_id) DO UPDATE SET kind=excluded.kind,created=excluded.created',(cid,user['id'],kind,time.time()))
                    if cm['user_id']!=user['id']:notification(c,cm['user_id'],user['id'],'reaction','reacted %s to your comment'%REACTION_LABELS[kind],str(cm['post_id']))
            return self.go('/post/%s'%cm['post_id'])
        if path=='/edit-comment':
            try:cid=int(data.get('comment_id',''))
            except ValueError:return self.go('/')
            body=data.get('body','').strip()
            if not body or len(body)>1000:return self.html(shell('Comment','Comments must be 1–1,000 characters.',user),400)
            with connect() as c:
                cm=c.execute('SELECT * FROM comments WHERE id=?',(cid,)).fetchone()
                if not cm:return self.go('/')
                if cm['user_id']!=user['id']:return self.html(shell('Comment','You can edit only your own comments.',user),403)
                c.execute('UPDATE comments SET body=? WHERE id=?',(body,cid))
                audit_event(c,user['id'],'edit_comment','comment',cid,{'body':body[:200]})
            return self.go('/post/%s'%cm['post_id'])
        if path=='/repost':
            try:pid=int(data.get('post_id',''))
            except ValueError:return self.go('/')
            comment=data.get('comment','').strip()[:500]
            with connect() as c:
                orig=c.execute('SELECT * FROM posts WHERE id=?',(pid,)).fetchone()
                if not orig:return self.html(shell('Repost','Post not found.',user),404)
                if orig['is_hidden']:return self.html(shell('Repost','That post is hidden and cannot be reposted.',user),403)
                # Privacy inheritance: a private post must NOT become public
                # by being reposted — the viewer must be able to see the
                # original, and the repost is only shown to people who can
                # see the original (enforced again at render time).
                if not can_view_post(c,user['id'],orig['user_id']):return self.html(shell('Repost','This post is private or unavailable.',user),403)
                if orig['group_id']:return self.html(shell('Repost','Group posts cannot be reposted to the main feed.',user),400)
                cur=c.execute('INSERT INTO posts(user_id,body,created,original_post_id) VALUES(?,?,?,?)',(user['id'],comment,time.time(),pid))
                store_hashtags(c,cur.lastrowid,comment)
                if orig['user_id']!=user['id']:notification(c,orig['user_id'],user['id'],'repost','reposted your post',str(pid))
            return self.go('/')
        if path=='/save':
            try:pid=int(data.get('post_id',''))
            except ValueError:return self.go('/')
            with connect() as c:
                p=c.execute('SELECT user_id FROM posts WHERE id=?',(pid,)).fetchone()
                if not p:return self.go('/')
                if not can_view_post(c,user['id'],p['user_id']):return self.html(shell('Save','This post is private or unavailable.',user),403)
                if c.execute('SELECT 1 FROM saved_posts WHERE user_id=? AND post_id=?',(user['id'],pid)).fetchone():
                    c.execute('DELETE FROM saved_posts WHERE user_id=? AND post_id=?',(user['id'],pid))
                else:
                    c.execute('INSERT OR IGNORE INTO saved_posts(user_id,post_id,created) VALUES(?,?,?)',(user['id'],pid,time.time()))
            return self.go('/')
        if path=='/pin':
            try:pid=int(data.get('post_id',''))
            except ValueError:return self.go('/')
            with connect() as c:
                p=c.execute('SELECT * FROM posts WHERE id=?',(pid,)).fetchone()
                if not p:return self.go('/')
                if p['user_id']!=user['id']:return self.html(shell('Pin','You can pin only your own posts.',user),403)
                c.execute('UPDATE posts SET is_pinned=? WHERE id=?',(0 if p['is_pinned'] else 1,pid))
            return self.go('/member/%s'%user['id'])
        if path=='/poll':
            try:pid=int(data.get('post_id',''))
            except ValueError:return self.go('/')
            question=data.get('question','').strip()
            options=[o.strip() for o in data.get('options','').replace('\r','\n').split('\n') if o.strip()]
            if not question or len(question)>200 or not (2<=len(options)<=5) or any(len(o)>120 for o in options):
                return self.html(shell('Poll','A poll needs a question (up to 200 characters) and 2–5 options (up to 120 characters each).',user),400)
            with connect() as c:
                p=c.execute('SELECT user_id FROM posts WHERE id=?',(pid,)).fetchone()
                if not p:return self.html(shell('Poll','Post not found.',user),404)
                if p['user_id']!=user['id']:return self.html(shell('Poll','Only the post author can add a poll.',user),403)
                if c.execute('SELECT 1 FROM polls WHERE post_id=?',(pid,)).fetchone():return self.html(shell('Poll','This post already has a poll.',user),400)
                cur=c.execute('INSERT INTO polls(post_id,user_id,question,created) VALUES(?,?,?,?)',(pid,user['id'],question,time.time()))
                for o in options:c.execute('INSERT INTO poll_options(poll_id,text) VALUES(?,?)',(cur.lastrowid,o))
            return self.go('/post/%s'%pid)
        if path=='/poll-vote':
            try:poll_id=int(data.get('poll_id',''));opt_id=int(data.get('option_id',''))
            except ValueError:return self.go('/')
            with connect() as c:
                poll=c.execute('SELECT * FROM polls WHERE id=?',(poll_id,)).fetchone()
                if not poll:return self.go('/')
                opt=c.execute('SELECT * FROM poll_options WHERE id=? AND poll_id=?',(opt_id,poll_id)).fetchone()
                if not opt:return self.html(shell('Poll','That poll option does not exist.',user),400)
                post=c.execute('SELECT user_id FROM posts WHERE id=?',(poll['post_id'],)).fetchone()
                if post and not can_view_post(c,user['id'],post['user_id']):return self.html(shell('Poll','This post is private or unavailable.',user),403)
                c.execute('INSERT INTO poll_votes(poll_id,user_id,option_id,created) VALUES(?,?,?,?) ON CONFLICT(poll_id,user_id) DO UPDATE SET option_id=excluded.option_id,created=excluded.created',(poll_id,user['id'],opt_id,time.time()))
            return self.go('/post/%s'%poll['post_id'])
        if path=='/group':
            name=data.get('name','').strip();desc=data.get('description','').strip();vis=data.get('visibility','public')
            if not name or len(name)>80 or len(desc)>500 or vis not in ('public','private'):
                return self.html(shell('Group','Group name 1–80 characters, description up to 500, visibility public or private.',user),400)
            with connect() as c:
                cur=c.execute('INSERT INTO groups(name,description,visibility,owner_id,created) VALUES(?,?,?,?,?)',(name,desc,vis,user['id'],time.time()))
                gid=cur.lastrowid
                c.execute('INSERT INTO group_members(group_id,user_id,role,status,created) VALUES(?,?,?,\'accepted\',?)',(gid,user['id'],'owner',time.time()))
                audit_event(c,user['id'],'create_group','group',gid,{'name':name,'visibility':vis})
            return self.go('/group/%s'%gid)
        if path=='/group-action':
            try:gid=int(data.get('group_id',''))
            except ValueError:return self.go('/groups')
            action=data.get('action','')
            with connect() as c:
                g=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone()
                if not g:return self.go('/groups')
                mem=group_role(c,gid,user['id'])
                is_admin=g['owner_id']==user['id'] or (mem and mem['role'] in ('owner','admin') and mem['status']=='accepted')
                if action=='join' and g['visibility']=='public' and not mem:
                    c.execute('INSERT INTO group_members(group_id,user_id,role,status,created) VALUES(?,?,?,\'accepted\',?)',(gid,user['id'],'member',time.time()))
                elif action=='request' and g['visibility']=='private' and not mem:
                    c.execute('INSERT INTO group_members(group_id,user_id,role,status,created) VALUES(?,?,?,\'pending\',?)',(gid,user['id'],'member',time.time()))
                    for adm in c.execute("SELECT user_id FROM group_members WHERE group_id=? AND role IN ('owner','admin') AND status='accepted'",(gid,)).fetchall():
                        if adm['user_id']!=user['id']:notification(c,adm['user_id'],user['id'],'group_invite','requested to join %s'%g['name'],str(gid))
                elif action=='leave' and mem and g['owner_id']!=user['id']:
                    c.execute('DELETE FROM group_members WHERE group_id=? AND user_id=?',(gid,user['id']))
                elif action in ('approve','reject') and is_admin:
                    try:tid=int(data.get('user_id',''))
                    except ValueError:return self.go('/group/%s'%gid)
                    if action=='approve':
                        c.execute("UPDATE group_members SET status='accepted' WHERE group_id=? AND user_id=? AND status='pending'",(gid,tid))
                        notification(c,tid,user['id'],'group_invite','approved your request to join %s'%g['name'],str(gid))
                    else:
                        c.execute("DELETE FROM group_members WHERE group_id=? AND user_id=? AND status='pending'",(gid,tid))
                elif action in ('promote','demote') and is_admin:
                    try:tid=int(data.get('user_id',''))
                    except ValueError:return self.go('/group/%s'%gid)
                    if tid!=g['owner_id']:
                        c.execute("UPDATE group_members SET role=? WHERE group_id=? AND user_id=?",('admin' if action=='promote' else 'member',gid,tid))
            return self.go('/group/%s'%gid)
        if path=='/group-post':
            try:gid=int(data.get('group_id',''))
            except ValueError:return self.go('/groups')
            body=data.get('body','').strip()
            if not body or len(body)>2000:return self.html(shell('Group post','Write something (up to 2,000 characters).',user),400)
            with connect() as c:
                g=can_view_group(c,gid,user['id'])
                if not g:return self.html(shell('Group','You are not a member of this group.',user),403)
                cur=c.execute('INSERT INTO posts(user_id,body,created,group_id) VALUES(?,?,?,?)',(user['id'],body,time.time(),gid))
                store_hashtags(c,cur.lastrowid,body)
            return self.go('/group/%s'%gid)
        if path=='/event':
            title=data.get('title','').strip();desc=data.get('description','').strip();loc=data.get('location','').strip()
            try:starts=time.mktime(time.strptime(data.get('starts_at',''),'%Y-%m-%dT%H:%M'))
            except ValueError:return self.html(shell('Event','Choose a valid date and time for the event.',user),400)
            if not title or len(title)>120 or len(desc)>500 or len(loc)>120 or starts<=time.time():
                return self.html(shell('Event','Events need a title (up to 120 characters) and a future date and time.',user),400)
            gid=None
            if data.get('group_id'):
                try:gid=int(data.get('group_id'))
                except ValueError:gid=None
            with connect() as c:
                if gid and not can_view_group(c,gid,user['id']):return self.html(shell('Event','You are not a member of that group.',user),403)
                cur=c.execute('INSERT INTO events(group_id,user_id,title,description,location,starts_at,created) VALUES(?,?,?,?,?,?,?)',(gid,user['id'],title,desc,loc,starts,time.time()))
                c.execute('INSERT INTO event_rsvps(event_id,user_id,status,created) VALUES(?,?,\'going\',?)',(cur.lastrowid,user['id'],time.time()))
            return self.go('/events')
        if path=='/event-rsvp':
            try:eid=int(data.get('event_id',''))
            except ValueError:return self.go('/events')
            status=data.get('status','')
            if status not in ('going','maybe','not_going'):return self.go('/events')
            with connect() as c:
                ev=c.execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
                if not ev:return self.go('/events')
                if ev['group_id'] and not can_view_group(c,ev['group_id'],user['id']):return self.html(shell('Event','You are not a member of that group.',user),403)
                c.execute('INSERT INTO event_rsvps(event_id,user_id,status,created) VALUES(?,?,?,?) ON CONFLICT(event_id,user_id) DO UPDATE SET status=excluded.status,created=excluded.created',(eid,user['id'],status,time.time()))
            return self.go('/events')
        if path=='/event-invite':
            try:eid=int(data.get('event_id',''));fid=int(data.get('friend_id',''))
            except ValueError:return self.go('/events')
            with connect() as c:
                ev=c.execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
                if not ev:return self.go('/events')
                if not are_friends(c,user['id'],fid):return self.html(shell('Event','You can invite accepted friends only.',user),403)
                notification(c,fid,user['id'],'event_invite','invited you to %s'%ev['title'],str(eid))
            return self.go('/events')
        if path=='/report':
            target_type=data.get('target_type','');target_id=data.get('target_id','').strip();reason=data.get('reason','').strip();details=data.get('details','').strip()
            if target_type not in ('post','comment','user','message') or not target_id or not reason or len(reason)>80 or len(details)>500:
                return self.html(shell('Report','Choose what you are reporting and a reason (up to 80 characters).',user),400)
            with connect() as c:
                exists=False
                if target_type=='post':exists=bool(c.execute('SELECT 1 FROM posts WHERE id=?',(target_id,)).fetchone()) if target_id.isdigit() else False
                elif target_type=='comment':exists=bool(c.execute('SELECT 1 FROM comments WHERE id=?',(target_id,)).fetchone()) if target_id.isdigit() else False
                elif target_type=='user':exists=bool(user_by_id(c,int(target_id))) if target_id.isdigit() else False
                elif target_type=='message':exists=bool(c.execute('SELECT 1 FROM messages WHERE id=?',(target_id,)).fetchone())
                if not exists:return self.html(shell('Report','That post, comment, user or message does not exist.',user),404)
                if c.execute('SELECT 1 FROM reports WHERE reporter_id=? AND target_type=? AND target_id=? AND status=\'open\'',(user['id'],target_type,target_id)).fetchone():
                    return self.html(shell('Report','You have already reported this. An admin will review it.',user),400)
                c.execute('INSERT INTO reports(reporter_id,target_type,target_id,reason,details,status,created,updated) VALUES(?,?,?,?,?,\'open\',?,?)',(user['id'],target_type,target_id,reason,details,time.time(),time.time()))
                audit_event(c,user['id'],'report','report',None,{'target_type':target_type,'target_id':target_id,'reason':reason})
            return self.html(shell('Report','<section class="card"><h1>Report sent</h1><p>Thanks — an admin will review it. You will be notified of the outcome.</p><p><a class="btn" href="/">Back to feed</a></p></section>',user))
        if path=='/admin/report-action':
            if not user['is_admin']:return self.html(shell('Admin','<section class="card"><h1>Admin access only</h1></section>',user),403)
            try:rid=int(data.get('report_id',''))
            except ValueError:return self.go('/admin/moderation')
            action=data.get('action','')
            if action not in ('hide','remove','warn','dismiss'):return self.go('/admin/moderation')
            with connect() as c:
                rep=c.execute('SELECT * FROM reports WHERE id=?',(rid,)).fetchone()
                if not rep or rep['status']!='open':return self.go('/admin/moderation')
                target_owner=None
                if rep['target_type']=='post' and rep['target_id'].isdigit():
                    row=c.execute('SELECT user_id FROM posts WHERE id=?',(int(rep['target_id']),)).fetchone()
                    target_owner=row['user_id'] if row else None
                    if action=='hide' and row:c.execute('UPDATE posts SET is_hidden=1 WHERE id=?',(int(rep['target_id']),))
                    if action=='remove' and row:c.execute('DELETE FROM posts WHERE id=?',(int(rep['target_id']),))
                elif rep['target_type']=='comment' and rep['target_id'].isdigit():
                    row=c.execute('SELECT user_id FROM comments WHERE id=?',(int(rep['target_id']),)).fetchone()
                    target_owner=row['user_id'] if row else None
                    if action in ('hide','remove') and row:c.execute('DELETE FROM comments WHERE id=?',(int(rep['target_id']),))
                elif rep['target_type']=='user' and rep['target_id'].isdigit():
                    target_owner=int(rep['target_id'])
                elif rep['target_type']=='message':
                    row=c.execute('SELECT sender_id FROM messages WHERE id=?',(rep['target_id'],)).fetchone()
                    target_owner=row['sender_id'] if row else None
                    if action in ('hide','remove') and row:c.execute('DELETE FROM messages WHERE id=?',(rep['target_id'],))
                if action=='warn' and target_owner and target_owner!=user['id']:
                    notification(c,target_owner,user['id'],'report_update','An admin reviewed a report about your content and issued a warning.',str(rid))
                c.execute("UPDATE reports SET status='actioned',updated=? WHERE id=?",(time.time(),rid))
                if target_owner:notification(c,rep['reporter_id'],user['id'],'report_update','Your report #%s was reviewed: %s.'%(rid,action),str(rid))
                audit_event(c,user['id'],'moderate_report','report',rid,{'action':action,'target_type':rep['target_type'],'target_id':rep['target_id']})
            return self.go('/admin/moderation')
        if path=='/notification-prefs':
            with connect() as c:
                for kind in NOTIF_KINDS:
                    enabled=1 if data.get('pref_'+kind)=='on' else 0
                    c.execute('INSERT INTO notification_prefs(user_id,kind,enabled) VALUES(?,?,?) ON CONFLICT(user_id,kind) DO UPDATE SET enabled=excluded.enabled',(user['id'],kind,enabled))
            return self.go('/settings')
        if path=='/notifications/read-all':
            with connect() as c:c.execute('UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL',(time.time(),user['id']))
            return self.go('/notifications')
        if path=='/account/delete':
            if user['is_admin']:return self.html(shell('Delete account','Admin accounts cannot be deleted here.',user),403)
            confirm_email=normalize_email(data.get('confirm_email',''))
            pwd=data.get('password','')
            if confirm_email!=user['email'].lower() or not check_password(pwd,user['password_hash']):
                return self.html(shell('Delete account','To delete your account, type your exact email address and enter your current password.',user),400)
            backup_path=backup_database()
            with connect() as c:
                target=user['id']
                def rows(table,where,args):
                    return [dict(r) for r in c.execute('SELECT * FROM %s WHERE %s'%(table,where),args).fetchall()]
                saved={'user':dict(user_by_id(c,target)),'settings':rows('user_settings','user_id=?',(target,)),'posts':rows('posts','user_id=?',(target,)),'comments':rows('comments','user_id=?',(target,)),'likes':rows('likes','user_id=?',(target,)),'reactions':rows('reactions','user_id=?',(target,)),'friendships':rows('friendships','requester_id=? OR addressee_id=?',(target,target)),'messages':rows('messages','sender_id=? OR recipient_id=?',(target,target)),'notifications':rows('notifications','user_id=? OR actor_id=?',(target,target)),'uploads':rows('uploads','user_id=?',(target,)),'blocks':rows('blocks','blocker_id=? OR blocked_id=?',(target,target)),'saved_posts':rows('saved_posts','user_id=?',(target,)),'group_members':rows('group_members','user_id=?',(target,)),'poll_votes':rows('poll_votes','user_id=?',(target,))}
                keys=[r['key'] for r in c.execute('SELECT key FROM uploads WHERE user_id=?',(target,)).fetchall()]
                now=time.time()
                c.execute('INSERT INTO removed_users(original_user_id,name,email,password_hash,is_admin,joined,data_json,backup_file,removed_at,removed_by,restore_deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(target,display(user),user['email'],user['password_hash'],0,user['joined'],json.dumps(saved,ensure_ascii=False),backup_path.name if backup_path else None,now,target,now+REMOVED_RETENTION_DAYS*86400))
                audit_event(c,target,'self_delete','user',target,{'name':display(user),'email':user['email']})
                c.execute('DELETE FROM users WHERE id=?',(target,))
            for key in keys:
                src=UPLOAD_DIR/key
                if src.is_file():shutil.move(str(src),str(REMOVED_DIR/key))
            return self.go('/','network_session=; HttpOnly; %sSameSite=Lax; Path=/; Max-Age=0; Priority=High'%self.secure_cookie())
        return self.html(shell('Not found','Page not found.',user),404)


class NetworkHTTPServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    request_queue_size=128


def main():
    if not 1<=PORT<=65535:raise ValueError('PORT must be between 1 and 65535.')
    init_db()
    with connect() as c:
        check=c.execute('PRAGMA quick_check').fetchone()[0]
        if check!='ok':raise RuntimeError('SQLite integrity check failed: %s'%check)
    backup_database()
    threading.Thread(target=backup_worker,name='database-backups',daemon=True).start()
    if os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'):
        print('Password reset email delivery is enabled.')
    else:
        print('Password reset email is not configured. Set SMTP_HOST and SMTP_FROM on the server.')
    if PRODUCTION and not os.environ.get('DATA_DIR'):
        print('WARNING: DATA_DIR is not explicitly configured. Use a persistent disk path in production.')
    if PRODUCTION and not shutil.which('ffprobe'):
        print('WARNING: ffprobe is unavailable. Production video uploads will be rejected until ffprobe is installed.')
    url='http://127.0.0.1:%s'%PORT
    print('The Quad is listening on %s:%s'%(HOST,PORT))
    print('Mode:','production' if PRODUCTION else 'development')
    if not HOSTED:print('Open it on this device:',url)
    else:print('Open the HTTPS URL provided by your host.')
    print('Database:',DB_PATH)
    print('Photos/videos:',UPLOAD_DIR)
    srv=NetworkHTTPServer((HOST,PORT),Handler)
    def request_shutdown(signum=None,frame=None):
        print('Shutdown requested; finishing active requests.')
        threading.Thread(target=srv.shutdown,daemon=True).start()
    for sig_name in ('SIGTERM','SIGINT'):
        sig=getattr(signal,sig_name,None)
        if sig is not None:
            try:signal.signal(sig,request_shutdown)
            except (ValueError,OSError):pass
    def open_browser():
        try:
            opened=webbrowser.open(url,new=2)
            if not opened:print('Open %s in Safari.'%url)
        except Exception as exc:print('Automatic browser launch is unavailable (%s). Open %s in Safari.'%(exc,url))
    if not HOSTED:
        timer=threading.Timer(1.0,open_browser);timer.daemon=True;timer.start()
    try:srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close()
        backup_database()
        print('Server stopped cleanly.')

if __name__=='__main__':main()

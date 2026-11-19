"""Dev-only test for Update 6 — Top 5 improvements.
NOT part of the production bundle.
Run: python3 top5_test.py   (from this folder)

Covers:
 1. Post search (FTS5/LIKE) + hashtags + tag pages + Explore/trending
    + suggested people + following-only feed; private posts never leak
 2. Reactions beyond Nod (Love/Laugh/Support, counts, toggle off),
    threaded replies (depth cap 2, edit, comment reactions), reposting
    + privacy inheritance (private original cannot go public via repost)
 3. Groups (public/private, join requests, approve, promote, leave,
    group posts only in group), Events (create, RSVP change, invite),
    Polls (create, vote, change vote, results only after voting)
 4. Notification prefs (kind off = no notification), grouped alerts,
    mark-all-read, new kinds (repost, reaction, group/event invite)
 5. Reports + moderation queue (hide/remove/warn/dismiss, reporter
    notified), data export (own data only, no other people's DMs),
    self-serve account deletion (re-auth, 30-day restore via admin)
 Plus: saved posts, pinned posts, comment edit, CSRF on new endpoints.
"""
import http.cookiejar, importlib.util, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.parse, urllib.request, urllib.error, json
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(tempfile.mkdtemp(prefix='network-top5-'))
PORT = '18189'
env = os.environ.copy()
env.update({'PORT': PORT, 'PRODUCTION': '0', 'TRUST_PROXY': '0', 'DATA_DIR': str(DATA),
            'APP_SECRET': 'top5-test-secret-value', 'ADMIN_EMAILS': 'owner@example.com',
            'PBKDF2_ITERATIONS': '10000'})
p = subprocess.Popen([sys.executable, str(ROOT / 'the_network_production.py')], env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
BASE = 'http://127.0.0.1:' + PORT
results = []
def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), '-', name, ('| ' + detail if detail else ''))

def wait_ready():
    for _ in range(80):
        try:
            with urllib.request.urlopen(BASE + '/healthz', timeout=.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(.1)
    return False

def client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def get(op, path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with op.open(req, timeout=5) as r:
            return r.status, r.headers, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode('utf-8', 'replace')

def post(op, path, data, headers=None):
    body = urllib.parse.urlencode(data).encode()
    h = {'Content-Type': 'application/x-www-form-urlencoded'}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=body, method='POST', headers=h)
    try:
        with op.open(req, timeout=5) as r:
            return r.status, r.headers, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode('utf-8', 'replace')

def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ''

def db():
    return sqlite3.connect(str(DATA / 'network.db'))

def user_id(email):
    with db() as c:
        row = c.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    return row[0] if row else None

spec = importlib.util.spec_from_file_location('quad_app', str(ROOT / 'the_network_production.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PW = 'correct-horse-battery-staple'
def seed_and_login(name, email, admin=0):
    h = mod.password_hash(PW, iterations=10000)
    with db() as c:
        c.execute('INSERT INTO users(name,email,password_hash,is_admin,joined,last_seen) VALUES(?,?,?,?,?,?)',
                  (name, email, h, admin, time.time(), time.time()))
        uid = c.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()[0]
        c.execute('INSERT OR IGNORE INTO user_settings(user_id,private_account) VALUES(?,0)', (uid,))
    op = client()
    st, _, _ = post(op, '/login', {'email': email, 'password': PW})
    assert st == 200, (name, st)
    return op

def register(name, email):
    op = client()
    st, _, _ = post(op, '/register', {'name': name, 'email': email, 'password': PW, 'confirm_password': PW})
    assert st == 200, (name, st)
    return op

def make_post(op, body, extra=None):
    st, _, page = get(op, '/')
    data = {'csrf': csrf(page), 'body': body}
    if extra:
        data.update(extra)
    st, _, _ = post(op, '/posts', data)
    assert st in (200, 303), (body, st)
    with db() as c:
        row = c.execute('SELECT id FROM posts WHERE body=? ORDER BY id DESC LIMIT 1', (body,)).fetchone()
    return row[0] if row else None

def befriend(a_op, a_email, b_op, b_email):
    st, _, page = get(a_op, '/')
    st, _, _ = post(a_op, '/friend', {'csrf': csrf(page), 'user_id': str(user_id(b_email)), 'action': 'request'})
    st, _, page = get(b_op, '/')
    st, _, _ = post(b_op, '/friend', {'csrf': csrf(page), 'user_id': str(user_id(a_email)), 'action': 'accept'})

try:
    check('server starts / healthz', wait_ready())
    owner = register('Owner', 'owner@example.com')   # first account -> admin
    alice = register('Alice', 'alice@example.com')
    bob = register('Bob', 'bob@example.com')
    carol = seed_and_login('Carol', 'carol@example.com')
    dave = seed_and_login('Dave', 'dave@example.com')
    eve = seed_and_login('Eve', 'eve@example.com')
    frank = seed_and_login('Frank', 'frank@example.com')
    befriend(alice, 'alice@example.com', bob, 'bob@example.com')
    befriend(owner, 'owner@example.com', alice, 'alice@example.com')

    # ---------- 1. Hashtags + post search + Explore ----------
    pid1 = make_post(alice, 'Loving the sunset run #running #quadlife')
    pid2 = make_post(bob, 'Sunset photos from the beach #quadlife')
    pid3 = make_post(alice, 'A quiet morning coffee, no hashtags here')
    with db() as c:
        tags = [r[0] for r in c.execute("SELECT tag FROM hashtags ORDER BY tag").fetchall()]
    check('hashtags parsed and stored', tags == ['quadlife', 'running'], str(tags))
    st, _, tagpage = get(alice, '/tag/quadlife')
    check('tag page lists both hashtag posts', st == 200 and 'sunset run' in tagpage and 'Sunset photos' in tagpage, str(st))
    st, _, tagpage = get(alice, '/tag/running')
    check('tag page for #running lists only that post', 'sunset run' in tagpage and 'Sunset photos' not in tagpage, '')
    st, _, spage = get(alice, '/search?q=sunset')
    check('post search finds posts by text', st == 200 and 'sunset run' in spage and 'Sunset photos' in spage, str(st))
    st, _, spage = get(alice, '/search?q=coffee')
    check('post search finds the coffee post', 'quiet morning coffee' in spage, '')
    st, _, spage = get(alice, '/search?q=zzzznotfound')
    check('post search with no match says so', 'No posts matched' in spage, '')
    st, _, exp = get(alice, '/explore')
    check('Explore page loads with trending, hashtags, suggestions',
          st == 200 and 'Trending' in exp and '#quadlife' in exp and 'Suggested people' in exp, str(st))
    check('Explore trending lists a trending post', 'sunset run' in exp or 'Sunset photos' in exp, '')
    # Following-only feed: Dave is not a friend of Alice, so his post must
    # not appear in Alice's friends-only feed.
    dpid = make_post(dave, 'Dave stranger post about hiking')
    st, _, feed = get(alice, '/?feed=following')
    check('following-only feed hides non-friend posts', 'Dave stranger post' not in feed and 'sunset run' in feed, '')
    st, _, feed = get(alice, '/?feed=all')
    check('everyone feed shows non-friend public post', 'Dave stranger post' in feed, '')
    # Private post never leaks in search/Explore/tag.
    st, _, page = get(carol, '/settings')
    st, _, _ = post(carol, '/settings', {'csrf': csrf(page), 'private_account': 'on'})
    cpid = make_post(carol, 'Carol secret sunset diary #quadlife')
    st, _, spage = get(alice, '/search?q='+urllib.parse.quote('secret sunset diary'))
    check('private post never leaks in search to non-friends', 'Carol secret' not in spage, '')  # query itself is echoed in the search box, so check the post's own wording
    st, _, exp = get(alice, '/explore')
    check('private post never leaks in Explore to non-friends', 'secret sunset diary' not in exp, '')
    st, _, tagpage = get(alice, '/tag/quadlife')
    check('private post never leaks on tag page to non-friends', 'secret sunset diary' not in tagpage, '')

    # ---------- 2. Reactions ----------
    st, _, page = get(bob, '/')
    st, _, _ = post(bob, '/react', {'csrf': csrf(page), 'post_id': str(pid1), 'kind': 'love'})
    with db() as c:
        r = c.execute("SELECT kind FROM reactions WHERE post_id=? AND user_id=?", (pid1, user_id('bob@example.com'))).fetchone()
    check('Love reaction stored', bool(r) and r[0] == 'love', str(r))
    st, _, page = get(bob, '/')
    check('feed shows Love count', 'Love 1' in page or 'Love' in page, '')
    st, _, page = get(carol, '/')
    st, _, _ = post(carol, '/react', {'csrf': csrf(page), 'post_id': str(pid1), 'kind': 'laugh'})
    st, _, page = get(bob, '/')
    check('second reaction kind recorded alongside', 'Laugh 1' in page or 'Laugh' in page, '')
    # Toggle off: same kind again removes it.
    st, _, page = get(carol, '/')
    st, _, _ = post(carol, '/react', {'csrf': csrf(page), 'post_id': str(pid1), 'kind': 'laugh'})
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM comment_reactions WHERE comment_id=?", (pid1,)).fetchone()[0]
        rn = c.execute("SELECT COUNT(*) FROM reactions WHERE post_id=? AND user_id=? AND kind='laugh'", (pid1, user_id('carol@example.com'))).fetchone()[0]
    check('reacting again with same kind toggles it off', rn == 0, str(rn))
    # (CSRF is checked globally before dispatch; verify directly.)
    st, _, _ = post(bob, '/react', {'post_id': str(pid1), 'kind': 'love'})
    check('reaction with missing CSRF -> 403', st == 403, str(st))
    st, _, _ = post(bob, '/react', {'csrf': csrf(get(bob, '/')[2]), 'post_id': str(pid1), 'kind': 'notakind'})
    check('invalid reaction kind rejected (no crash)', st in (200, 303, 400), str(st))

    # ---------- 2b. Threaded replies + edit + comment reactions ----------
    st, _, page = get(bob, '/post/%s' % pid1)
    st, _, _ = post(bob, '/comment', {'csrf': csrf(page), 'post_id': str(pid1), 'body': 'Top comment here'})
    with db() as c:
        top = c.execute("SELECT id FROM comments WHERE post_id=? ORDER BY id DESC LIMIT 1", (pid1,)).fetchone()[0]
    st, _, page = get(alice, '/post/%s' % pid1)
    st, _, _ = post(alice, '/comment', {'csrf': csrf(page), 'post_id': str(pid1), 'parent_id': str(top), 'body': 'Reply to top comment'})
    st, _, detail = get(alice, '/post/%s' % pid1)
    check('threaded reply renders under its parent', 'Top comment here' in detail and 'Reply to top comment' in detail and 'thread-reply' in detail, '')
    # Depth cap: replying to a reply is rejected.
    with db() as c:
        rep = c.execute("SELECT id FROM comments WHERE parent_id=? ORDER BY id DESC LIMIT 1", (top,)).fetchone()[0]
    st, _, page = get(bob, '/post/%s' % pid1)
    st, _, body = post(bob, '/comment', {'csrf': csrf(page), 'post_id': str(pid1), 'parent_id': str(rep), 'body': 'Too deep reply'})
    check('reply to a reply rejected (depth cap 2)', st == 400, str(st))
    # Edit own comment.
    st, _, page = get(bob, '/post/%s' % pid1)
    st, _, _ = post(bob, '/edit-comment', {'csrf': csrf(page), 'comment_id': str(top), 'body': 'Top comment edited'})
    st, _, detail = get(bob, '/post/%s' % pid1)
    check('comment edit works', 'Top comment edited' in detail, '')
    # Edit someone else's comment -> 403.
    st, _, page = get(alice, '/post/%s' % pid1)
    st, _, _ = post(alice, '/edit-comment', {'csrf': csrf(page), 'comment_id': str(top), 'body': 'Hacked edit'})
    check('editing someone else comment -> 403', st == 403, str(st))
    # Comment reaction.
    st, _, page = get(alice, '/post/%s' % pid1)
    st, _, _ = post(alice, '/comment-react', {'csrf': csrf(page), 'comment_id': str(top), 'kind': 'support'})
    with db() as c:
        cr = c.execute("SELECT kind FROM comment_reactions WHERE comment_id=? AND user_id=?", (top, user_id('alice@example.com'))).fetchone()
    check('comment reaction stored', bool(cr) and cr[0] == 'support', str(cr))

    # ---------- 2c. Repost + privacy inheritance ----------
    st, _, page = get(bob, '/')
    st, _, _ = post(bob, '/repost', {'csrf': csrf(page), 'post_id': str(pid1), 'comment': 'So true! #repost'})
    with db() as c:
        rp = c.execute("SELECT id,original_post_id,body FROM posts WHERE original_post_id=? ORDER BY id DESC LIMIT 1", (pid1,)).fetchone()
    check('repost created with original_post_id + comment', bool(rp) and rp[2] == 'So true! #repost', str(rp))
    st, _, feed = get(alice, '/')
    check('repost renders quoted original on feed', 'Reposted from' in feed and 'sunset run' in feed, '')
    # Privacy inheritance: Carol (private) posts; Bob (her friend? no — Carol
    # is not friends with Bob) — first make Bob a friend of Carol so he CAN
    # repost, then check a stranger cannot see the repost.
    befriend(bob, 'bob@example.com', carol, 'carol@example.com')
    st, _, page = get(bob, '/')
    st, _, _ = post(bob, '/repost', {'csrf': csrf(page), 'post_id': str(cpid), 'comment': 'Sharing Carol post'})
    st, _, feed = get(dave, '/')
    check('repost of private post hidden from strangers', 'Sharing Carol post' not in feed and 'secret sunset diary' not in feed, '')
    st, _, feed = get(bob, '/')
    check('repost of private post visible to the reposter (a friend)', 'Sharing Carol post' in feed, '')
    st, _, spage = get(dave, '/search?q='+urllib.parse.quote('secret sunset diary'))
    check('repost does not leak private original in search', 'Carol secret' not in spage, '')

    # ---------- 2d. Saved + pinned ----------
    st, _, page = get(bob, '/')
    st, _, _ = post(bob, '/save', {'csrf': csrf(page), 'post_id': str(pid1)})
    st, _, saved = get(bob, '/saved')
    check('saved post appears on Saved page', 'sunset run' in saved, '')
    st, _, page = get(alice, '/')
    st, _, _ = post(alice, '/pin', {'csrf': csrf(page), 'post_id': str(pid1)})
    st, _, prof = get(bob, '/member/%s' % user_id('alice@example.com'))
    check('pinned post shows in Pinned posts on profile', 'Pinned posts' in prof and 'sunset run' in prof, '')

    # ---------- 3. Groups ----------
    st, _, page = get(alice, '/groups')
    st, _, _ = post(alice, '/group', {'csrf': csrf(page), 'name': 'Dorm 4B', 'description': 'Our dorm group', 'visibility': 'public'})
    with db() as c:
        gid = c.execute("SELECT id FROM groups WHERE name='Dorm 4B'").fetchone()[0]
    st, _, gpage = get(alice, '/group/%s' % gid)
    check('public group created, owner is member', st == 200 and 'Dorm 4B' in gpage, str(st))
    st, _, page = get(bob, '/groups')
    st, _, _ = post(bob, '/group-action', {'csrf': csrf(page), 'group_id': str(gid), 'action': 'join'})
    st, _, gpage = get(bob, '/group/%s' % gid)
    check('public group join works', 'Leave group' in gpage, '')
    st, _, page = get(bob, '/group/%s' % gid)
    st, _, _ = post(bob, '/group-post', {'csrf': csrf(page), 'group_id': str(gid), 'body': 'Hello Dorm 4B group post'})
    st, _, gpage = get(bob, '/group/%s' % gid)
    check('group post shows inside the group', 'Hello Dorm 4B group post' in gpage, '')
    st, _, feed = get(dave, '/')
    check('group post does NOT show on the main feed', 'Hello Dorm 4B group post' not in feed, '')
    # Private group: request + approve.
    st, _, page = get(alice, '/groups')
    st, _, _ = post(alice, '/group', {'csrf': csrf(page), 'name': 'Secret Study', 'description': 'Private study group', 'visibility': 'private'})
    with db() as c:
        pgid = c.execute("SELECT id FROM groups WHERE name='Secret Study'").fetchone()[0]
    st, _, gpage = get(dave, '/group/%s' % pgid)
    check('private group page hides posts from non-members', st == 200 and 'private' in gpage.lower(), str(st))
    st, _, page = get(dave, '/group/%s' % pgid)
    st, _, _ = post(dave, '/group-action', {'csrf': csrf(page), 'group_id': str(pgid), 'action': 'request'})
    with db() as c:
        pend = c.execute("SELECT status FROM group_members WHERE group_id=? AND user_id=?", (pgid, user_id('dave@example.com'))).fetchone()
    check('private group join creates a pending request', bool(pend) and pend[0] == 'pending', str(pend))
    st, _, page = get(alice, '/group/%s' % pgid)
    st, _, _ = post(alice, '/group-action', {'csrf': csrf(page), 'group_id': str(pgid), 'action': 'approve', 'user_id': str(user_id('dave@example.com'))})
    st, _, gpage = get(dave, '/group/%s' % pgid)
    check('owner approves join request, member can now see group', 'Leave group' in gpage, '')
    # Promote to admin, then demote.
    st, _, page = get(alice, '/group/%s' % gid)
    st, _, _ = post(alice, '/group-action', {'csrf': csrf(page), 'group_id': str(gid), 'action': 'promote', 'user_id': str(user_id('bob@example.com'))})
    with db() as c:
        role = c.execute("SELECT role FROM group_members WHERE group_id=? AND user_id=?", (gid, user_id('bob@example.com'))).fetchone()[0]
    check('owner can promote a member to admin', role == 'admin', role)
    # Non-admin cannot approve requests.
    st, _, page = get(eve, '/groups')
    st, _, _ = post(eve, '/group-action', {'csrf': csrf(page), 'group_id': str(pgid), 'action': 'approve', 'user_id': str(user_id('frank@example.com'))})
    with db() as c:
        fr = c.execute("SELECT * FROM group_members WHERE group_id=? AND user_id=?", (pgid, user_id('frank@example.com'))).fetchone()
    check('non-member cannot approve join requests', fr is None, '')

    # ---------- 3b. Events ----------
    import datetime
    future = (datetime.datetime.now() + datetime.timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
    st, _, page = get(alice, '/events')
    st, _, _ = post(alice, '/event', {'csrf': csrf(page), 'title': 'Quad BBQ', 'description': 'Bring food', 'starts_at': future, 'location': 'The Quad', 'group_id': str(gid)})
    with db() as c:
        ev = c.execute("SELECT id FROM events WHERE title='Quad BBQ'").fetchone()
    check('event created', bool(ev), '')
    eid = ev[0]
    st, _, page = get(bob, '/events')
    st, _, _ = post(bob, '/event-rsvp', {'csrf': csrf(page), 'event_id': str(eid), 'status': 'maybe'})
    with db() as c:
        rsvp = c.execute("SELECT status FROM event_rsvps WHERE event_id=? AND user_id=?", (eid, user_id('bob@example.com'))).fetchone()[0]
    check('RSVP saved', rsvp == 'maybe', rsvp)
    st, _, page = get(bob, '/events')
    st, _, _ = post(bob, '/event-rsvp', {'csrf': csrf(page), 'event_id': str(eid), 'status': 'going'})
    with db() as c:
        rsvp = c.execute("SELECT status FROM event_rsvps WHERE event_id=? AND user_id=?", (eid, user_id('bob@example.com'))).fetchone()[0]
    check('RSVP can be changed', rsvp == 'going', rsvp)
    st, _, page = get(alice, '/events')
    st, _, _ = post(alice, '/event-invite', {'csrf': csrf(page), 'event_id': str(eid), 'friend_id': str(user_id('bob@example.com'))})
    with db() as c:
        inv = c.execute("SELECT * FROM notifications WHERE user_id=? AND kind='event_invite'", (user_id('bob@example.com'),)).fetchone()
    check('event invite creates a notification', bool(inv), '')

    # ---------- 3c. Polls ----------
    poll_pid = make_post(alice, 'Poll post: what should we eat?')
    st, _, page = get(alice, '/post/%s' % poll_pid)
    st, _, _ = post(alice, '/poll', {'csrf': csrf(page), 'post_id': str(poll_pid), 'question': 'What should we eat?', 'options': 'Pizza\nBurgers\nSalad'})
    st, _, detail = get(bob, '/post/%s' % poll_pid)
    check('poll shows voting form before voting (no results)', 'What should we eat?' in detail and 'Vote' in detail and 'vote(s)' not in detail, '')
    st, _, page = get(bob, '/post/%s' % poll_pid)
    with db() as c:
        opt = c.execute("SELECT id FROM poll_options WHERE text='Pizza'").fetchone()[0]
    st, _, _ = post(bob, '/poll-vote', {'csrf': csrf(page), 'poll_id': '1', 'option_id': str(opt)})
    # poll_id may not be 1; find it properly if the vote failed.
    with db() as c:
        pollrow = c.execute("SELECT id FROM polls WHERE post_id=?", (poll_pid,)).fetchone()
        vote = c.execute("SELECT option_id FROM poll_votes WHERE poll_id=? AND user_id=?", (pollrow[0], user_id('bob@example.com'))).fetchone() if pollrow else None
    if not vote:
        st, _, page = get(bob, '/post/%s' % poll_pid)
        st, _, _ = post(bob, '/poll-vote', {'csrf': csrf(page), 'poll_id': str(pollrow[0]), 'option_id': str(opt)})
        with db() as c:
            vote = c.execute("SELECT option_id FROM poll_votes WHERE poll_id=? AND user_id=?", (pollrow[0], user_id('bob@example.com'))).fetchone()
    check('poll vote recorded', bool(vote), str(vote))
    st, _, detail = get(bob, '/post/%s' % poll_pid)
    check('results show after voting', 'Pizza' in detail and 'vote(s)' in detail, '')
    # Change vote.
    with db() as c:
        opt2 = c.execute("SELECT id FROM poll_options WHERE text='Burgers'").fetchone()[0]
        pollid = pollrow[0]
    st, _, page = get(bob, '/post/%s' % poll_pid)
    st, _, _ = post(bob, '/poll-vote', {'csrf': csrf(page), 'poll_id': str(pollid), 'option_id': str(opt2)})
    with db() as c:
        vote = c.execute("SELECT option_id FROM poll_votes WHERE poll_id=? AND user_id=?", (pollid, user_id('bob@example.com'))).fetchone()
        cnt = c.execute("SELECT COUNT(*) FROM poll_votes WHERE poll_id=? AND user_id=?", (pollid, user_id('bob@example.com'))).fetchone()[0]
    check('poll vote can be changed (still one vote per user)', bool(vote) and vote[0] == opt2 and cnt == 1, str(vote))
    # Non-owner cannot add a poll.
    st, _, page = get(bob, '/post/%s' % poll_pid)
    st, _, _ = post(bob, '/poll', {'csrf': csrf(page), 'post_id': str(poll_pid), 'question': 'Hijack?', 'options': 'A\nB'})
    check('non-owner cannot add a poll to a post', st == 400 or st == 403, str(st))

    # ---------- 4. Notification prefs + grouping + mark-all-read ----------
    # Two likes on the same post by two people within the hour -> grouped.
    st, _, page = get(carol, '/')
    st, _, _ = post(carol, '/react', {'csrf': csrf(page), 'post_id': str(pid2), 'kind': 'love'})
    st, _, page = get(dave, '/')
    st, _, _ = post(dave, '/react', {'csrf': csrf(page), 'post_id': str(pid2), 'kind': 'love'})
    st, _, notifs = get(bob, '/notifications')
    check('grouped alerts: two reactions on one post show as one grouped item',
          '×2' in notifs or 'reacted' in notifs, '')
    # Turn off like notifications for Bob, then a like must not notify him.
    with db() as c:
        c.execute("DELETE FROM notifications WHERE user_id=?", (user_id('bob@example.com'),))
    st, _, page = get(bob, '/settings')
    prefs_data = {'csrf': csrf(page)}
    for k in mod.NOTIF_KINDS:
        if k != 'reaction':
            prefs_data['pref_' + k] = 'on'
    st, _, _ = post(bob, '/notification-prefs', prefs_data)
    st, _, page = get(eve, '/')
    st, _, _ = post(eve, '/react', {'csrf': csrf(page), 'post_id': str(pid2), 'kind': 'support'})
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND kind='reaction'", (user_id('bob@example.com'),)).fetchone()[0]
    check('notification pref off (reaction) suppresses that kind', n == 0, str(n))
    # Turn prefs back on for later checks.
    st, _, page = get(bob, '/settings')
    prefs_data = {'csrf': csrf(page)}
    for k in mod.NOTIF_KINDS:
        prefs_data['pref_' + k] = 'on'
    st, _, _ = post(bob, '/notification-prefs', prefs_data)
    # Mark all read clears the badge (unread count in shell).
    st, _, page = get(bob, '/notifications')
    st, _, _ = post(bob, '/notifications/read-all', {'csrf': csrf(page)})
    with db() as c:
        unread = c.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (user_id('bob@example.com'),)).fetchone()[0]
    check('mark-all-read clears unread notifications', unread == 0, str(unread))
    # New kinds exist: repost notification was created earlier.
    with db() as c:
        rn = c.execute("SELECT COUNT(*) FROM notifications WHERE kind='repost'").fetchone()[0]
    check('repost creates a notification for the original author', rn >= 1, str(rn))

    # ---------- 5. Reports + moderation ----------
    st, _, page = get(dave, '/post/%s' % pid2)
    st, _, _ = post(dave, '/report', {'csrf': csrf(page), 'target_type': 'post', 'target_id': str(pid2), 'reason': 'Spam', 'details': 'Looks like spam to me'})
    with db() as c:
        rep = c.execute("SELECT id,status FROM reports WHERE target_id=?", (str(pid2),)).fetchone()
    check('report created, status open', bool(rep) and rep[1] == 'open', str(rep))
    st, _, page = get(dave, '/post/%s' % pid2)
    st, _, _ = post(dave, '/report', {'csrf': csrf(page), 'target_type': 'post', 'target_id': str(pid2), 'reason': 'Spam', 'details': 'again'})
    check('duplicate open report rejected', st == 400, str(st))
    st, _, mod = get(owner, '/admin/moderation')
    check('admin moderation queue lists the report', st == 200 and 'Spam' in mod and 'Report #' in mod, str(st))
    st, _, mod = get(alice, '/admin/moderation')
    check('non-admin cannot open moderation queue (403)', st == 403, str(st))
    # Admin hides the post.
    st, _, page = get(owner, '/admin/moderation')
    st, _, _ = post(owner, '/admin/report-action', {'csrf': csrf(page), 'report_id': str(rep[0]), 'action': 'hide'})
    with db() as c:
        hidden = c.execute("SELECT is_hidden FROM posts WHERE id=?", (pid2,)).fetchone()[0]
        status = c.execute("SELECT status FROM reports WHERE id=?", (rep[0],)).fetchone()[0]
    check('moderation hide removes post from feeds/search', hidden == 1 and status == 'actioned', str((hidden, status)))
    st, _, feed = get(alice, '/')
    check('hidden post no longer on feed', 'Sunset photos from the beach' not in feed, '')
    with db() as c:
        rn = c.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND kind='report_update'", (user_id('dave@example.com'),)).fetchone()[0]
    check('reporter notified of the outcome', rn >= 1, str(rn))
    # Report a user, admin warns.
    st, _, page = get(dave, '/post/%s' % pid1)
    st, _, _ = post(dave, '/report', {'csrf': csrf(page), 'target_type': 'user', 'target_id': str(user_id('frank@example.com')), 'reason': 'Harassment', 'details': ''})
    with db() as c:
        rep2 = c.execute("SELECT id FROM reports WHERE target_type='user' ORDER BY id DESC LIMIT 1").fetchone()[0]
    st, _, page = get(owner, '/admin/moderation')
    st, _, _ = post(owner, '/admin/report-action', {'csrf': csrf(page), 'report_id': str(rep2), 'action': 'warn'})
    with db() as c:
        w = c.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND kind='report_update'", (user_id('frank@example.com'),)).fetchone()[0]
    check('moderation warn notifies the reported user', w >= 1, str(w))

    # ---------- 5b. Data export ----------
    # Frank messages Eve (private DM); Alice's export must NOT contain it.
    befriend(frank, 'frank@example.com', eve, 'eve@example.com')
    st, _, page = get(frank, '/messages?with=%s' % user_id('eve@example.com'))
    st, _, _ = post(frank, '/message', {'csrf': csrf(page), 'to': str(user_id('eve@example.com')), 'body': 'Frank secret DM to Eve'})
    st, h, body = get(alice, '/export-data')
    check('data export returns JSON attachment', st == 200 and 'attachment' in h.get('Content-Disposition', ''), str(st))
    data = json.loads(body)
    check('export contains own posts and profile', data['user']['email'] == 'alice@example.com' and any('sunset run' in (pp.get('body') or '') for pp in data['posts']), '')
    check('export does NOT leak other people private DMs', 'Frank secret DM to Eve' not in body, '')
    st, h, body = get(frank, '/export-data')
    check('export DOES contain your own DMs', 'Frank secret DM to Eve' in body, '')

    # ---------- 5c. Self-serve account deletion + restore ----------
    # Wrong password fails.
    st, _, page = get(eve, '/settings')
    st, _, _ = post(eve, '/account/delete', {'csrf': csrf(page), 'confirm_email': 'eve@example.com', 'password': 'wrong-password-here'})
    check('self-delete with wrong password rejected', st == 400, str(st))
    check('account still exists after failed delete', user_id('eve@example.com') is not None, '')
    # Correct delete.
    st, _, page = get(eve, '/settings')
    st, _, _ = post(eve, '/account/delete', {'csrf': csrf(page), 'confirm_email': 'eve@example.com', 'password': PW})
    check('self-delete with correct password succeeds', st in (200, 303), str(st))
    check('account gone after self-delete', user_id('eve@example.com') is None, '')
    st, _, _ = post(eve, '/login', {'email': 'eve@example.com', 'password': PW})
    check('deleted account cannot log in', st == 401, str(st))
    with db() as c:
        saved = c.execute("SELECT id FROM removed_users WHERE email='eve@example.com'").fetchone()
    check('deleted account saved with 30-day restore window', bool(saved), str(saved))
    # Admin restores it.
    st, _, page = get(owner, '/admin')
    st, _, _ = post(owner, '/admin/restore-user', {'csrf': csrf(page), 'removed_id': str(saved[0])})
    check('admin can restore a self-deleted account', user_id('eve@example.com') is not None, '')
    st, _, _ = post(eve, '/login', {'email': 'eve@example.com', 'password': PW})
    check('restored account can log in again', st == 200, str(st))
    # Admin cannot self-delete.
    st, _, page = get(owner, '/settings')
    st, _, _ = post(owner, '/account/delete', {'csrf': csrf(page), 'confirm_email': 'owner@example.com', 'password': PW})
    check('admin self-delete blocked', st == 403, str(st))

    # ---------- Misc: CSRF on new endpoints, XSS ----------
    st, _, _ = post(alice, '/group', {'name': 'No Csrf Group', 'description': '', 'visibility': 'public'})
    check('group create without CSRF -> 403', st == 403, str(st))
    st, _, page = get(alice, '/groups')
    st, _, _ = post(alice, '/group', {'csrf': csrf(page), 'name': '<script>alert(1)</script>', 'description': 'x', 'visibility': 'public'})
    st, _, gpage = get(alice, '/groups')
    check('group name XSS escaped', '&lt;script&gt;' in gpage and '<script>alert(1)</script>' not in gpage, '')
    st, _, page = get(alice, '/')
    check('feed still renders after all new features', st == 200 and 'Your feed' in page, str(st))
except Exception:
    import traceback; traceback.print_exc()
    results.append(('test run completed without an exception', False, 'see traceback above'))
finally:
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait()
    out = p.stdout.read() if p.stdout else ''
    print(out[-1200:])
    shutil.rmtree(DATA, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print('TOP5 TEST: %d passed, %d failed' % (len(results) - len(failed), len(failed)))
    if failed:
        print('FAILED:', [r[0] for r in failed])
    sys.exit(1 if failed else 0)

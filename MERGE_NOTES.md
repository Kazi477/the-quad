# Merge notes — The Network + The Quad (DEV ONLY, 2026-11-19)

Dev copy only. Nothing here has been deployed. The live site was not touched.
Backup (do not modify): `~/workspace/network-merge/backup/the-network-production-bundle-2026-11-19.zip`
(md5 `aae87ec977f13b371de50aedb192cd96`, verified unchanged after the merge.)

## Branding choice
Product name stays **The Network**. "The Quad" is the design / community-space
name inside it (header pill, feed hero: "Started in a dorm room. Now it's the
whole quad."). Renaming the product itself would break the live identity —
Render service name, admin emails, SMTP subjects, and users' existing
accounts all say "The Network". The Quad styling is applied on top of that
identity, not instead of it.

## What changed (only `the_network_production.py`, 1003 → 1055 lines)
- **Design:** full CSS restyle — teal brand (`#0d9488` / `#0f766e`, dark mode
  `#2dd4bf`), clean white/dark cards, pill navigation, blurred glass top bar,
  rounded buttons/cards. All original class names kept, so every template
  still works. Old blue (`#365ee8` / `#205bce`) is gone.
- **Vocabulary:** Like → **Nod** / Nodded, "Most liked" → "Most nodded".
  Backend `likes` table and notifications are unchanged.
- **Quad moments (stories):** feed shows a "Quad moments" row of the latest
  posters — you + accepted friends only, so private-account rules are
  respected. No fake accounts were added; it is real data only.
- **Quad Radio music:** new `posts.music_track` column (added with
  `add_column`, so existing databases migrate on startup). Composer has a
  music picker + preview buttons; posts show a play bar. Playback is Web
  Audio in `/app.js` — 3 built-in note-sequence tracks, no external files,
  no third-party hosts. Invalid track values are stored as none. All JS
  stays in `/app.js`; no inline scripts, CSP `script-src 'self'` still holds.
- **Post detail:** now has a working Nod button (it had only a count before).
- **Friends:** People/Friends pages headed "Your Circle". The existing
  request / accept / decline / cancel / remove flow is unchanged.
- **Delete own post:** unchanged and kept — owner (or admin) only, with
  confirm + audit log. No change to who can delete what.
- **Admin — Traffic Shield:** new section showing configured rate limits
  (posts 30/h, comments 120/h, messages 300/h, logins, registrations) plus
  recent rate-limit windows from the `rate_limits` table (keyed one-way
  identifiers only, no raw IPs).
- **Admin — Manage accounts:** new section listing all accounts with a
  Remove button. Removal is admin-only, CSRF-checked, rate-limited
  (10/hour), cannot remove yourself or another admin / owner-email account,
  deletes the account (DB cascades posts/comments/messages/uploads) and
  removes upload files from disk, and writes a `remove_user` audit record
  (with the removed name/email in the summary). Audit log survives deletion.

## What was NOT changed
Auth/sessions, PBKDF2 passwords, CSRF, same-origin checks, security headers,
rate limits, private-account privacy, blocks, media authorisation, video
duration checks, backups, audit log, notifications, messages, Dockerfile,
render.yaml, nginx.conf, start.sh, README_PRODUCTION.md, smoke_test.py.
No existing functionality was deleted and no user data was removed.
The Quad artifact (`facebook-style-2`, versions v1/v2) was not edited.

## Tests (dev only, temp databases, wiped after each run)
- `python3 smoke_test.py` — **PASS** (baseline before the merge also PASS).
- `python3 merge_test.py` (new, dev-only; not copied by the Dockerfile) —
  **41 passed, 0 failed**. Covers: startup/healthz, CSP, Quad branding/CSS,
  feed hero + moments, composer music picker/preview, music post rendering,
  Nod on feed + post detail, friend request/accept, message, comment,
  admin dashboard / Traffic Shield / Manage accounts, non-admin 403,
  admin remove-user + login-after-removal + self-removal blocked,
  delete-own-post + audit log, private-bio privacy for a non-friend,
  all main pages 200, `/app.js` Quad Radio, invalid music track rejected.
- `python3 -m py_compile` — OK. Extracted `/app.js` — `node --check` OK.
- Two failures found during testing and fixed before the final run:
  1. `remove_user` audit entry did not show who was removed — fixed by
     including the name/email in the audit summary.
  2. A test bug: it checked private-bio privacy using a *friend* (who is
     allowed to see it) and checked the raw search term, which is echoed in
     the search box. Fixed the test to use a stranger and to check the
     person/card. The product behaviour was correct in both cases.

## Known risks / remaining issues
- Music is Web Audio note sequences, not real songs — it sounds like simple
  synth loops. Real uploaded audio would need storage, licensing and
  moderation decisions.
- Admin "Manage accounts" removal is irreversible by design — it is
  confirm-guarded, admin-only, and audit-logged, but there is no undo.
  Rollback is via the database backups, not from the panel.
- Traffic Shield shows app-level rate-limit windows only; Nginx-level
  blocking is not visible in the panel (it happens before Python).
- Layout was checked via viewport meta + responsive media queries and by
  rendering all pages over HTTP, not in a real phone/desktop browser —
  Mitchell should eyeball the feed on his phone before approving.
- No live deployment was done and none is claimed. Deployment still needs
  Mitchell's explicit approval, per his instructions.

## To test again
```
cd ~/workspace/network-merge/dev
python3 smoke_test.py
python3 merge_test.py
```

## Update 2 — Rename to The Quad + official login / create-account screens (DEV ONLY, 2026-11-19)
Dev copy only. Nothing deployed. Backup md5 still `aae87ec977f13b371de50aedb192cd96` (re-verified).

### Name chosen: The Quad
Short, memorable, distinct from Facebook, fits the Social Network dorm-room
origin ("Started in a dorm room. Now it's the whole quad.") and matches the
Quad design already merged in. User-facing branding renamed everywhere served:
page titles, header brand (`the quad.`), feed hero, login/register screens,
admin dashboard, footer, password-reset email subject/body, server version
header (`TheQuad`), startup log line. No "The Network" remains in any served
page (verified by tests on landing, /login, /register, feed and /admin).

### Deliberately NOT renamed (technical identifiers — renaming would break things)
- SQLite file `network.db`, uploads dir, backup filenames (`network-*.db`)
- Session cookie `network_session` (existing sessions would be invalidated)
- Render service name `the-network` and disk `network-data` in render.yaml
- `README_PRODUCTION.md` (historical bundle doc, not served to users)
- Docstring notes "formerly The Network" for traceability.
Flagged as a risk, not a bug: if Mitchell wants those renamed too, it is a
separate, deployment-affecting change needing its own approval and a
migration plan for existing accounts/sessions.

### Official login / create-account screens
- New real routes `GET /login` and `GET /register` (logged-in users are
  redirected to the feed). Anon `/` is now a polished landing page linking
  to both, instead of two cramped forms.
- Login: email + password, show/hide password toggle, clear generic error
  ("Email or password is incorrect.") with the email preserved, links to
  create account and forgot password. Rate limits / lockout unchanged
  (20/10 min per client, 10/10 min per email — verified: 10×401 then 429).
- Create account: name, email, password + confirm password, live strength
  meter + hint in /app.js (advisory only; server enforces 12–256 chars),
  show/hide on both password fields, clear errors for invalid email /
  weak password / mismatched passwords / duplicate email (409), with the
  name/email preserved on the re-rendered form. First-account admin rules
  unchanged. New accounts are now auto-logged-in straight to the feed
  (previously registration left you signed out on the landing page).
- Auth/session security unchanged: PBKDF2 (600k in production), CSRF,
  same-origin checks, security headers, 7-day sessions / 24h idle expiry.
  All JS stays in /app.js; CSP `script-src 'self'` still holds.

### Tests (dev only, temp DBs wiped after each run)
- `python3 smoke_test.py` — PASS (updated in dev to send confirm_password, as the new form does)
- `python3 merge_test.py` — 43 passed, 0 failed (was 41; +2 checks for the new /login and /register screens, branding check updated)
- `python3 auth_test.py` (new, dev-only) — 22 passed, 0 failed. Covers:
  landing/links, both screens, rename (no "The Network" served), valid
  register auto-login, new account can post, logged-in /login and /register
  redirect to feed, logout/login cycle, wrong password 401, unknown email
  same generic 401, duplicate 409, weak 400, mismatched confirm 400,
  invalid email 400, login lockout (10×401 then 429), admin branding, app.js.
- `python3 -W error -m py_compile` — OK. Extracted /app.js — `node --check` OK.

### Bugs found and fixed during testing
1. New JS insertion dropped the closing of the Quad Radio IIFE — `node --check`
   failed with "Unexpected end of input". Restored the close; JS_OK after.
2. `\d` in the new strength-meter JS triggered a Python SyntaxWarning
   (invalid escape sequence). Escaped it; compile is warning-free now.
3. Smoke + merge tests registered without `confirm_password` and got 400
   after the new validation — updated both test files to send it, as the
   real form does. This was a test-harness lag, not a product bug.

### Remaining issues / known risks
- Duplicate-email error now names the email as taken, which slightly weakens
  the old account-enumeration protection (login errors stay generic on
  purpose). Mitchell explicitly asked for a clear duplicate message.
- Register is rate-limited to 5/hour per IP in the app, so auth tests run on
  their own server/temp DB (auth_test.py) to avoid tripping it; noted in the
  test file.
- Password strength meter is advisory — a 12+ character passphrase passes
  even without mixed case/digits/symbols, by design (existing test passwords
  are passphrases).
- Layout was checked via viewport meta + responsive media queries and by
  rendering pages over HTTP, not in a real phone/desktop browser — Mitchell
  should eyeball /login and /register on his phone before approving.
- No live deployment was done and none is claimed. Deployment still needs
  Mitchell's explicit approval, per his instructions.

### To test again
```
cd ~/workspace/network-merge/dev
python3 smoke_test.py
python3 merge_test.py
python3 auth_test.py
python3 security_test.py
```

## Update 3 — Security hardening + fix all remaining issues (DEV ONLY, 2026-11-19)
Dev copy only. Nothing deployed. Backup md5 still `aae87ec977f13b371de50aedb192cd96` (re-verified).
File: `the_network_production.py` 1109 → ~1260 lines. `py_compile` (with `-W error`) OK, `/app.js` `node --check` OK.

### Issues fixed
1. **Duplicate-email enumeration — fixed.** Registration on an existing email
   now returns a generic 200 page ("If this email is new here… If you already
   have an account… try signing in, or reset your password") instead of
   409 "An account with that email already exists". No account is created and
   no session is granted. Login, forgot-password and reset-code flows were
   already generic and were verified identical for unknown/known emails.
   Residual: a determined prober could still distinguish a successful
   registration (303 redirect to the feed) from a duplicate (200 page) by
   response code alone; the message itself no longer confirms existence.
2. **Admin removal irreversible — fixed with a safety net.** Removal now
   requires the admin to type the member's exact email address, takes a full
   database backup first, and saves the account (profile, posts, comments,
   messages, likes, friendships, notifications, uploads metadata, blocks)
   in a new `removed_users` table with a **30-day restore window**. Upload
   files are moved to `removed_uploads/`, not deleted. New admin endpoint
   `POST /admin/restore-user` (admin-only, CSRF, 10/hour) restores the
   account under its original id when free, otherwise a new id; restore is
   refused (409) if the email is now taken, or after the 30-day deadline.
   Expired saved copies are pruned on startup. Self/other-admin removal is
   still blocked. Audit records `remove_user` and `restore_user`.
3. **Traffic Shield Nginx visibility — fixed by labelling, not inventing data.**
   The admin panel now has two labelled sections: "app level (measured by
   The Quad)" and "Nginx level (configured, not measured)", showing the real
   configured Nginx limits from `nginx.conf.template` (20 req/s per IP,
   burst 60, 20 conns/IP, 27 MB body, timeouts) with a clear note that Nginx
   counts are not visible to the app.
4. **Password policy strengthened (server-side).** New `validate_password()`
   applied to registration AND password reset: 12–256 chars, at least 2 of
   (lower, upper, digit, symbol), a ~50-entry common-password blocklist,
   no single repeated character, no plain sequence, and no use of the user's
   own name or email local part. Register hint updated to match. Existing
   accounts are NOT forced to change — the check only runs when a password
   is newly set (see residual risks).
5. **Music — kept as labelled synth loops, no real songs added.** Composer
   now says "built-in synth loops, not real songs" and every music post bar
   says "built-in loop". No user audio uploads were added: real songs would
   need storage, licensing and moderation decisions.
6. **Mobile layout — real fix found.** `.auth-wrap` (login/register) was a
   two-column grid with no mobile breakpoint, so it stayed cramped on phones.
   It now stacks to one column under 650px, the auth hero shrinks, table
   rows wrap, music bars wrap, and story avatars shrink. Verified by CSS
   checks in security_test.py (viewport meta, stacking rule, no fixed
   desktop widths). Still not checked in a real phone browser — Mitchell
   should eyeball /login and /register on his phone before approving.

### Extra hardening found by the adversarial audit
- **X-Forwarded-For spoofing:** rate-limit keys used the FIRST XFF entry,
  which an attacker can spoof. Now uses the LAST entry (added by our own
  Nginx). Verified: 21 logins via the same last-hop IP hit 429; a different
  last-hop IP is a different bucket.
- **Page-view log flooding:** every GET inserted a `page_views` row with no
  limit, so a GET flood could fill the database. Logging is now throttled
  to 20/minute per client (pages still serve normally); admin traffic
  figures are therefore approximate, and the panel says so.
- **Duplicate spam:** no duplicate-content filter existed. Identical posts
  and identical comments from the same user within 60 seconds are now
  rejected (429).
- **LIKE wildcard abuse:** search terms were used raw in `LIKE`, so `%`
  acted as a wildcard. Terms are now escaped with `escape_like()`.
- **Notification URLs:** `target_id` is now escaped when building links
  (defence in depth; values were already server-generated).

### Vulnerability test — `security_test.py` (new, dev-only): 78 passed, 0 failed
- **Auth:** wrong/unknown-email login both generic 401; SQLi login 401;
  session token rotates on login; logout invalidates; CSRF missing/wrong/
  another-user's all 403; login lockout 10×401 then 429; reset-code requests
  identical for unknown/known email; guessed reset code rejected and the
  account still logs in afterwards; session cookie HttpOnly + SameSite=Lax
  + Path=/.
- **Access control:** non-admin /admin 403, remove/restore 403; cannot
  delete others' posts/comments (403); cannot view private post/profile
  (403) while an accepted friend can (200); cannot message non-friends;
  profile edits affect only your own account; private bios not searchable
  by non-friends.
- **Injection:** stored XSS in a post is escaped (`&lt;script&gt;`); SQLi in
  search and post-id fails safely; path traversal (`..%2f`, `%2e%2e`,
  `....//`) all 404 with no file content leaked.
- **Uploads:** HTML labelled as PNG rejected (magic bytes), SVG rejected,
  >8 MB image rejected, valid private photo accepted and 404 for
  non-friends / 200 for friends / 401 when signed out; video claiming
  >30 s rejected (ffprobe is present in dev, so server-side duration
  checks apply here too).
- **Rate limits / abuse:** post flood 30 ok then 429; duplicate post 429;
  25 rapid page views all serve.
- **Headers/CSP:** CSP `script-src 'self'` with no unsafe-inline, nosniff,
  X-Frame DENY, Referrer same-origin, Permissions-Policy, COOP/CORP all
  present; HSTS absent in dev and present (`max-age=31536000`) in a
  production-mode test server.
- **Admin safety net:** remove without/wrong typed confirmation 400,
  with confirmation succeeds, 30-day window recorded, full DB backup
  exists, removed user cannot log in, restore works and the user can log
  in again, restore blocked (409) when the email is re-taken.

### Test bugs found while testing (not product bugs)
- My first password check rejected any password containing the substring
  "password", which broke the passphrase `another-correct-password-123`.
  Fixed to exact-match against the common-password list only.
- urllib follows login 303s, losing Set-Cookie — fixed with a
  non-redirecting opener for the cookie/token checks.
- Reset-code comparison wrongly required identical pages, but the form
  legitimately echoes the submitted email — fixed to compare the notice text.
- Flood-post check expected 303, but the followed redirect reads as 200.
- CSS width check matched the substring inside `max-width:1160px` — fixed
  to check for real fixed min-widths.
- Music-bar check ran before any music post existed in that test DB.

### Regression suites (re-run after all fixes)
- `python3 smoke_test.py` — PASS
- `python3 merge_test.py` — 49 passed, 0 failed (was 43; +6 checks for
  typed confirmation, restore, and the new admin sections)
- `python3 auth_test.py` — 23 passed, 0 failed (was 22; duplicate-email
  check updated to the generic response, plus original-password-still-works)
- `python3 security_test.py` — 78 passed, 0 failed

### Remaining issues / known risks
- Existing accounts with weak passwords are not forced to change; the new
  policy only applies when a password is newly set. A forced reset for
  weak accounts would need Mitchell's approval and a user notice.
- Duplicate-registration responses still differ by status code (303 on
  success vs 200 generic page on duplicate) — see issue 1.
- `style-src 'unsafe-inline'` remains in the CSP because the CSS is inline
  in `<style>`; moving it to a separate stylesheet would remove that, but
  is a larger change and was not attempted here.
- Password reset forms (`/send-reset-code`, `/reset-password`) have no CSRF
  token because they are unauthenticated; the 6-digit code itself (hashed,
  5 attempts, 10-minute expiry, rate-limited) is the secret. Acceptable
  as-is, noted here.
- Video duration in dev falls back to the client-supplied `duration_ms`
  only when ffprobe cannot read the file; in production a missing ffprobe
  rejects the upload outright. A corrupt-but-playable file is not a
  realistic >30 s bypass.
- Restore re-inserts saved rows with `INSERT OR IGNORE`, so if an id was
  reused by new content after deletion, that one saved row is skipped
  rather than overwriting live data.
- Layout was checked via CSS/HTTP rendering, not in a real phone/desktop
  browser — Mitchell should eyeball /login, /register and the feed on his
  phone before approving.
- No live deployment was done and none is claimed. Deployment still needs
  Mitchell's explicit approval, per his instructions.

### To test again
```
cd ~/workspace/network-merge/dev
python3 smoke_test.py
python3 merge_test.py
python3 auth_test.py
python3 security_test.py
```

## Update 4 — Fix ALL remaining issues (DEV ONLY, 2026-11-19)
Dev copy only. Nothing deployed. Backup md5 still `aae87ec977f13b371de50aedb192cd96` (re-verified).
File: `the_network_production.py` 1256 → 1390 lines. `py_compile` (`-W error`) OK, extracted `/app.js` `node --check` OK.

### 1. Forced password change — implemented, safely
On login (the only moment the plaintext password is available, so the
stored one-way hash is never guessed at), an account is flagged when its
hash is not the current PBKDF2-SHA256 format/iteration count OR its
password fails the current `validate_password()` policy. Flagged users
get a `users.must_change_password` flag, are sent to a new real route
`GET/POST /change-password`, and cannot use the feed, admin, posting or
any other page (GET and POST gates) until they change it. The change
form requires the current password, a new password passing the policy,
and a different password from the current one; other sessions are
invalidated on change and the event is audit-logged
(`password_change_required` on flagging, `password_change` on change).
Admins are not exempt. Password reset also clears the flag (the new
password already passed the policy). Existing strong passwords are
unaffected — a strong-password login still goes straight to the feed.
User notice Mitchell would need to send (suggested wording):
"Hi — The Quad has tightened its password rules. If your password was
set a while ago or is too simple, you will be asked to choose a new one
(12+ characters, at least two kinds of characters) the next time you
sign in. It only takes a minute, and your posts, friends and messages
are all still there."

### 2. Duplicate registration — identical observable response
Successful and duplicate registrations now return the SAME status (200)
and the SAME page (`REGISTRATION_NOTICE`, with a "Continue to The Quad"
button). A new account is still signed in via its session cookie, so
Continue leads to the feed; a duplicate gets no cookie, so Continue
leads to the sign-in landing. Residual difference, documented honestly:
the Set-Cookie header itself differs (a new account must receive a
session somehow) and there may be a tiny timing difference. The status
code, page text and links no longer leak existence.

### 3. External stylesheet — `style-src 'unsafe-inline'` removed
CSS moved out of the inline `<style>` block to a served route
`GET /app.css` (text/css, like `/app.js`), linked from `shell()`.
CSP is now `style-src 'self'` with no `unsafe-inline` anywhere in the
header. All inline `style="..."` attributes were replaced with classes:
avatar sizes (`.av30`–`.av90`), `.brand-dot`, `.rail-h2`, `.shield-h2`,
`.confirm-email`, and traffic-bar heights (`.tb3`–`.tb100` in 5% steps).
The password-meter bar still uses JS `element.style.width` (CSSOM),
which CSP does not block. Design is identical.

### 4. Password-reset CSRF — signed stateless tokens
New `reset_token()` / `verify_reset_token()`: HMAC-SHA256 signed with
APP_SECRET over purpose + timestamp, 15-minute expiry, no server state
and no session needed. Both reset forms carry one (`send-reset` on the
forgot-password form and on the "send another code" form,
`reset-password` on the code form). Missing or forged tokens are
rejected (403 / invalid-request page) before any rate-limit or email
lookup. Residual: the token is only as strong as APP_SECRET, and a
token stolen from a victim's own browser could be used within its
15-minute window — the same exposure as any form token; the 6-digit
code (hashed, 5 attempts, 10-minute expiry, rate-limited) is still
required to actually reset anything.

### 5. Video duration — client fallback removed
`save_upload()` now rejects any video whose duration ffprobe cannot
verify server-side, in dev AND production. The browser-side
`duration_ms` field is ignored entirely (it remains only as a client
pre-check that warns before upload). A corrupt/unreadable video is
rejected with "Video validation is unavailable on the server."

### 6. Restore id collisions — remapped, nothing lost
Restore no longer uses `INSERT OR IGNORE` for child rows. New
`insert_with_id()` keeps a row's id when free, otherwise inserts it
under a new id and returns the id actually used. Posts are remapped
first (`post_map`), then comments/likes/notifications follow the new
post ids, and friendships/messages/notifications/uploads/blocks follow
the remapped user id. The restored user's `must_change_password` flag
is preserved. Tested with both a reused post id and a reused user id:
both the live row and the restored row survive, with references intact.

### 7. Responsive layout — checked at 360px and desktop
Added a `@media(max-width:400px)` breakpoint (header, nav, cards,
auth hero, metric grid → 1fr, traffic chart, stories, message list,
table rows stack, confirm-email full width). Verified by HTTP rendering
of /login, /register, /, /profile and /admin plus CSS audit: viewport
meta on all pages, no `<style>` / `style="` anywhere, no fixed
min-width over 360px in CSS, traffic bars use classes. Not checked in a
real phone/desktop browser — Mitchell should still eyeball /login,
/register and the feed on his phone before approving (no browser was
available in this dev environment).

### Tests (dev only, temp DBs wiped after each run)
- `python3 smoke_test.py` — PASS
- `python3 merge_test.py` — 51 passed, 0 failed (was 49; +2 for /app.css)
- `python3 auth_test.py` — 25 passed, 0 failed (was 23; register flow updated)
- `python3 security_test.py` — 84 passed, 0 failed (was 78; +6 for reset tokens, CSP, external CSS)
- `python3 fix_test.py` (new, dev-only) — 32 passed, 0 failed. Covers
  all seven fixes above, including forced change (legacy hash,
  weak-policy password, admin, strong password unaffected), identical
  register responses, /app.css + CSP, reset tokens, unverifiable video,
  restore with post-id AND user-id collisions, and responsive checks.

### Bugs found and fixed during testing
1. Fix-test used `password123456` as the "weak" password — it actually
   passes the stated policy (12 chars, 2 classes, not on the blocklist),
   so login correctly did not force a change. Changed the test password
   to `aaaaaaaaaaaa`, which genuinely fails the policy.
2. Fix-test checked for an avatar class on the feed, but the feed shows
   no avatar when there are no posts. Changed the check to /profile,
   where an avatar always renders.
3. The change-password page's strength meter did not update because the
   JS only bound `#register-password`. It now also binds `#new-password`.

### Remaining issues / known risks
- Set-Cookie is the one unavoidable difference between a new and a
  duplicate registration (see issue 2); a determined prober could also
  notice a small timing difference.
- Reset tokens are HMAC-signed with APP_SECRET (see issue 4).
- Forced change only triggers at login; an account that never signs in
  keeps its old password until it does. No mass forced reset was done.
- Layout was checked via CSS/HTTP rendering at 360px/desktop widths,
  not in a real phone/desktop browser.
- No live deployment was done and none is claimed. Deployment still
  needs Mitchell's explicit approval, per his instructions.

## Update 6 — Top 5 improvements (DEV ONLY, 2026-11-19)
Dev copy only. Nothing deployed, nothing uploaded to GitHub, nothing
touched on Render, no payment. Backup md5 still
`aae87ec977f13b371de50aedb192cd96` (re-verified). A file backup of the
pre-Top-5 code is kept in dev as `the_network_production.py.bak-top5`.
File: `the_network_production.py` 1557 → 2295 lines. New dev-only test
`top5_test.py` (78 checks). `py_compile` (`-W error`) OK, extracted
`/app.js` `node --check` OK. Quad Radio and Listening Notes untouched.

### What Mitchell asked for
Implement the Top 5 from the audit, in order, in dev only:
1. Post search + hashtags + Explore/trending
2. Reactions beyond Nod + threaded replies + reposting
3. Groups + Events + Polls
4. Notification preferences + grouped in-app alerts
5. Reports + moderation queue + data export and account deletion

### 1. Post search + hashtags + Explore/trending
- **Post search** (`GET /search?q=`): SQLite **FTS5** (virtual table
  `posts_fts`, content-synced via triggers, backfilled on startup;
  LIKE fallback if FTS5 is unavailable — FTS5 is present on the dev
  SQLite 3.45.1). Searches post text; results are privacy-filtered in
  Python (`can_view_post` / `can_view_repost` / group membership), so
  private and blocked posts never leak — tested explicitly for search,
  Explore and tag pages.
- **Hashtags:** parsed on post/repost/group-post create
  (`#tag`, 1–50 chars, stored in `hashtags` + `post_hashtags`),
  rendered as links in post bodies, tag pages at `/tag/<name>`.
- **Explore** (`GET /explore`): trending posts (time-decayed score —
  (Nods×3 + comments×2) / (1 + age_hours/24), last 14 days, max 200
  candidates), trending hashtags (last 7 days), suggested people
  (friends-of-friends / recent members, no fake accounts), new groups.
- **Following-only feed:** `/?feed=following` shows only your own posts
  and accepted friends' posts; `/?feed=all` is the default. Pagination
  links preserve the mode.

### 2. Reactions + threaded replies + reposting
- **Reactions:** fixed set Nod / Love / Laugh / Support, on posts and
  on comments (`reactions` and `comment_reactions` tables, one reaction
  per user per target, tapping the same kind again removes it).
  Nod stays on the legacy `likes` table for backward compatibility;
  other kinds use `reactions`. Counts by type shown on the feed.
  Reaction notifications (kind `reaction`) go to the post/comment
  author, respecting notification prefs.
- **Threaded replies:** comments gain `parent_id`; replies are one
  level deep (replying to a reply is rejected, 400), rendered indented
  under their parent, with a reply notification to the parent author.
- **Comment edit:** author-only, 1–1000 chars, audit-logged
  (`edit_comment`); editing someone else's comment is 403.
- **Reposting:** a repost is a post row with `original_post_id` +
  optional comment (≤500 chars), rendered with the quoted original.
  **Privacy inheritance is enforced twice:** at repost time (you must
  be able to see the original) and at render/search time
  (`can_view_repost` — a private original is hidden even from friends
  of the reposter who are not friends of the original author).
  Group posts cannot be reposted to the main feed. Repost notifies
  the original author (kind `repost`).
- **Also added (cheap, clean):** saved posts/bookmarks (`saved_posts`,
  `/saved` page, Save toggle on the feed) and pinned posts
  (`posts.is_pinned`, Pin toggle on own posts, Pinned section on the
  member profile page).

### 3. Groups + Events + Polls
- **Groups** (`groups`, `group_members`): public/private, owner/admin/
  member roles, join (public, instant) or join request (private,
  pending → owner/admin approve or reject), promote/demote, leave.
  Group discussions are group-scoped posts (`posts.group_id`) that
  only show inside the group — never on the main feed. Private groups
  hide their posts from non-members. Group admins are notified of
  join requests (kind `group_invite`).
- **Reports are built alongside groups** (see §5): a group cannot be
  used to dodge moderation — posts in groups can be reported and
  hidden/removed from the moderation queue.
- **Events** (`events`, `event_rsvps`): title, description, location,
  future date/time, optional group (membership checked). RSVP
  going/maybe/not going, changeable. Invitations to accepted friends
  create an `event_invite` notification.
- **Polls** (`polls`, `poll_options`, `poll_votes`): post author only,
  question + 2–5 options, one vote per user, changeable. Results
  (counts + percentages) show only after you vote — before voting you
  see the question and the voting form only.

### 4. Notification preferences + grouped in-app alerts
- **Preferences** (Settings, `notification_prefs` table): per-kind
  on/off for all 11 kinds (like, comment, friend_request,
  friend_accept, message, repost, reaction, group_invite,
  event_invite, event_reminder, report_update). Default is on; a
  stored off suppresses that kind at send time (`notification()`).
- **Grouped alerts:** the Notifications page groups by
  (actor, kind, target) within the last hour and shows a ×N badge.
  New kinds: repost, reaction, group_invite, event_invite,
  event_reminder, report_update.
- **Mark all as read** (`POST /notifications/read-all`); opening the
  page no longer silently marks everything read — the button does.
- **Push notifications were NOT built**, as instructed: they need a
  service worker + VAPID keys + a push sender, which this single-file
  server does not have. Settings says so plainly.

### 5. Reports + moderation queue + data export and account deletion
- **Reports** (`reports` table): report a post, comment, user or
  message with a reason (≤80 chars) + details (≤500). One open report
  per reporter per target. Rate-limited 20/hour.
- **Moderation queue** (`GET /admin/moderation`, admin only): reports
  newest first with target, reason, reporter and status. Actions:
  hide (post → `is_hidden=1`, removed from feed/search/Explore/tag/
  profile/friends rail), remove (delete the target), warn (notify the
  target's owner), dismiss. Every action is audit-logged
  (`moderate_report`) and the reporter is notified of the outcome
  (kind `report_update`).
- **Data export** (`GET /export-data`): your profile, posts, comments,
  messages, likes, reactions, saved posts, listening notes and groups
  as a JSON download. It includes only messages where you are sender
  or recipient — other people's private DMs are never included
  (tested: Frank's DM to Eve is absent from Alice's export, present
  in Frank's).
- **Self-serve account deletion** (`POST /account/delete`): requires
  typing your exact email + your current password (re-auth). Takes a
  full DB backup first, saves the account into `removed_users` with
  the existing 30-day restore window (restorable from the admin panel
  — tested end-to-end), moves uploads to `removed_uploads/`, deletes
  the account and signs you out. Admin accounts cannot self-delete.

### What did NOT change
Auth/sessions, PBKDF2, CSRF, same-origin, security headers, rate
limits (new actions added to the existing rate-limit map), private-
account privacy, blocks, media authorisation, video duration checks,
backups, audit log, Quad Radio, Listening Notes, Dockerfile,
render.yaml, nginx, start.sh, README, smoke_test.py. No existing
functionality was deleted and no user data was removed. No
credentials or tokens were added. **Not built, as instructed:**
group chats, typing indicators, live Spotify sync (no Spotify app/
credentials), push notifications, true algorithmic personalised
ranking, 2FA, Postgres migration, load testing. **SMTP was NOT
configured** — there are no SMTP credentials in the code and
render.yaml leaves `SMTP_*` unset, so password recovery still cannot
send mail until SMTP is configured with real credentials Mitchell
provides. **The `the-network` technical identifiers were NOT
renamed** (Render service, disk, DB file, session cookie) — that is a
deployment-affecting migration needing its own approval. **No hosting
decision was made** — the Python version remains undeployed (Render
paid plan needs a card; Mitchell has said no payment).

### Tests (dev only, temp DBs wiped after each run)
- `python3 smoke_test.py` — PASS
- `python3 merge_test.py` — 51 passed, 0 failed (unchanged)
- `python3 auth_test.py` — 25 passed, 0 failed (unchanged)
- `python3 security_test.py` — 84 passed, 0 failed (unchanged)
- `python3 fix_test.py` — 32 passed, 0 failed (unchanged)
- `python3 listening_test.py` — 44 passed, 0 failed (unchanged)
- `python3 top5_test.py` (new, dev-only) — **78 passed, 0 failed**.
  Covers: hashtags + tag pages, FTS post search, Explore/trending/
  suggestions, following-only feed, private-post leak checks in
  search/Explore/tag/repost, reactions (store, counts, toggle off,
  CSRF, invalid kind), threaded replies (render, depth cap, edit,
  edit-other 403, comment reactions), reposts + privacy inheritance
  (stranger hidden, friend visible, search leak check), saved +
  pinned, groups (public join, private request/approve, promote,
  non-admin approve blocked, group post only in group), events
  (create, RSVP, RSVP change, invite notification), polls (form
  hidden results before voting, vote, change vote, one vote per user,
  non-owner add blocked), notification prefs (off suppresses),
  grouped alerts (×2), mark-all-read, repost notifications, reports
  (create, duplicate rejected, queue, non-admin 403, hide, reporter
  notified, warn), data export (own data in, others' DMs out),
  self-delete (wrong password rejected, correct deletes, login
  fails, 30-day restore, admin restore + login again, admin
  self-delete blocked), CSRF on new endpoints, group-name XSS
  escaped, feed still renders.

### Bugs found and fixed during testing
1. **Event RSVP insert (real product bug):** the event-creation
   handler inserted 5 values into the 4-column `event_rsvps` table
   and crashed the request (server returned no response). Fixed the
   INSERT to 4 values; event creation now works.
2. **Poll results leaked before voting (real product bug):** the poll
   box header showed the total vote count even to people who had not
   voted. The total now shows only after voting.
3. **Hidden posts still showed in the feed's "Friends' posts" rail
   and on member profiles (real product bug):** the main feed query
   excluded `is_hidden`, but the friends-rail query and the public-
   profile query did not. Both now filter `is_hidden=0`.
4. **Test-harness bugs, not product bugs:** (a) search URLs with
   spaces raised `InvalidURL` — fixed by URL-encoding the query;
   (b) the private-post leak checks matched the search term echoed
   in the search box itself — fixed to check the post's own wording
   ("Carol secret"); (c) the notification-pref check turned off
   `like` but tested a `support` reaction, which sends kind
   `reaction` — fixed the test to turn off `reaction`.

### Remaining issues / known risks
- Admin restore does not restore the new Top 5 rows (reactions,
  saved posts, group memberships, poll votes) — the data is preserved
  in the saved JSON in `removed_users`, but the restore code only
  re-inserts the older tables. A restored account keeps its profile,
  posts, comments, messages and friendships; its reactions/saves/
  group memberships are lost. Worth a follow-up if restore fidelity
  matters.
- Notifications no longer auto-mark-read when the page is opened —
  the Mark all as read button does it. The header badge therefore
  stays until the button is pressed; that is the intended behaviour,
  but it is a change from before.
- Trending is computed over the last 200 posts from the last 14 days
  on every Explore load — fine for a small community, but it will
  need caching if the site ever gets busy.
- FTS5 search uses a phrase query; a multi-word search only matches
  the exact phrase. Single words work well; phrase matching is a
  deliberate, safer choice (no FTS query-syntax injection).
- Group posts have no per-post audience beyond group membership, and
  group admins cannot yet hide/remove individual group posts from
  inside the group page — they must use the moderation queue.
- Layout was checked via CSS/HTTP rendering, not in a real
  phone/desktop browser — Mitchell should eyeball Explore, Groups,
  Events, the moderation queue and a poll on his phone before
  approving.
- No live deployment was done and none is claimed. Deployment still
  needs Mitchell's explicit approval, per his instructions.

### To test again
```
cd ~/workspace/network-merge/dev
python3 smoke_test.py
python3 merge_test.py
python3 auth_test.py
python3 security_test.py
python3 fix_test.py
python3 listening_test.py
python3 top5_test.py
```

## Update 5 — Spotify Listening Notes (DEV ONLY, 2026-11-19)
Dev copy only. Nothing deployed. Backup md5 still `aae87ec977f13b371de50aedb192cd96` (re-verified).
File: `the_network_production.py` 1390 → 1557 lines. New dev-only test
`listening_test.py` (44 checks). `py_compile` (`-W error`) OK, extracted
`/app.js` `node --check` OK. Quad Radio untouched and still working.

### What Mitchell asked for
Make the music link to Spotify, show in real time what people are
listening to, and let anyone pick a song to display anytime, like the
notes section on Instagram.

### Honest real-time limitation — read this first
**Automatic real-time Spotify sync does NOT work in this build, and this
report does not claim it does.** There is no Spotify developer app /
client credentials in this codebase, and Mitchell's Spotify is not
connected to Muse. A live "now playing" feed would need all of:
1. a Spotify developer app configured on the server (client ID + secret
   — none are stored in this code, and none were invented);
2. each person linking their own Spotify account through Spotify OAuth;
3. permission to read what they are currently playing
   (`user-read-currently-playing` scope).
No Spotify tokens are read, stored or sent anywhere in this build.
What WAS built is the part that works without any of that: a manual
Listening Note, set by hand anytime, with a real Spotify link + preview
player. The app says so on the page itself — Settings has a "Spotify —
live now-playing" card and `GET /notes/refresh` is a stub that only
explains the above and states "Live Spotify sync is not set up yet /
not connected". It changes no state and fakes no "connected" state.

### What changed
- **New table `listening_notes`** (created in `init_db`, pruned on
  startup when expired): `user_id` UNIQUE (one active note per user),
  `track_title`, `artist`, `spotify_url`, `spotify_track_id`, `created`,
  `expires_at` (created + 24 hours, like Instagram Notes).
- **Spotify validation — `parse_spotify_link()`:** accepts a bare
  22-char track id, `spotify:track:<id>`, or an
  `open.spotify.com/track/<id>` URL (optional query/fragment, canonical
  URL stored). Rejects everything else: non-Spotify hosts (including
  lookalike `evil-open.spotify.com`), album/artist/playlist links,
  short/long ids. Id must match `[A-Za-z0-9]{22}`. No network call is
  made — the id is checked for well-formedness only, not verified to
  exist on Spotify.
- **Set/change/clear — `POST /listening-note`** (CSRF, rate-limited
  10/hour per user, owner-only by construction: the note is always for
  the signed-in session user, a `user_id` field in the form is ignored).
  Title 1–120 chars, artist 0–120 chars, link optional. Clear is a
  separate confirm-guarded form. Set/clear are audit-logged
  (`listening_note_set` / `listening_note_clear`).
- **Display:** (a) Feed stories row ("Quad moments") shows a small
  ♫ note bubble above a friend's avatar when they have an active note.
  (b) Feed right rail has a "Now listening" section listing friends'
  active notes with track, artist, time set and Spotify link.
  (c) Own profile and the feed both show a "Your Listening Note"
  card with the set/change/clear form and, when a Spotify track is
  attached, the embed player. (d) A member's public profile
  (`/member/<id>`) shows their active note with embed — only when the
  viewer is allowed to see that profile (see visibility).
  Each note links out to `https://open.spotify.com/track/<id>` and,
  where shown, embeds `https://open.spotify.com/embed/track/<id>`
  (no-auth preview player). **No auto-play anywhere.**
- **Visibility — `visible_listening_note()`:** a note is visible to its
  owner and to anyone who can view the owner's posts
  (`can_view_post()` — so a non-friend cannot see a private user's
  note, and a blocked user cannot see it either). Expired notes are
  never visible. The feed "Now listening" list is friends only.
- **Spotify on posts (kept, clean):** composer has an optional
  "Attach a Spotify track" field; `posts` gains `spotify_url` /
  `spotify_track_id` columns via `add_column` (existing DBs migrate on
  startup). A post with only a Spotify track (no text/media) is
  allowed; an invalid link rejects the post with 400 and nothing is
  created. Feed posts and post detail render the Spotify link + embed.
  Admin restore now preserves the two new post columns.
- **CSP:** `frame-src https://open.spotify.com` added so the Spotify
  embed iframe loads. `script-src 'self'` / `style-src 'self'` and no
  `unsafe-inline` are unchanged; `frame-ancestors 'none'` unchanged.
- **CSS:** new `.note-bubble`, `.spotify-note`, `.spotify-embed`
  (100% wide, 80px high, no inline styles) and `.now-listening`
  classes in the external stylesheet.

### What did NOT change
Quad Radio (built-in synth loops) is untouched — same tracks, same
composer picker/preview, same play bars, verified still rendering.
Auth/sessions, PBKDF2, CSRF, same-origin, security headers, rate
limits, private-account privacy, blocks, media authorisation, video
duration checks, backups, audit log, notifications, messages,
Dockerfile, render.yaml, nginx, start.sh, README, smoke_test.py.
No existing functionality was deleted and no user data was removed.
No credentials or tokens were added. No deployment, no GitHub upload,
no Render change.

### Tests (dev only, temp DBs wiped after each run)
- `python3 smoke_test.py` — PASS
- `python3 merge_test.py` — 51 passed, 0 failed (unchanged)
- `python3 auth_test.py` — 25 passed, 0 failed (unchanged)
- `python3 security_test.py` — 84 passed, 0 failed (unchanged)
- `python3 fix_test.py` — 32 passed, 0 failed (unchanged)
- `python3 listening_test.py` (new, dev-only) — **44 passed, 0 failed**.
  Covers: valid Spotify URL / bare id / `spotify:` URI; title-only
  notes; all six invalid-link cases rejected with 400 and nothing
  stored; XSS title/artist escaped on the feed; change (one active
  note only) and clear; 24h expiry (note made expired in the DB is
  invisible on feed and profile); friend sees a private user's note
  on their member page, non-friend gets 403 and sees nothing, stranger
  sees a public user's note, blocked stranger still gets 403; stories
  bubble + Now listening sidebar; rate limit (first 10 note changes
  allowed, 11th 429); missing/wrong CSRF 403; `user_id` spoof ignored
  (owner's note unchanged, attacker's own note set instead);
  Spotify-only post renders link + embed on feed and post detail,
  invalid post link 400 with no post created; `/notes/refresh`
  says "not set up / not connected" and never claims a live status;
  Settings explains the Spotify app + OAuth requirement;
  CSP `frame-src https://open.spotify.com` present with
  `script-src 'self'` and no `unsafe-inline`; Quad Radio still works.

### Bugs found and fixed during testing
1. **Post-detail Spotify render was applied to the wrong occurrence:**
   an edit matching the feed's music line duplicated it in the feed
   loop (with wrong indentation) and left post detail without the
   Spotify block. Fixed by removing the duplicate feed line and
   adding the block to `post_detail()` with its own unique context;
   `py_compile` and the listening tests (feed + detail) now pass.
2. **Test-harness bug, not a product bug:** the first listening-test
   run stopped after 29 checks because a 6th `/register` hit the
   app's 5/hour registration limit (all test clients share one IP),
   and the `finally` block reported "29 passed, 0 failed" with exit 0
   even though the run never finished. Fixed by seeding the 6th user
   directly in the DB and logging in normally, and by making the
   harness record an exception as a failed check. Full run is now
   44 passed, 0 failed.
3. **Spotify-only posts were rejected:** the old "write text or attach
   a photo/video" guard ran before the Spotify link was parsed.
   Moved the emptiness check after parsing so a post with only a
   Spotify track is allowed (message updated to mention it).

### Remaining issues / known risks
- **No live sync (by design, see above).** A note only changes when
  its owner changes it. If Mitchell wants true real-time now-playing,
  that is a separate piece of work: register a Spotify developer app,
  configure its credentials on the server (never in code), add
  per-user OAuth linking, store refresh tokens securely, and poll
  Spotify's currently-playing endpoint. That needs his explicit
  approval and is not in this build.
- A Spotify track id is validated for format only, not existence — a
  well-formed but non-existent id shows Spotify's own "not found"
  inside the embed, not a Quad error.
- The Spotify embed loads Spotify's player in an iframe (allowed by
  CSP `frame-src` only for open.spotify.com). It plays a 30-second
  preview; full playback needs the listener's own Spotify sign-in.
- Notes are friends-visible by design, like Instagram Notes — a
  public user's note is visible to any signed-in member who can view
  their profile. There is no per-note audience picker.
- Layout was checked via CSS/HTTP rendering, not in a real
  phone/desktop browser — Mitchell should eyeball the feed, profile
  and a Spotify embed on his phone before approving.
- No live deployment was done and none is claimed. Deployment still
  needs Mitchell's explicit approval, per his instructions.


## Update 7 — Forgot Password with recovery codes (DEV ONLY, 2026-11-19)
Dev copy only. Nothing deployed, nothing uploaded to GitHub, nothing
touched on Render, no payment. Backup md5 still
`aae87ec977f13b371de50aedb192cd96` (re-verified). File:
`the_network_production.py` 2295 → 2401 lines. New dev-only test
`forgot_test.py` (29 checks). `py_compile` (`-W error`) OK, extracted
`/app.js` `node --check` OK.

### What Mitchell asked for
A working Forgot Password system. The audit had found a reset flow
already existed (six-digit emailed code, hashed, 5 attempts, 10-minute
expiry, HMAC-signed CSRF tokens, password policy) but it could not
send mail until SMTP is configured, so in practice a locked-out user
had no self-service recovery.

### Audit of the existing flow (before changing anything)
- `/login` already linked to `/forgot-password`; the forgot page
  said a code would be emailed "if email delivery is configured".
- Unknown vs known email already gave the same response (no
  enumeration), and no code row was created when SMTP was off.
- Email-code verification, single-use, expiry, 5-attempt deletion,
  password policy, session invalidation and audit logging all worked
  when a code row was present (verified by seeding code rows in the
  test DB, since no SMTP exists in the test env).
- Bugs found: (1) if SMTP delivery failed, the async sender deleted
  the code row, leaving the user with a "code is being sent" message
  for a code that no longer existed; (2) the verify rate limit was
  enforced but returned HTTP 200 instead of 429, unlike the login
  lockout. Both fixed (see below).
- `users.recovery_hash` was a dead column (written nowhere, only
  cleared on reset). Left untouched.

### What was chosen (one approach, fully built)
Recovery codes — NOT admin-assisted reset. A signed-in user creates
10 one-time recovery codes in Settings → Password recovery. Each code
is 12 characters from an unambiguous alphabet (no 0/O/1/I), shown as
XXXX-XXXX-XXXX, displayed ONCE in the response that creates them, and
only its PBKDF2 hash is stored — plaintext is never saved, logged or
emailed. A locked-out user enters one on the reset page instead of an
emailed code; it works any time until used, then it is consumed.
Regenerating a set replaces the old codes immediately. An admin
reset was not chosen because it needs out-of-band identity checks
the app cannot do, and it would leave the user waiting on a person.

### What changed
- New `recovery_codes` table (user, PBKDF2 code hash, used flag,
  created/used_at) with index; helpers `new_recovery_code`,
  `normalise_recovery_code`, `generate_recovery_codes`,
  `count_unused_recovery_codes`, `consume_recovery_code`.
- `/forgot-password` now explains both paths honestly: emailed code
  when SMTP is configured, recovery codes always; when SMTP is not
  configured it says so plainly and points at recovery codes.
- Reset form accepts a six-digit email code OR a recovery code in
  one field; wrong codes give the same generic error either way.
- `/reset-password` verifies recovery codes (single-use, audit-logged
  with `{'method': 'recovery_code'}` vs `{'method': 'email_code'}`),
  invalidates all sessions, clears any pending email-code row.
- Settings → Password recovery: shows the count of unused codes
  (never the codes themselves), a create/regenerate button
  (`POST /recovery-codes/regenerate`, CSRF, 5/hour), and the codes
  shown once after generation with a "save them now" warning.
- Feed shows a one-time "Save your recovery codes" nudge (owner
  only, first page) until the user has at least one code.
- `/send-reset-code` no-SMTP message now points at recovery codes
  instead of "ask the administrator to configure mail".
- Bug fixes: failed SMTP delivery no longer deletes the code row
  (it is kept until it expires); verify rate-limit breach now
  returns 429 via a new `status` parameter on `reset_password_form`.

### What did NOT change
Email-code flow, password policy, PBKDF2, CSRF, same-origin,
security headers, rate limits, private-account privacy, blocks,
media authorisation, backups, audit log, notifications, messages,
Quad Radio, Listening Notes, Top 5 features, Dockerfile,
render.yaml, nginx, start.sh, README, smoke_test.py. No existing
functionality was deleted and no user data was removed. No
credentials or tokens were added. No deployment, no GitHub upload,
no Render change.

### Tests (dev only, temp DBs wiped after each run)
- `python3 smoke_test.py` — PASS
- `python3 merge_test.py` — 51 passed, 0 failed (unchanged)
- `python3 auth_test.py` — 25 passed, 0 failed (unchanged)
- `python3 security_test.py` — 84 passed, 0 failed (unchanged)
- `python3 fix_test.py` — 32 passed, 0 failed (unchanged)
- `python3 listening_test.py` — 44 passed, 0 failed (unchanged)
- `python3 top5_test.py` — 78 passed, 0 failed (unchanged)
- `python3 forgot_test.py` (new, dev-only) — **29 passed, 0 failed**.
  Covers: /login forgot link; forgot page honesty; unknown vs known
  email identical (no enumeration); no code row when SMTP is off;
  email-code reset, single-use, old password dead, new password
  works, audit method email_code; wrong code counted then still
  usable; expired code fails and row removed; 5th wrong attempt
  deletes the row; weak/mismatched passwords rejected; forged
  reset token rejected; verify rate limit 429 on the 11th attempt;
  feed nudge shown with no codes and gone once codes exist;
  Settings shows none, then 10 codes once in the right format with
  a shown-once warning, then only the count; codes hashed in the DB;
  regenerate without CSRF 403; recovery-code reset with no email,
  reuse fails, 9 codes left, audit method recovery_code; wrong
  recovery code fails generically and consumes nothing; recovery
  code for an unknown email fails generically; regenerate replaces
  old codes (old ones dead, new ones work); old session invalidated
  by a reset; regenerate rate limit 429 on the 6th attempt;
  real /register still returns the generic thanks page and the new
  account sees the feed nudge. Total across all suites: **343
  checks + smoke PASS, 0 failed.**

### Bugs found and fixed during testing
1. **Verify rate limit returned 200, not 429:** the reset-verify
   limit was enforced, but `reset_password_form` always returned
   200. Added a `status` parameter (default 200) and the rate-limit
   branch now returns 429. Found by the new test, not by inspection.
2. **Failed SMTP delivery deleted the code:** the async sender
   removed the code row when sending failed, so the user was told a
   code was on its way when none existed. The row is now kept until
   it expires (10 minutes); a new request overwrites it.
3. **Wrong file edited once during this work:** an edit for the
   regenerate POST route briefly landed in the GET routing instead
   (it would have generated codes on every Settings visit). Caught
   immediately, reverted, and re-applied in the POST handler; the
   smoke test and the full suite pass after the fix.

### Remaining issues / known risks
- **SMTP is still not configured.** Emailed reset codes still
  cannot be sent until Mitchell provides real SMTP credentials
  (SMTP_HOST, SMTP_FROM, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD)
  on the server — never in code. Recovery codes are the working
  path until then, and they only help users who created them while
  signed in. A user who never saved recovery codes and forgets
  their password still has no self-service way back in; the only
  fallback is an admin removing/restoring the account.
- Recovery codes are shown once and cannot be recovered — if a user
  loses them, they must create a new set while signed in.
- A recovery code is as powerful as a password until used: anyone
  who photographs or copies a user's codes can reset that account
  (they still need the email address and a new password that passes
  the policy, and the reset is audit-logged and invalidates all
  sessions, so the owner is signed out and can see it in the log).
- After a reset, remaining recovery codes stay valid by design;
  the success page tells the user they can create a fresh set.
- The dead `users.recovery_hash` column is still there (cleared on
  email-code reset as before); it is not used by recovery codes.
- Layout was checked via CSS/HTTP rendering, not in a real
  phone/desktop browser — Mitchell should eyeball the forgot page,
  the reset form and Settings → Password recovery on his phone
  before approving.
- No live deployment was done and none is claimed. Deployment still
  needs Mitchell's explicit approval, per his instructions.

## Update 8 — Dual backend with failover + shared synchronised data layer (DEV ONLY, 2026-11-19)
Dev copy only. Nothing deployed, nothing uploaded to GitHub, nothing
touched on Render, no payment, no credentials invented or stored.
Backup md5 still `aae87ec977f13b371de50aedb192cd96` (re-verified after
this work). Pre-change copy of the code kept in dev as
`the_network_production.py.bak-failover`. File:
`the_network_production.py` 2401 → 2406 lines. New dev-only files:
`failover_test.py` (31 checks), `nginx_failover.conf.template`,
`start_failover.sh`, `DUAL_BACKEND.md` (full architecture doc — read
it for the production path). Original `nginx.conf.template`,
`start.sh`, `Dockerfile` and `render.yaml` are unchanged.

### What Mitchell asked for
A real second backend with failover and a shared synchronised data
layer — not two separate databases that drift apart.

### Architecture (what is real)
- **Two Python processes**, same `the_network_production.py`, on
  different ports (primary 8081, secondary 8082), behind one nginx
  front door (`nginx_failover.conf.template`).
- **Shared data layer — one SQLite file**, `DATA_DIR/network.db`,
  opened by both processes. This was possible without a rewrite
  because the app already had the right foundation: WAL mode
  (`PRAGMA journal_mode=WAL` in `init_db()`), `busy_timeout=10000`,
  `synchronous=NORMAL`, per-request connections, `BEGIN IMMEDIATE`
  in `rate_limit()`. Sessions, CSRF tokens, rate limits, posts,
  accounts, notifications, groups, polls, listening notes and
  recovery codes all live in that one database, so a session created
  by A is valid on B. Uploads live in the shared
  `DATA_DIR/network_uploads/` directory, so a photo uploaded via A
  is served by B — this only works because both processes share one
  disk in dev.
- **Failover (nginx, passive):** upstream `quad_backend`, primary
  `max_fails=2 fail_timeout=10s`, secondary marked `backup`
  (nginx only sends it traffic when the primary is unavailable),
  `proxy_next_upstream error timeout http_502 http_503 http_504`
  with `proxy_next_upstream_tries 2`. Open-source nginx has no
  active health checks — a dead primary is detected when a request
  to it fails. A single in-flight request already accepted by the
  primary before it died is not rescued; the *next* request
  succeeds via the secondary.
- **Both backends MUST share the same `DATA_DIR`, the same
  `APP_SECRET`, the same `PBKDF2_ITERATIONS` and the same
  `ADMIN_EMAILS`.** `APP_SECRET` signs CSRF/reset tokens; different
  secrets per backend would break tokens across a failover.
  `start_failover.sh` generates one secret and exports it to both
  when none is set. Start A first, wait for `/healthz`, then start
  B (both run idempotent `init_db()` and a startup backup).

### Code change (one, small)
- `backup_database()` now names files
  `network-<timestamp>-p<port>.db` instead of
  `network-<timestamp>.db`. With two backends sharing one DATA_DIR,
  both take startup/shutdown backups and could otherwise target the
  same file in the same second. Retention (newest 7, `network-*.db`)
  is unchanged; the security suite's backup glob still matches.

### What did NOT change
Auth/sessions, PBKDF2, CSRF, same-origin, security headers, rate
limits, private-account privacy, blocks, media authorisation, video
duration checks, audit log, notifications, messages, Quad Radio,
Listening Notes, Top 5 features, Forgot Password/recovery codes,
Dockerfile, render.yaml, nginx.conf.template, start.sh, README,
smoke_test.py. No existing functionality was deleted and no user
data was removed. No credentials or tokens were added. **No
Postgres migration was done** — no Postgres server or driver is
available in this dev VM (no `psql`, no `psycopg2`/`psycopg`, no
Docker, no nginx binary — apt has no nginx package here).

### Tests (dev only, temp DBs wiped after each run)
- `python3 smoke_test.py` — PASS
- `python3 merge_test.py` — 51 passed, 0 failed (unchanged)
- `python3 auth_test.py` — 25 passed, 0 failed (unchanged)
- `python3 security_test.py` — 84 passed, 0 failed (unchanged)
- `python3 fix_test.py` — 32 passed, 0 failed (unchanged)
- `python3 listening_test.py` — 44 passed, 0 failed (unchanged)
- `python3 top5_test.py` — 78 passed, 0 failed (unchanged)
- `python3 forgot_test.py` — 29 passed, 0 failed (unchanged)
- `python3 failover_test.py` (new, dev-only) — **31 passed, 0 failed**.
  Covers: both backends start against the same live DB; post via A
  visible on B and vice versa; session created on A valid on B;
  CSRF token issued by A accepted by B; second account via B;
  FTS search on B finds a post written via A; 4 concurrent writes
  (2 via A, 2 via B) all land exactly once, both backends agree;
  front-door proxy serves while A is up, still serves with A killed,
  accepts a post during the outage (in the shared DB exactly once);
  A restarts and the outage post is visible via A; post count is
  exactly 8 with no duplicated bodies; DB integrity check passes;
  proxy still serves after A returns; backups from both processes
  exist with distinct names and pass integrity checks; nginx config
  contains the upstream, both backends, `backup`,
  `proxy_next_upstream` and `max_fails`.
  Total across all suites: **374 checks + smoke PASS, 0 failed.**
- `python3 -m py_compile` — OK. `/app.js` fetched from a live
  server — `node --check` OK.

### Bugs found and fixed during testing
1. **Test-harness bug, not a product bug:** the "post via A visible
   on B" check read B's feed with a client that had not logged in
   yet, so it got the landing page instead of the feed. Fixed the
   test to use the logged-in owner client; the post had been visible
   all along (the later FTS-search check proved it independently).
   First run was 30 passed / 1 failed; after the fix, 31 passed /
   0 failed on a clean re-run.
2. **No product bugs were found** by the failover suite — the shared
   WAL database, shared sessions and shared secret all worked as
   designed on the first live run.

### Honest limits / remaining issues (read before approving)
- **The live front door in the test is a stdlib Python proxy**,
  not real nginx: nginx is not installed in this dev VM and could
  not be run here, so the nginx config is checked by content only,
  not by a live nginx run. It is derived from the existing
  production template (same rate/body limits) and must be validated
  with `nginx -t` in the Docker build before any deploy.
- **This dev setup shares one disk.** Two separate Render services
  cannot share a Render disk, so this exact shape cannot be deployed
  as-is. Production needs managed Postgres (a real migration:
  placeholders, types, FTS5 triggers, the sqlite backup API and
  PRAGMAs all need replacing, suites re-run against Postgres),
  object storage for uploads, and two app services + one nginx
  proxy service — all **paid**. Mitchell has said no payment; do
  not provision any of it without his explicit approval and a cost
  decision. See DUAL_BACKEND.md § "What is NOT production-ready".
- **Single point of failure remains in dev:** one disk, one machine,
  one nginx. True high availability needs the Postgres +
  object-storage shape above.
- **No active health checks** (open-source nginx). Faster detection
  would need nginx Plus or an external health-checker.
- **Write throughput:** SQLite WAL serialises writers (one writer
  at a time, others wait up to 10s). Fine for a small community;
  Postgres removes this limit.
- **Secret rotation:** rotate `APP_SECRET` on both instances at the
  same time, or the other's tokens break.
- **Backup workers:** both processes run a 6-hourly backup worker;
  if their timers ever align, a failed backup only prints an error
  (live DB untouched), and filenames can no longer collide.
- Layout was checked via CSS/HTTP rendering, not in a real
  phone/desktop browser — no user-facing pages changed in this
  update, so there is nothing new to eyeball, but the dual setup
  itself has only been exercised by the test suite.
- No live deployment was done and none is claimed. Deployment still
  needs Mitchell's explicit approval, per his instructions.

### To test again
```
cd ~/workspace/network-merge/dev
python3 smoke_test.py
python3 merge_test.py
python3 auth_test.py
python3 security_test.py
python3 fix_test.py
python3 listening_test.py
python3 top5_test.py
python3 forgot_test.py
python3 failover_test.py
```
To run both backends locally (dev):
```
DATA_DIR=/path/to/shared/data APP_SECRET=<one shared secret> ./start_failover.sh
```

## Update 9 — PythonAnywhere (free) WSGI adapter (DEV ONLY, 2026-11-19)
Dev copy only. Nothing deployed to PythonAnywhere, nothing uploaded
to GitHub, nothing touched on Render, no payment. Backup md5 still
`aae87ec977f13b371de50aedb192cd96` (re-verified). `the_network_production.py`
was **not modified** (still 2406 lines).

### Why
Mitchell has a free PythonAnywhere Beginner account
(username `mitchellwhite`, expected site
`https://mitchellwhite.pythonanywhere.com`, email verified) and wants
friends to use The Quad with accounts saved on a server, paying
nothing. PythonAnywhere web apps are WSGI-only; the backend is a
stdlib `http.server` app with no WSGI entry point.

### What was built
- **New `pythonanywhere_wsgi.py` (218 lines):** WSGI adapter.
  On first use it starts the existing backend on a loopback-only
  port (127.0.0.1, ephemeral) in a daemon thread and proxies each
  WSGI request to it with `http.client` — method, path+query, body
  bytes untouched (multipart uploads work), cookies, Content-Type,
  Origin. It sets `X-Forwarded-Proto: https` (Secure cookies) and
  `X-Forwarded-For: <real client IP>` (backend uses the last XFF
  entry, so rate limiting is per visitor, not per 127.0.0.1).
  Response status/headers/body pass through, hop-by-hop headers
  filtered. It also runs `init_db()`, a startup backup and the
  6-hourly backup worker, as `main()` does. It refuses to start
  with the placeholder APP_SECRET still in place — a real long
  random secret must be set first (it signs CSRF/reset tokens).
  Defaults (overridable by env): code `/home/mitchellwhite/quad`,
  data `/home/mitchellwhite/quad-data`, `PRODUCTION=1`,
  `TRUST_PROXY=1`. No virtualenv needed (stdlib only).
- **New `pythonanywhere_test.py`:** serves the adapter through a
  real `wsgiref` server and drives it over HTTP.
  **12 passed, 0 failed** — healthz, landing, register, login
  (Secure session cookie set), feed + CSRF, post, post visible on a
  second request, `/app.js`, `/app.css`, admin page, DB created in
  DATA_DIR.
- **New `PYTHONANYWHERE.md`:** full setup guide — files to upload,
  dashboard steps (Manual configuration, Python 3.12, paste the
  WSGI file, replace the secret, Reload), where data lives, limits.
- **Smoke test re-run: PASS.** Nothing in the backend broke.

### Test-harness issues found (not product bugs)
1. First run 7/11: Python's `CookieJar` will not return a `Secure`
   cookie over plain HTTP in the local test, so login looked lost.
   Fixed by managing cookies manually in the test client. Real
   PythonAnywhere traffic is HTTPS, so this does not affect deployment.
2. Then 11/12: test registered `owner@example.com`, not in the
   admin-email list, so `/admin` correctly returned 403. Fixed by
   registering with `mitchellwhite018@gmail.com` (the real admin email).

### Honest limits
- **Dual backend / failover (Update 8) does NOT apply here:**
  PythonAnywhere free is one web app / one worker — no second
  backend, no nginx. A worker restart restarts the loopback backend
  with it; the on-disk database survives, so nothing is lost.
- **Video uploads will be rejected:** they need `ffprobe` to verify
  duration; PythonAnywhere free does not provide it, and the backend
  already rejects unverifiable videos. Images (≤ 8 MB) work.
- **Emailed reset codes will not send** until SMTP is configured
  (no credentials exist). Recovery codes (Update 7) are the working
  path.
- Free limits: 100 CPU-seconds/day, one web app. Fine for a small
  friends site, not a high-traffic design.
- The nginx config was not validated live here (nginx is not in this
  VM) — that only matters for the Render/Docker path, not for
  PythonAnywhere, which does not use it.
- No live deployment was done and none is claimed. Deployment still
  needs Mitchell's explicit approval, per his instructions.

### To test again
```
cd ~/workspace/network-merge/dev
python3 pythonanywhere_test.py
python3 smoke_test.py
```

## Update 10 — Render free + Turso free (DEV ONLY, 2026-11-19)
Dev copy only. Nothing deployed, no Render/Turso account created,
nothing uploaded to GitHub, no payment. Backup md5 still
`aae87ec977f13b371de50aedb192cd96` (re-verified). Pre-change code kept
as `the_network_production.py.bak-renderfree`; paid render.yaml kept
as `render.yaml.bak-paid` (render.yaml itself untouched).

### Why
Mitchell chose this route (option 3) after PythonAnywhere's
email-verification gate kept blocking the dashboard. Requirements:
no payment, no card, friends can open a live link, accounts/posts
saved on a server and surviving restarts. Render free alone cannot
do that — its disk is ephemeral, so a plain SQLite file would be
wiped on the first sleep/restart. Turso free (hosted SQLite/libSQL,
5 GB, 100 DBs, 500M reads / 10M writes / 3 GB syncs per month, no
card, GitHub sign-up, no expiry) holds the durable copy.

### What changed
- **New `db_backend.py`** (310 lines). When `TURSO_DATABASE_URL`
  + `TURSO_AUTH_TOKEN` are set, `connect()` opens the DB as a
  libSQL embedded replica (local file in DATA_DIR synced to the
  Turso cloud primary; reads local, writes forwarded to the cloud).
  The wrapper makes libSQL look like sqlite3 to the app:
  name-addressable rows, `with` blocks that commit, `executescript`,
  `lastrowid`, errors translated to `sqlite3.Error` /
  `sqlite3.IntegrityError`. Verified gaps it closes (libsql-
  experimental 0.0.55): no `row_factory`, no context-manager
  protocol, no `.backup()`, errors are plain `ValueError`.
  Without the Turso env vars the app never imports libSQL and runs
  exactly as before, stdlib-only.
- **`the_network_production.py` 2406 → 2449 lines** — two surgical
  changes only: `connect()` dispatches to `db_backend` in Turso
  mode (plus a `TURSO_DATABASE_URL=local` dev-test sentinel: same
  driver, local file, no cloud sync, so the Turso code path can be
  tested without a real Turso DB), and `backup_database()` syncs
  the replica then copies the local file with the sqlite3 backup
  API. No routes, auth, or features changed.
- **New `render-free.yaml`** — `plan: free`, no disk,
  `healthCheckPath: /healthz`, `DATA_DIR=/tmp/quad-data`
  (ephemeral on purpose — Turso is the durable copy),
  `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` as `sync: false`
  secrets, `APP_SECRET` generated by Render.
- **New `requirements.txt`** — `libsql-experimental==0.0.55`.
- **`Dockerfile`** — installs requirements.txt and copies
  `db_backend.py` into the image. Nothing else changed.
- **New `RENDER_FREE.md`** — full deploy steps and limits.
- **New `render_free_test.py`** — 30 checks (see Tests).

### Bugs found and fixed during testing
1. **Wrapper wrapped the wrong object (real bug):** `_Cursor`
   held the libSQL *connection*, so `lastrowid` raised
   `AttributeError` (it lives on the cursor). Fixed so
   `_Connection.execute()` wraps the cursor `execute()` returns.
2. **Backup crashed the server at startup in Turso mode (real
   bug):** `backup_database()` passed the `'local'` dev sentinel
   to libSQL as a sync URL; the Rust driver panicked on the
   invalid URI, and `pyo3 PanicException` is a `BaseException`,
   so it escaped `except Exception` and killed `main()` before
   the server started. Fixed: `backup_turso()` skips the sync
   step for the `'local'` sentinel, and catches `BaseException`
   so a backup failure can never take the server down.
3. **Test check too strict (test bug):** the "no disk" check
   searched for the substring `disk:` and matched the word in
   comments. Fixed to match a `disk:` YAML block only.

### Tests (dev only, temp DBs wiped after each run)
- `python3 render_free_test.py` — **30 passed, 0 failed**.
  Wrapper unit (rows, context manager, IntegrityError, FTS5,
  backup with a bad sync URL returning False), end-to-end in
  Turso mode (register/login/post/feed/FTS search/admin, backup
  file created), data + session surviving a restart, unreachable
  sync URL failing loudly (never serves /healthz), and
  render-free.yaml content checks. The test installs
  libsql-experimental into `/tmp/quad-turso-venv` itself if
  missing.
- All suites re-run after the change, all pass:
  smoke **PASS**, merge 51, auth 25, security 84, fix 32,
  listening 44, top5 78, forgot 29, failover 31,
  pythonanywhere 12, render_free 30 — **416 checks + smoke PASS,
  0 failed**.
- `python3 -W error -m py_compile` OK for the changed files.
  Docker build was NOT run (no Docker in this dev VM) — the
  Dockerfile change must be proven by the first Render build.

### Honest limits — read before approval
- **Real Turso cloud sync is UNTESTED in dev.** No Turso account
  was created here. The wrapper and the whole app are tested
  end-to-end against a local libSQL file (same driver, same code
  path). The first real deploy is the first live sync test. If
  the sync URL is unreachable at startup the app fails loudly
  instead of silently running local-only (tested).
- **Uploads are still ephemeral.** Photos/videos go to
  `DATA_DIR/network_uploads/` on Render's disk and are lost when
  the instance sleeps/restarts. Database content (accounts, posts,
  friends, messages) survives via Turso; an uploaded photo on a
  post will break after a restart. Proper fix needs object
  storage (e.g. Cloudflare R2 free tier) — a follow-up, not in
  this build.
- **Sleep / cold start:** free instances spin down after 15 min
  idle; first visitor after that waits 30–60 s. 750
  instance-hours/month per workspace — one service fits.
- **Dual backend / failover (Update 8) does NOT apply here.**
  Render free is one instance only. Turso protects the *data*,
  not uptime.
- **Video uploads will be rejected** (ffprobe duration checks);
  images ≤ 8 MB work.
- **Emailed reset codes will not send** until SMTP is configured
  (no credentials; Render free also blocks ports 25/465/587).
  Recovery codes (Update 7) are the working path.
- **Turso write limit (10M/month)** is the first limit to watch
  if the site ever gets busy — the app writes on many requests
  (page-view logging throttled, sessions/last_seen updates).
- **APP_SECRET must stay stable** once Render generates it;
  regenerating it logs everyone out and breaks reset links.
- No live deployment was done and none is claimed. Deployment
  still needs Mitchell's explicit approval, per his instructions.

### To test again
```
cd ~/workspace/network-merge/dev
python3 render_free_test.py
python3 smoke_test.py
```

## Update 11 — Object storage for photos (DEV ONLY, 2026-11-19)
Dev copy only. Nothing deployed, no Cloudflare/R2/Tigris account
or bucket created, nothing uploaded to GitHub, no payment. Backup
md5 still `aae87ec977f13b371de50aedb192cd96` (re-verified).
Pre-change code kept as `the_network_production.py.bak-objstorage`;
render-free.yaml kept as `render-free.yaml.bak-objstorage`.
File: `the_network_production.py` 2449 → 2579 lines.

### Why
Update 10 fixed the database (Turso) but left uploads on Render
free's ephemeral disk: every photo broke on the first sleep/
restart. Mitchell asked for object storage for photos.

### Research — which free store (verified 2026-11-19, re-verify on
### the provider's own pricing page before signing up)
- **Cloudflare R2 (recommended on limits):** 10 GB storage, 1M
  Class A writes/month, 10M Class B reads/month, $0 egress,
  no expiry, S3-compatible SigV4 API, endpoint
  `https://<account-id>.r2.cloudflarestorage.com`, region `auto`,
  buckets private by default. **Caveat that matters here:
  several sources say activating R2 requires a payment method
  on file** (nothing is charged inside the free limits, but the
  checkout flow asks for a card). That conflicts with Mitchell's
  no-payment rule, so the deploy steps say: if R2 asks for a
  card, do NOT add one — use the fallback.
- **Tigris (no-card fallback):** 5 GB storage, zero egress, no
  card required, S3-compatible, endpoint
  `https://fly.storage.tigris.dev`. Much smaller free op
  allowance (10K Class A / 100K Class B per month) — fine for a
  small friends site, the read counter is the thing to watch.
- **Backblaze B2 (second fallback):** 10 GB, no card, 1 GB/day
  free downloads, egress capped at 3x stored volume.
- The code is provider-agnostic: any S3-compatible store works
  with the same four env vars; only the endpoint/keys change.

### Design decision — private bucket, proxied through the app
A public bucket URL (`r2.dev` / custom domain) would let anyone
with the link see a photo the app itself would refuse to show
them (private accounts, blocks). So the bucket stays private,
there is no public URL, no `r2.dev`, no CORS, and no CSP change:
the app fetches bytes server-side with its own key and serves
them through the existing `/media/<key>` route after the same
`can_view_upload` permission check. Feed, post detail and avatar
rendering are untouched — every `/media/<key>` link keeps
working, and `img-src 'self'` still holds.

### What changed
- **New `object_storage.py`** (stdlib only, ~330 lines) — a
  minimal S3 client: SigV4 signing (hashlib/hmac), PUT/GET/
  DELETE over `urllib`, path-style `/<bucket>/<key>`, key
  validation (no `/`, `\` or `..`). Config from
  `OBJECT_STORAGE_ENDPOINT` / `OBJECT_STORAGE_BUCKET` /
  `OBJECT_STORAGE_ACCESS_KEY` / `OBJECT_STORAGE_SECRET_KEY`
  (+ optional `REGION`, default `auto`, and `PREFIX`). No boto3:
  it is a large dependency for four operations, and the app is
  stdlib-only by design — so requirements.txt is unchanged.
- **`the_network_production.py` 2449 → 2579 lines:**
  - `uploads` gains a `storage` column (`'local'` default via
    `add_column`, so legacy rows keep serving from disk).
  - `save_upload()` validates exactly as before (magic bytes,
    8 MB images / 25 MB videos, ffprobe duration via a temp
    file), then PUTs to the bucket when configured, or writes
    local disk as before. **No silent fallback:** a failed PUT
    raises a plain "Photo storage is unavailable" error and
    creates nothing — a local copy would look fine and vanish
    on the next restart.
  - `GET /media/<key>` checks permission first, then fetches
    from the bucket (object rows) or disk (local rows). A down
    bucket 404s that one file; the post still renders.
  - Delete-post, avatar-replace, admin remove/restore and
    self-delete all go through new helpers
    (`upload_storage`, `delete_stored_file`, `move_to_removed`,
    `restore_stored_file`). Object files stay in the bucket
    across admin remove/self-delete (so the 30-day restore can
    bring them back — the DB row is re-inserted with
    `storage='object'`); local files move to `removed_uploads/`
    as before. Object deletes are best-effort and never raise.
  - Startup log line states whether object storage is enabled
    (endpoint/bucket only — never keys).
- **`render-free.yaml`:** four `OBJECT_STORAGE_*` env vars as
  `sync: false` secrets, region `auto`. No values in the file.
- **`Dockerfile`:** copies `object_storage.py`. Nothing else.
- **`RENDER_FREE.md`:** new "Object storage for photos" section
  with deploy steps for R2 and Tigris, and the test counts
  updated (484 checks + smoke PASS).
- **New `object_storage_test.py`** — 68 checks (see Tests).

### Bugs found and fixed during testing
1. **Edit broke the Handler class (real bug, caught by
   py_compile before any test ran):** the new storage helpers
   were inserted at module level in the middle of the class
   body, so `save_upload` fell out of the class and `do_POST`
   raised IndentationError. Fixed by moving the helpers above
   `class Handler` and re-indenting `save_upload` as a method.
   Compile is clean (`-W error`).
2. No product bugs found in the storage code itself — the
   first full test run was 68/68.

### Tests (dev only, temp DBs wiped after each run)
- `python3 object_storage_test.py` — **68 passed, 0 failed**.
  Fake-S3 unit tests (SigV4 header shape + payload hash,
  PUT/GET/DELETE round-trip, bad keys refused, dead endpoint
  raises, unconfigured refuses), end-to-end in object mode
  (photo post → bucket, not disk; feed renders /media/;
  friend 200 / stranger 404 / signed-out 401, with the
  stranger 404 never hitting S3; avatar replace deletes the
  old object; delete-post deletes the object; 1s video via
  ffprobe into the bucket; legacy local photo still serves
  with object mode on; admin remove keeps the object and
  restore brings it back), storage-down loud failure (400,
  no post, no local file, no row), local fallback unchanged,
  yaml/Dockerfile/requirements checks.
- All suites re-run after the change, all pass:
  smoke **PASS**, merge 51, auth 25, security 84, fix 32,
  listening 44, top5 78, forgot 29, failover 31,
  pythonanywhere 12, render_free 30, object_storage 68 —
  **484 checks + smoke PASS, 0 failed**.
- `python3 -W error -m py_compile` OK. Docker build was NOT
  run (no Docker in this dev VM) — the Dockerfile change
  (copy object_storage.py) must be proven by the first Render
  build.

### Honest limits — read before approval
- **A real R2/Tigris bucket is UNTESTED in dev.** No account
  or bucket was created. Tests run against a local fake S3
  server (same HTTP + SigV4 code path); the fake checks the
  SigV4 header shape and payload hash, not a full signature
  recomputation. The first real deploy is the first live-bucket
  test — verify an upload survives a service restart before
  calling it done.
- **R2 may ask for a card at signup.** If it does, do NOT add
  one (Mitchell's no-payment rule) — use Tigris, which needs
  no card but has a much smaller free op allowance.
- **No migration of old photos.** Photos uploaded before
  object storage was switched on stay on local disk and will
  still break on restart. A migration script (copy local files
  up, flip `storage` to `'object'`) is a follow-up, not built.
- **Every `/media/` view is one Class B read** from the bucket
  (one Class A write per upload). On R2 that is 10M reads/month;
  on Tigris only 100K — watch the counter if the site gets busy.
- **If the bucket is down**, uploads fail and existing photos
  404 until it recovers; posts, accounts and everything in
  Turso keep working.
- **Orphaned objects are not cleaned up** (e.g. an object whose
  restore window expired, or a delete whose bucket call failed
  — the DB row is the source of truth, so orphans are
  unreachable, just wasted bytes).
- **Dual backend / failover (Update 8) does NOT apply** —
  Render free is one instance; object storage protects the
  files, not uptime.
- No live deployment was done and none is claimed. Deployment
  still needs Mitchell's explicit approval, per his instructions.

### To test again
```
cd ~/workspace/network-merge/dev
python3 object_storage_test.py
python3 smoke_test.py
```

## Update 12 — Rename to The Campus (DEV ONLY, 2026-11-19)
Dev copy only. Nothing deployed, nothing uploaded to GitHub, no payment.
Backup zip md5 still `aae87ec977f13b371de50aedb192cd96` (re-verified).
Pre-change code kept as `the_network_production.py.bak-campus`.
Mitchell chose **The Campus** as the new professional name (Facebook-style
social network, professional and respectable). User-facing branding renamed
everywhere served: page titles, header brand (`the campus.`), hero
("Started in a dorm room. Now it's the whole campus."), login/register
screens, feed hero, Campus Moments (was Quad Moments), Campus Radio (was
Quad Radio, incl. "Campus Anthem"), Explore ("Around the Campus"), Groups
copy ("A group is a circle on The Campus"), event location placeholder,
password-reset email subject, export filename (`the-campus-export.json`),
footer, admin Traffic Shield copy, Spotify/Live-sync copy, server header
(`TheCampus`), startup log line. Nods, Your Circle, Listening Notes, all
features/routes/behaviour unchanged — branding only.

### Deliberately unchanged (technical identifiers, per task)
- GitHub repo `Kazi477/the-quad`, Render service `the-quad-free`,
  Turso database name `the-quad`, DB file `network.db`, session cookie,
  `render.yaml`/`render-free.yaml` service names, `DATA_DIR` paths.
- Internal code identifiers that are stored or wired into the app:
  music-track keys `quad-dawn` / `quad-night` / `quad-quad` (stored in
  `posts.music_track` — renaming them would orphan existing posts),
  CSS classes `.quad-hero` / `.quad-pill`, `data-quad-track` attributes,
  `quad_backend` nginx upstream, `quad_app` test module alias, temp-dir
  prefixes, `#quadlife` test hashtag, and test post bodies that say
  "quad" (user content, not branding). The docstring still says
  "(formerly The Quad, originally The Network)" as history.
- Suggested object-storage bucket renamed to `the-campus-media` in
  RENDER_FREE.md / object_storage.py docstring (bucket does not exist
  yet, so this costs nothing); the object_storage test still uses
  `the-quad-media` as an arbitrary test value.
- Docs updated: RENDER_FREE.md (title + note on unchanged technical
  identifiers), PYTHONANYWHERE.md, DUAL_BACKEND.md, db_backend.py,
  object_storage.py, pythonanywhere_wsgi.py docstrings/error bodies.

### Tests (dev only, temp DBs wiped after each run)
- All suites re-run after the rename: smoke **PASS**, merge **51**, auth **25**,
  security **84**, fix **32**, listening **44**, top5 **78**, forgot **29**,
  failover **31**, pythonanywhere **12**, render_free **30**, object_storage
  **68** — **484 checks + smoke PASS, 0 failed** (same total as before the
  rename; no checks added or removed).
- `python3 -W error -m py_compile the_network_production.py` OK.
- Tests that asserted the old brand were updated to The Campus /
  Campus Moments / Campus Radio (auth, merge, fix, forgot, listening,
  security, pythonanywhere, top5). No test behaviour changed.

### To test again
```
cd ~/workspace/network-merge/dev
python3 smoke_test.py
```

## Update 13 — Fix live 502 Bad Gateway: broken Turso replica WAL (DEV ONLY, 2026-11-19)

### What happened live
- The Campus went live on Render free 19 Nov 2026 21:28 AEST
  (https://the-quad-free.onrender.com, commit dd5e30d). Homepage
  loaded once ~21:30, then every GET / returned 502, while
  /healthz stayed ok. Live logs 21:36 AEST: every homepage
  request crashed in rate_limit() — BEGIN IMMEDIATE + INSERT INTO
  rate_limits raised `sqlite3.Error: wal_insert_begin failed`
  (db_backend.py line 196 in the live build). The exception
  escaped do_GET, the handler closed the connection without a
  response, and Render/nginx answered 502.
- Root cause, in plain terms: the Turso embedded replica keeps a
  local copy of the database in /tmp/quad-data/network.db plus a
  write-ahead log (-wal/-shm files). When Render spins the free
  instance down it kills the process uncleanly, and /tmp can keep
  those WAL files in a half-written state. Reads still work
  (so /healthz, which only does SELECT 1, looks healthy), but
  every real write fails with wal_insert_begin failed. And every
  GET / writes first — do_GET calls rate_limit('view-log', ...)
  before rendering anything — so one broken WAL 502s the whole
  site. Aggravating factors: ThreadingHTTPServer opens a fresh
  libSQL replica connection per request (each with its own
  60-second background sync), and init_db() forced
  `PRAGMA journal_mode=WAL` on the replica at every startup.
- This was NOT reproducible end-to-end in dev with a real Turso
  cloud replica (no cloud account in this VM); the failure was
  reproduced exactly by fault injection (a wrapper that makes
  every DB write raise wal_insert_begin failed), which is what
  wal502_test.py does.

### What changed (dev only — NOT deployed, NOT uploaded to GitHub)
- Pre-change code kept as `the_network_production.py.bak-wal502`
  and `db_backend.py.bak-wal502`. Backup zip md5 re-verified
  unchanged before and after (aae87ec977f13b371de50aedb192cd96).
- `the_network_production.py` 2579 → 2683 lines:
  - rate_limit() (Update 13): a transient WAL/lock failure is
    retried 3 times with backoff (0.05s/0.1s), then falls back to
    thread-safe in-memory counters (same window/limit logic) and
    NEVER raises for a DB write failure. Non-transient DB errors
    still raise, as before. Warns in the log at most once a
    minute per action.
  - do_GET(): the page-view logging block (rate_limit + page_views
    INSERT + last_seen UPDATEs) is wrapped in try/except — logging
    is bookkeeping and must never 502 a page.
  - touch() and session()'s expired-session DELETE: same guard,
    bookkeeping writes only.
  - init_db(): `PRAGMA journal_mode=WAL` now runs only for plain
    sqlite3, never on a libSQL replica (the driver manages its
    own journal).
- `db_backend.py` 329 → 479 lines:
  - connect_turso() now runs a once-per-process WAL self-check
    (`PRAGMA wal_checkpoint(TRUNCATE)`) on first open. If the WAL
    is broken: close, remove stale -wal/-shm sidecars, reopen;
    if still broken AND there is a real libsql:// sync URL,
    delete the local file and re-sync fresh from Turso (the cloud
    primary is the source of truth). The local file is NEVER
    deleted when there is no sync URL (local/dev mode safety).
    If even opening the replica fails with a WAL error, the
    sidecars are removed and open is retried once.
  - busy_timeout raised 10s → 30s.
- New `wal502_test.py` — 20 checks (see Tests below).
- No branding, features, routes, auth or behaviour changed when
  the DB is healthy. No payment, no credentials invented.

### Tests (dev only, temp DBs wiped after each run)
- `python3 wal502_test.py` — **20 passed, 0 failed**: rate_limit
  falls back to memory on wal failure and still enforces the
  limit (21st call denied); retry lands the write after 2
  transient failures; GET / returns 200 (homepage renders) and
  /healthz 200 while EVERY DB write fails; garbage -wal/-shm
  sidecars do not break open/write; forced WAL failure triggers
  recovery with data intact and sidecars removed; unhealable
  replica in local mode fails loudly and the file is NOT deleted.
  Two real bugs were caught by this suite while writing it (an
  UnboundLocalError in the recovery path — Python clears `exc`
  after the except block — and a per-connection retry counter)
  and fixed before the full run.
- All suites re-run after the fix: smoke **PASS**, merge **51**,
  auth **25**, security **84**, fix **32**, listening **44**,
  top5 **78**, forgot **29**, failover **31**, pythonanywhere
  **12**, render_free **30**, object_storage **68**, wal502 **20**
  — **504 checks + smoke PASS, 0 failed**.
- `python3 -W error -m py_compile` OK for all changed files.

### Remaining risks (honest)
- The real Turso cloud sync + a real Render spin-down still
  cannot be tested in dev; the first live restart after deploy
  is the true proof. The fix makes a broken WAL self-heal at
  startup AND makes pages survive even if it does not heal, so
  the 502 should not recur either way — but watch the first
  restart's logs for "Turso replica WAL looks stale" / "rebuilt
  from Turso" lines.
- While the DB is unusable, rate limiting is per-process memory
  only (resets on restart; fine on the single free instance) and
  page-view/traffic counts will be under-counted for that window.
  Real writes (posts, logins, registrations) still fail loudly
  if the replica cannot write — they are user actions the user
  can retry, and the startup WAL recovery is what heals them.
- Concurrent libSQL connections (one per request thread, each
  with a 60s background sync) can still transiently collide on
  the WAL; the retry + fallback now absorbs that, but the
  underlying contention is unchanged.
- If the replica has to be rebuilt from Turso, any writes that
  were only in the local WAL and never reached the cloud are
  lost — that is inherent to the embedded-replica design, and
  is why the rebuild is a last resort after the sidecar removal.

### To deploy after Mitchell approves
- A Render restart/redeploy IS needed: the fix is code, not a
  setting. Redeploy from GitHub after uploading (commit dd5e30d
  is what is live now; this fix is not on GitHub yet). No env
  var or secret changes. After deploy, verify: GET / 200,
  /healthz ok, register/login/post work, then let the instance
  sleep and check GET / still returns 200 after it wakes.

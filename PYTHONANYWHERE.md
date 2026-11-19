# The Quad on PythonAnywhere (free Beginner account) — setup guide

**Account:** username `mitchellwhite`, site `https://mitchellwhite.pythonanywhere.com`
**Status:** DEV ONLY preparation. Nothing here has been deployed to
PythonAnywhere, nothing uploaded to GitHub, no payment. The WSGI adapter
was tested locally (see below).

## Why a WSGI adapter is needed

`the_network_production.py` is a stdlib `http.server` app — it binds a
port and serves requests itself. PythonAnywhere web apps are WSGI-only:
they load a file exposing `application(environ, start_response)` and do
not run a long-lived server on a port. Rewriting the app as native WSGI
would touch all 2,406 lines.

`pythonanywhere_wsgi.py` (in this folder) is the adapter: on first use
it starts the existing backend on a loopback-only port (127.0.0.1,
ephemeral, never exposed) in a daemon thread, and proxies each WSGI
request to it with `http.client`. The backend is **unchanged** — same
routes, auth, sessions, CSRF, rate limits, SQLite database. It also
runs `init_db()`, a startup backup, and the 6-hourly backup worker,
exactly as `main()` does.

## Files to upload to PythonAnywhere

Upload these two files to `/home/mitchellwhite/quad/` (create that
folder first — use the PythonAnywhere **Files** tab, or the Bash console
`mkdir -p ~/quad ~/quad-data`):

| File (from this dev folder) | Put it at |
|---|---|
| `the_network_production.py` | `/home/mitchellwhite/quad/the_network_production.py` |
| `pythonanywhere_wsgi.py` | `/home/mitchellwhite/quad/pythonanywhere_wsgi.py` |

No other files are needed to run (tests, Dockerfile, render.yaml,
nginx templates are not used on PythonAnywhere). No virtualenv is
needed — the app uses the Python standard library only. No `pip`
packages to install.

## Step-by-step setup (in the PythonAnywhere dashboard)

1. **Sign in** to https://www.pythonanywhere.com as `mitchellwhite`
   (email must already be verified — the account was registered
   2026-11-19 and the confirmation link was clicked).
2. **Web tab → Add a new web app.**
   - Domain: accept the default `mitchellwhite.pythonanywhere.com`.
   - Python version: pick **Python 3.12** (the code needs 3.9+;
     3.12 was used for local testing).
   - Framework: choose **Manual configuration** (NOT Flask/Django —
     this app is not Flask/Django).
   - **Do not** create a virtualenv (none needed).
3. **Set the WSGI configuration file.** On the Web tab, under "Code",
   click the link to the WSGI configuration file
   (`/var/www/mitchellwhite_pythonanywhere_com_wsgi.py`). Delete its
   default contents and paste the **entire contents of
   `pythonanywhere_wsgi.py`** (the file in this folder — it is
   paste-ready, with the paths already set for this account).
   - Before pasting, replace the placeholder in that file:
     `PASTE-A-LONG-RANDOM-SECRET-HERE` with a real long random secret.
     Generate one in a Bash console:
     `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
     **Keep that secret private and stable.** It signs CSRF tokens and
     password-reset tokens — if it changes, everyone is logged out
     and reset links break. The adapter refuses to start with the
     placeholder still in place.
   - The file already sets: code dir `/home/mitchellwhite/quad`,
     data dir `/home/mitchellwhite/quad-data`, `PRODUCTION=1`,
     `TRUST_PROXY=1`.
4. **Source code / working directory** (Web tab, "Code" section): set
   it to `/home/mitchellwhite/quad` if the dashboard asks.
5. **Static files — none to configure.** The app serves its own
   `/app.js` and `/app.css` from the Python code, and uploaded media
   from `/media/` via the app (with its own authorisation checks).
   Do not map a static-files entry for them.
6. **Reload** the web app (green **Reload** button on the Web tab).
   First load takes a few seconds while the loopback backend starts.
7. **Check it:** open `https://mitchellwhite.pythonanywhere.com/`
   — you should see The Quad landing page. Then
   `https://mitchellwhite.pythonanywhere.com/healthz` should return
   `ok`. Register the first account with
   `mitchellwhite018@gmail.com` — that email is in the admin list, so
   that account is the admin/owner.

## Where the data lives

* Database: `/home/mitchellwhite/quad-data/network.db` (SQLite, WAL)
* Uploads: `/home/mitchellwhite/quad-data/network_uploads/`
* Backups: `/home/mitchellwhite/quad-data/backups/` (newest 7 kept,
  taken on startup and every 6 hours while the worker is alive)
* All under the home directory, which is persistent on PythonAnywhere
  free — accounts, posts, friends and messages survive reloads and
  restarts. (This is the key difference from Render free, whose disk
  is wiped on restart.)

## What works / what does not (honest limits)

* **Works:** everything the local backend does — register/login,
  posts, Nods/reactions, threaded replies, reposts, friends, 1:1
  messages, groups, events, polls, search/hashtags/Explore,
  Spotify Listening Notes, notifications, reports/moderation,
  data export, self-delete, Forgot Password **recovery codes**
  (Settings → Password recovery).
* **Video uploads will be rejected.** They need `ffprobe` to verify
  duration server-side; PythonAnywhere free does not provide it, and
  the backend already rejects videos it cannot verify. Image uploads
  (≤ 8 MB) work.
* **Emailed password-reset codes will not send** until SMTP is
  configured (no SMTP credentials exist). Recovery codes are the
  working path.
* **Dual backend / failover (Update 8) does NOT apply here.**
  PythonAnywhere free is one web app / one worker — there is no
  second backend and no nginx front door. If the worker restarts,
  the loopback backend restarts with it; the on-disk database
  survives, so nothing is lost.
* **Free limits:** 100 CPU-seconds/day, one web app. Fine for a
  small friends/community site; the site will be slow or throttled
  if it ever gets genuinely busy.
* **Reloads:** after editing code or the WSGI file, press **Reload**
  on the Web tab. A reload restarts the worker (and the loopback
  backend); sessions live in the database, so signed-in users stay
  signed in — *provided APP_SECRET has not changed*.

## Local test results (dev, 2026-11-19)

* `pythonanywhere_test.py`: **12 passed, 0 failed** — healthz, landing
  page, register, login (Secure session cookie set), feed + CSRF,
  post, post visible on a second request, `/app.js`, `/app.css`,
  admin page, database created in DATA_DIR. Served through a real
  `wsgiref` server in a thread, temp DB wiped afterwards.
* `smoke_test.py`: **PASS** (re-run after the adapter was added;
  `the_network_production.py` itself was not modified).
* Two test-harness issues found and fixed (not product bugs):
  1. First run was 7/11 — Python's `CookieJar` will not return a
     `Secure` cookie over plain HTTP in the local test, so login
     looked lost. Fixed by managing cookies manually in the test
     client. On PythonAnywhere real traffic is HTTPS, so this does
     not affect production.
  2. Then 11/12 — the test registered `owner@example.com`, which is
     not in the admin-email list, so `/admin` correctly returned 403.
     Fixed by registering with `mitchellwhite018@gmail.com` (the real
     admin email, as in deployment).

## To test again

```bash
cd ~/workspace/network-merge/dev
python3 pythonanywhere_test.py
python3 smoke_test.py
```

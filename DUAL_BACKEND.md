# Dual backend with failover — The Quad (DEV ONLY, 2026-11-19)

## What this is
Two Python backend processes, both running the same
`the_network_production.py`, behind one front door (nginx). If the
primary backend dies, traffic goes to the secondary with no data lost,
because both backends read and write **the same data** — they do not
each have their own copy that could drift apart.

## Architecture (what is real in dev)

```
            ┌──────────────┐
  users ──► │ nginx :PORT  │  upstream quad_backend
            │  (failover)  │    primary  127.0.0.1:8081  (max_fails=2)
            └──────┬───────┘    backup   127.0.0.1:8082
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
  backend A (8081)    backend B (8082)
  the_network_          the_network_
  production.py         production.py
        │                     │
        └──────► SHARED ◄──────┘
              DATA_DIR/network.db
              (SQLite, WAL mode)
              + network_uploads/ (shared disk)
```

### Shared data layer — genuinely shared, not two copies
- **One SQLite file**, `DATA_DIR/network.db`, opened by both
  processes. SQLite **WAL mode** (`PRAGMA journal_mode=WAL`, set in
  `init_db()`), `busy_timeout=10000` and `synchronous=NORMAL` were
  already in the app; per-request connections (`connect()`) mean each
  request sees the latest committed data from either process.
- Writers serialise in SQLite (one writer at a time; the other waits
  up to the 10-second busy timeout). `rate_limit()` already uses
  `BEGIN IMMEDIATE`. This is correct for a small community; it is not
  a high-write-throughput design.
- Everything that must survive a failover already lives in that one
  database: users, posts, comments, **sessions**, CSRF tokens,
  rate limits, notifications, groups, polls, listening notes,
  recovery codes. A session created by A is valid on B — proven in
  `failover_test.py`.
- Uploads live in the shared `DATA_DIR/network_uploads/` directory,
  so a photo uploaded via A is served by B. This only works because
  both processes share one disk in dev.
- **Both backends MUST use the same `DATA_DIR`, the same
  `APP_SECRET`, the same `PBKDF2_ITERATIONS` and the same
  `ADMIN_EMAILS`.** `APP_SECRET` signs CSRF/reset tokens
  (HMAC-SHA256); if the two processes had different secrets, a token
  minted by A would be rejected by B. `start_failover.sh` generates
  one secret and exports it to both when none is set.
- **Startup order matters:** start A, wait for `/healthz`, then start
  B. Both run `init_db()` (idempotent `CREATE TABLE IF NOT EXISTS`)
  and a startup backup; staggering avoids two processes running
  first-run migrations at the same instant.
- **Backups:** `backup_database()` now names files
  `network-<timestamp>-p<port>.db` so the two processes' startup /
  shutdown backups cannot target the same file in the same second.
  Retention (newest 7, `network-*.db`) is unchanged. Both processes
  also run a 6-hourly backup worker; if their timers ever align, a
  failed backup only prints an error — the live database is untouched.

### Failover — how it works
- nginx `upstream quad_backend`: primary `127.0.0.1:8081`
  (`max_fails=2 fail_timeout=10s`), secondary `127.0.0.1:8082`
  marked `backup`. nginx only sends traffic to the backup when the
  primary is unavailable.
- `proxy_next_upstream error timeout http_502 http_503 http_504`
  with `proxy_next_upstream_tries 2`: a request that fails against
  the primary is retried once against the secondary.
- This is **passive** failover (open-source nginx has no active
  health checks). A dead primary is detected when a request to it
  fails; after 2 failures in 10 seconds it is kept out of rotation
  for 10 seconds. A single in-flight request that was already
  accepted by the primary before it died is not magically rescued —
  the *next* request succeeds via the secondary. That is the honest
  limit of this setup.
- Config: `nginx_failover.conf.template` (derived from the existing
  production `nginx.conf.template`, same rate limits / body limits).
  The original single-backend `nginx.conf.template`, `start.sh`,
  `Dockerfile` and `render.yaml` are **unchanged**.

### What was tested
`failover_test.py` (dev only, temp DB wiped after each run) proves:
1. Both backends start against the same live database.
2. A post/account made via A is visible via B, and vice versa.
3. A session + CSRF token from A works on B.
4. Concurrent writes from both backends all land exactly once —
   no lost or duplicated posts.
5. With the primary killed, requests through the front-door proxy
   still succeed via the secondary, a post made during the outage
   is stored, and after the primary restarts that post is visible
   via the primary too — no data lost, no duplicates.
6. Backups from both processes exist, have distinct names, and pass
   SQLite integrity checks.
7. The nginx failover config contains the upstream, both backends,
   the `backup` marker, `proxy_next_upstream` and `max_fails`.

The front-door proxy in the test is a small stdlib Python proxy that
implements the same primary-then-backup semantics as the nginx
config, because **nginx is not installed in this dev VM** (no nginx
package in its apt repos) and could not be run here. The nginx
config itself is therefore **not** proven by a live nginx run in
this VM — it must be validated with `nginx -t` in the Docker build
before any deploy. See "Not production-ready yet".

## How to run it locally (dev)
```bash
cd ~/workspace/network-merge/dev
DATA_DIR=/path/to/shared/data APP_SECRET=<one shared secret> ./start_failover.sh
# primary http://127.0.0.1:8081, secondary http://127.0.0.1:8082,
# front door http://127.0.0.1:10000 (nginx)
```
Or run the two backends by hand (same rules: same DATA_DIR, same
APP_SECRET, start A first, wait for `/healthz`, then start B):
```bash
DATA_DIR=/tmp/quad-dual APP_SECRET=... BACKEND_PORT=8081 python3 the_network_production.py &
# wait for http://127.0.0.1:8081/healthz
DATA_DIR=/tmp/quad-dual APP_SECRET=... BACKEND_PORT=8082 python3 the_network_production.py &
```

## What is NOT production-ready yet — Mitchell's decisions needed
1. **This dev setup shares one disk.** Two separate Render
   services cannot share a Render disk, so this exact shape cannot
   be deployed as-is. Production needs:
   - **Managed Postgres** (one database both app instances connect
     to) replacing SQLite. That is a real migration: SQL placeholders
     (`?` → `%s`), `AUTOINCREMENT`/types, FTS5 triggers, the
     `sqlite3` backup API, and `PRAGMA` calls all need replacing,
     and every suite must be re-run against Postgres. Not done here
     — no Postgres server or driver is available in this dev VM.
   - **Object storage** (e.g. S3/R2) for uploads, replacing the
     shared `network_uploads/` directory.
   - **Two app services + one nginx proxy service** (or a managed
     load balancer) on Render, all sharing the same `APP_SECRET`
     env var and the same Postgres. Render does not fail over
     between two web services on its own.
   - All of the above are **paid services**. Mitchell has said no
     payment — do **not** provision any of this without his explicit
     approval and a cost decision.
2. **Single point of failure remains:** one disk, one machine, one
   nginx in dev. True high availability needs the Postgres +
   object-storage shape above, plus managed backups of Postgres.
3. **No active health checks** (open-source nginx). For faster
   detection, nginx Plus or an external health-checker would be
   needed — again a paid/complexity decision.
4. **Write throughput:** SQLite WAL serialises writers. Fine for a
   small community; under heavy concurrent posting, writers wait
   (up to 10s) and then fail. Postgres removes this limit.
5. **Sessions/CSRF across instances depend on the shared secret.**
   If `APP_SECRET` is ever rotated on one instance only, the other
   instance's tokens break. Rotate it on both at the same time.
6. **Layout / real-browser check** still not done for any of this
   (as with all dev work): no user-facing pages changed, so there
   is nothing new to eyeball, but the dual setup itself has only
   been exercised by the test suite, not by a person clicking
   through it.
7. **No deployment was done and none is claimed.** Nothing was
   uploaded to GitHub, nothing changed on Render, no payment was
   made. Deployment needs Mitchell's explicit approval, per his
   standing instructions.

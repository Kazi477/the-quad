# The Network — production build

This bundle hardens the Python/SQLite version of The Network and makes it deployable behind Nginx in Docker.

## What changed

- Persistent data path via `DATA_DIR` for SQLite, uploads, and backups.
- Production mode uses explicit admin emails only; a random first visitor cannot become admin.
- PBKDF2-SHA256 raised to 600,000 iterations with automatic upgrade of older hashes.
- Six-digit, expiring, one-use password reset codes and layered reset throttling.
- Login, registration, reset, posting, messaging, and other write-action rate limits.
- Generic login failures to reduce account enumeration.
- Seven-day session lifetime plus 24-hour idle expiry, bounded sessions per account, Secure/HttpOnly/SameSite cookies.
- CSRF tokens plus cross-site POST rejection.
- CSP no longer allows inline JavaScript; app JavaScript is served from `/app.js`.
- HSTS, Referrer-Policy, Permissions-Policy, COOP/CORP, nosniff and frame protections.
- Private-account search leak fixed; private bios/interests cannot be searched or displayed by non-friends.
- Media authorization fixed; private-post files are no longer fetchable merely by knowing a media key.
- Notification avatar crash fixed.
- Admin audit log survives post/comment deletion and is visible only in the admin dashboard.
- Raw visitor IPs are not persisted by the Python app; abuse throttling stores only keyed one-way identifiers.
- Server-side video duration verification with `ffprobe` in production.
- SQLite WAL/busy-timeout hardening, integrity check at startup, consistent rotating SQLite backups.
- Database-backed health check at `/healthz`.
- Graceful shutdown and bounded server queue/thread behavior.
- Automatic light/dark theme support.
- Nginx reverse proxy adds connection/request limits and request-size limits before traffic reaches Python.

## Render deployment

`render.yaml`, `Dockerfile`, `nginx.conf.template`, and `start.sh` are included. The Blueprint uses a 1 GB persistent disk mounted at `/var/data` and a paid 0.5 CPU / 512 MB web-service plan.

A persistent disk is required because the current architecture stores SQLite data and user uploads on the filesystem. Do **not** deploy this exact architecture on a free Render web service: its filesystem is ephemeral, so accounts, posts, and uploads can disappear on restart/redeploy.

Before going live, configure the SMTP variables if password-reset email is required. Do not commit SMTP passwords to source control.

## Current architecture limit

SQLite + one persistent disk is appropriate for a small friends/community site and intentionally runs as one service instance. It is not the right architecture for large-scale traffic or horizontal scaling. The next scale-up step is managed Postgres plus object storage for media.

## Local test

Run:

```bash
python smoke_test.py
```

The smoke test covers registration/login, security headers, posting, private-profile search privacy, admin access, deletion audit logging, and core session flows.

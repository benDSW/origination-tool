# Deploying to Railway

The tool runs as **one Railway project** with three parts:

| Part | What it is | Config |
|---|---|---|
| **Postgres** | Railway's Postgres database | added from the Railway UI |
| **web** | The dashboard (FastAPI + uvicorn). Runs migrations on every deploy | [`railway.json`](../railway.json) |
| **cron** | The daily origination run, `python -m app.jobs.daily`, at 06:00 **UTC** | [`railway.cron.json`](../railway.cron.json) |

Both services are built from the **same repository**. Railway's builder (Railpack) detects
`pyproject.toml` + `uv.lock` and installs the locked dependencies with uv, so no Dockerfile is
needed.

---

## 1. Create the project and database

1. In Railway, **New Project → Deploy from GitHub repo**, and pick this repository. This first
   service becomes **web**. Rename it to `web`.
2. In the project, **+ New → Database → PostgreSQL**. Railway creates a `DATABASE_URL` variable on
   the Postgres service.

## 2. Configure the web service

1. **Settings → Config-as-code**: the path is `/railway.json` (the default).
   That file sets:
   - start command:
     `python -m alembic upgrade head && python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips '*'`
   - health check: `GET /healthz`
   - restart on failure.

   `--proxy-headers` makes the app see the `https` scheme from Railway's proxy (needed for
   secure cookies). Railway's proxy addresses are not fixed, hence `--forwarded-allow-ips '*'`.
   With `'*'`, uvicorn copies the **leftmost** `X-Forwarded-For` entry into the client address,
   and that entry is whatever the client sent. The login rate limit therefore never uses it: it
   keys on the entry `TRUSTED_PROXY_HOPS` from the **right** (default `1`), which Railway's edge
   appends and a client cannot forge. A global budget (20 failed logins per 15 minutes across all
   addresses) caps guessing regardless, and logs an `ALERT:` line when it trips.
2. **Settings → Networking → Generate Domain** to get a public `*.up.railway.app` URL.
3. Add the variables (see section 4).

Migrations run before uvicorn starts on every deploy. `alembic upgrade head` is idempotent, so a
deploy with no new migrations does nothing. If a migration fails, the app does not start and the
previous deployment keeps serving.

## 3. Add the cron service

1. **+ New → GitHub Repo** and pick the **same repository** again. Rename it to `cron`.
2. **Settings → Config-as-code**: set the path to `/railway.cron.json`. That file sets:
   - start command `python -m app.jobs.daily`
   - cron schedule `0 6 * * *` (**06:00 UTC** every day; Railway cron schedules are always UTC,
     so this is 06:00 in winter and 07:00 UK time in summer)
   - restart policy `NEVER` (a failed run is not retried in a loop; it is recorded and the next
     day's run picks up where it left off).
3. **Remove any public domain** from the cron service. It needs no networking.
4. Give it the **same variables** as web (section 4). Tip: put the shared ones in Railway
   **Shared Variables** and reference them from both services.

The cron service does not run migrations. Deploy **web** first whenever a change includes a
migration.

The cron job and the dashboard's "Run now" button share a database lock, so they never run at
the same time. A run that finds the lock taken records itself as *skipped* and exits 0. The lock
records a random owner token and the run refreshes it (a heartbeat) at least once a minute while
it works, so a long backfill keeps it. A lock left behind by a crashed run expires after
`RUN_LOCK_TTL_SECONDS` (default one hour). If a run ever finds its lock taken over, it stops
(state FAILED, "run lock was lost") instead of running alongside the new holder, and it never
releases a lock it no longer owns.

RocketReach lookups are reserved in the database **before** each paid call (the founder's cache
row and one unit of the day's budget, in one transaction). The daily cap and "never look anyone
up twice" therefore hold even if the database fails after a call, or two runs overlap. If
anything unexpected fails after a paid call, lookups stop for the rest of that run.

## 4. Variables

Set these on **both** services (names match [`.env.example`](../.env.example)):

| Variable | Value |
|---|---|
| `DATABASE_URL` | Reference the Postgres service: `${{Postgres.DATABASE_URL}}`. `postgres://` URLs are converted to the psycopg driver automatically |
| `APP_ENV` | `production` |
| `EVERTRACE_API_KEY` | Evertrace API key (`sk_...`) |
| `ROCKETREACH_API_KEY` | RocketReach API key. Leave empty to disable RocketReach lookups entirely |
| `AFFINITY_API_KEY` | Affinity API key. **Required**: without it founders are never drafted (the Affinity dedupe check cannot run) |
| `ADMIN_PASSWORD_HASH` | Argon2 hash of the dashboard password (see below). Web only, but harmless on cron |
| `SESSION_SECRET` | Long random string (see below). Web only |
| `SUPPRESSION_SECRET` | Long random string, **the same on both services**, generated like `SESSION_SECRET`. Keys the hashes that stop forgotten founders from being re-ingested. **Never rotate it** (forgotten founders could come back). Production refuses to start without it |
| `ROCKETREACH_DAILY_CAP` | Max RocketReach lookups per UTC day (default `40`, max `500`) |
| `AFFINITY_ORIGINATION_LIST_ID` | `123879` |
| `AFFINITY_APPLICANT_LIST_ID` | `310945` |
| `DSW_EMAIL_DOMAINS` | Comma-separated DSW sender domains, e.g. `dsw.vc` |
| `FOLLOW_UP_AFTER_DAYS` | Days after sending before a follow-up is flagged (default `14`) |

Optional: `RUN_LOCK_TTL_SECONDS` (default `3600`), `TRUSTED_PROXY_HOPS` (default `1`: Railway's edge).

### Fail-closed start-up

Both services refuse to start (and log which variable to fix, never its value) when:

- they run on Railway (any `RAILWAY_*` platform variable is present) and `APP_ENV` is not
  `production`. Otherwise cookies would lose `Secure`, HSTS would be off and a missing
  `SESSION_SECRET` would silently become a temporary one;
- `APP_ENV=production` and `DATABASE_URL` is SQLite. Web and cron would each get their own
  temporary file, so they would no longer share the run lock, lookup cache or daily budget;
- `APP_ENV=production` and `SUPPRESSION_SECRET` is missing or shorter than 32 characters.

At start-up each service logs one line with its effective security mode (secure cookies on/off,
database backend, which secrets are set), without any values.

### Generating `ADMIN_PASSWORD_HASH`

On your own machine (the password is never echoed, printed or stored):

```bash
uv run python -m app.jobs.hash_password
```

Type the password twice (at least 12 characters). Copy the printed `$argon2id$...` line into the
`ADMIN_PASSWORD_HASH` variable. In Railway's raw editor the value contains `$` characters, so
paste it as-is in the variable UI (not inside a shell command).

### Generating `SESSION_SECRET`

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Changing it logs everyone out.

### Ending sessions

Sessions are also recorded server-side (only a SHA-256 of a random session id is stored), so:

- **Log out** ends that session everywhere, including any copy of its cookie;
- **Log out everywhere** (top bar) ends every dashboard session, on every device. Use it if a
  cookie or device may be compromised;
- changing `ADMIN_PASSWORD_HASH` ends every existing session after the redeploy.

New password hashes from `app.jobs.hash_password` use explicit Argon2id parameters (19 MiB,
t=2, p=1, the OWASP baseline). At most two password checks run at once; extra login attempts
get "try again in a moment" (HTTP 429) instead of queueing.

## 5. First deploy checklist

1. Deploy **web**. Watch the deploy log for `Running upgrade  -> 0001_initial` and
   `0001_initial -> 0002_security_hardening` (once each), then uvicorn starting. The health check on `/healthz` must pass.
2. Open the web URL, log in, and go to **Searches → Sync**. Enable the searches you want and set
   each one's location policy and lookback.
3. Click **Run now** (or run `python -m app.jobs.daily --dry-run --no-rocketreach` from a Railway
   shell first to rehearse), then check the **Runs** page.
4. Deploy **cron**. The next run happens at 06:00 UTC. To test it immediately, use the cron
   service's **Run now** button in Railway.

## 6. Operating it

- **Cron logs** end with a one-line JSON summary of the run (state, counts and errors). Exit
  code 1 means the run FAILED. PARTIAL runs exit 0, but their errors are listed in the summary
  and on the dashboard's Runs page.
- **Rotating a key**: update the variable on both services. Railway redeploys them.
- **Backups**: enable Railway's Postgres backups. The database holds all pipeline state.
- **Rollback**: redeploy a previous deployment from Railway. If the rollback crosses a migration,
  downgrade first with `python -m alembic downgrade -1` from a web-service shell.

## 7. Running locally on Windows instead (interim)

No code changes; the Railway config files are simply ignored.

1. `.env` needs the API keys plus `SESSION_SECRET`, `SUPPRESSION_SECRET`, `ADMIN_PASSWORD_HASH`
   (`uv run python -m app.jobs.hash_password`) and an **absolute** SQLite path, e.g.
   `DATABASE_URL=sqlite:///C:/Users/<you>/Downloads/Origination Tool/data/origination.db`,
   so the scheduled job and the dashboard share one database whatever directory they start in.
2. Double-click `scripts\start-dashboard.cmd` and open http://127.0.0.1:8000 (this computer only).
3. Daily run: Windows Task Scheduler runs `scripts\daily-run.cmd` (output in `logs\daily.log`).
   Register it once with
   `powershell -ExecutionPolicy Bypass -File scripts\register-daily-task.ps1` (08:00 daily; add
   `-At 07:30` to change the time). If the PC is off at that time it runs once you are next logged
   on; use **Run now** in the dashboard otherwise.
4. Moving to Railway later: copy the SQLite pipeline state into Postgres with a one-off script
   (not yet written) so founders already contacted are not drafted again.

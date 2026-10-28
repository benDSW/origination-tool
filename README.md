# DSW Origination Tool

An internal tool that automates DSW Ventures' founder-origination loop:

> **daily job → new Evertrace signals → eligibility filter → Affinity dedupe → find an email
> (Evertrace, else RocketReach) → render the "Funding Enquiry" draft → dashboard → you copy the
> draft into Outlook and send it → sent / replied / deck received are tracked**

The tool **never sends email** and **never writes to Affinity or Evertrace** (except creating a
new saved search when you ask it to). All pipeline state lives in the tool's own database.

---

## Architecture

Ports and adapters ("hexagonal"). Dependencies point inwards only, and
[import-linter](.importlinter) enforces this in the test suite, so a layer crossing fails the build.

```
                 +--------------------+
  browser  --->  | app/web (FastAPI)  |---+
                 +--------------------+   |      +---------------------+      +---------------------------+
                                          +----> | app/services        | ---> | app/domain                |
  Railway  --->  | app/jobs (CLI)     |---+      | orchestration only  |      | models, rules, PORTS      |
   cron          +--------------------+          +---------------------+      +---------------------------+
                                                                                     ^   (implement ports)
  app/container.py (composition root) builds ---> app/integrations/{evertrace,      |
                                                   rocketreach, affinity, inbox} ---+
                                                  app/persistence (SQLAlchemy) -----+
```

| Layer | Package | May import |
|---|---|---|
| Domain | `app/domain` | stdlib, pydantic, email-validator |
| Integrations | `app/integrations/<name>` | domain (+ httpx). Never each other |
| Persistence | `app/persistence` | domain (+ SQLAlchemy) |
| Services | `app/services` | domain only (ports, models, rules) |
| Web | `app/web` | services + domain |
| Jobs | `app/jobs` | services + container |
| Wiring | `app/container.py`, `app/config.py` | everything |

Rules: adapters return domain DTOs, never raw JSON. Services receive ports through their
constructors and never import an adapter. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
the data flow of each pipeline step and how to swap or add providers.

---

## Local setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                              # creates .venv with the locked dependencies
cp .env.example .env                 # then fill in the keys (never commit .env)
uv run python -m alembic upgrade head   # create / migrate the local SQLite database
uv run python -m app.jobs.hash_password # paste the output into ADMIN_PASSWORD_HASH
uv run python -m uvicorn app.main:app --reload
```

`SESSION_SECRET` can be generated with
`uv run python -c "import secrets; print(secrets.token_urlsafe(48))"`.

> **Windows note.** Group policy on some machines blocks the console-script `.exe` files in
> `.venv\Scripts` ("Access is denied"). Always run tools as modules: `python -m pytest`,
> `python -m ruff`, `python -m alembic`, `python -m uvicorn`, and so on. Every command in this
> README already does.

### Commands

| What | Command |
|---|---|
| All tests + coverage (fails under 85%) | `uv run python -m pytest` |
| Domain + services coverage (target ≥ 90%) | `uv run python -m pytest tests/services tests/domain tests/jobs --cov=app/services --cov=app/domain --cov=app/jobs --cov-report=term-missing` |
| Lint / format | `uv run python -m ruff check .` / `uv run python -m ruff format .` |
| Layer contracts (import-linter) | `uv run python -m pytest tests/architecture -q --no-cov` |
| Security lint | `uv run python -m bandit -r app` |
| Dependency audit | `uv run python -m pip_audit` |
| Daily run | `uv run python -m app.jobs.daily [--dry-run] [--no-rocketreach] [--search sch_...]` |
| Tracking only | `uv run python -m app.jobs.track` |
| Searches | `uv run python -m app.jobs.searches list\|sync\|enable ID\|disable ID\|policy ID POLICY\|lookback ID DAYS` |
| Admin password hash | `uv run python -m app.jobs.hash_password` |

Tests never touch the network or real data: integrations are tested with `respx` mocks and
services with in-memory fakes (`tests/fakes`) and a temporary SQLite database. All test data is
synthetic.

---

## First run

1. **Sync the saved searches** from Evertrace. New searches arrive **disabled**:
   ```bash
   uv run python -m app.jobs.searches sync
   uv run python -m app.jobs.searches list
   ```
2. **Enable the searches** you want and set their policy (the same is possible on the
   dashboard's Searches page):
   ```bash
   uv run python -m app.jobs.searches enable 3
   uv run python -m app.jobs.searches policy 5 london_allowed    # e.g. "Female founders - London"
   uv run python -m app.jobs.searches lookback 3 30              # days to backfill on the first run
   ```
   `uk_outside_london` (the default) excludes non-UK founders and anyone in London or inside the
   M25. `london_allowed` admits London.
3. **Rehearse** without spending RocketReach credits or moving any watermark:
   ```bash
   uv run python -m app.jobs.daily --dry-run --no-rocketreach
   ```
   The JSON summary shows signals seen, exclusions, drafts and errors. A dry run still records
   founders and drafts in the tool's own database (so you can look at them on the dashboard);
   it never advances watermarks, never calls RocketReach and skips tracking.
4. **Run for real**: `uv run python -m app.jobs.daily`. Exit code 0 means SUCCEEDED, PARTIAL
   (some items failed and are listed in `errors`) or SKIPPED (another run held the lock);
   1 means FAILED.

### What the daily run does

1. Takes the run lock (a concurrent cron and "Run now" cannot overlap) and records a run.
2. Syncs the saved-search list from Evertrace (keeps going with the stored list if that fails).
3. For each enabled search, fetches signals since its watermark (or `lookback_days` on the first
   run). Founders are deduplicated by Evertrace signal id **and** LinkedIn slug.
4. Applies the search's location policy. If any search admits a founder, that wins.
5. Takes **one** Affinity snapshot of the Origination and Applicant Opportunities lists and
   excludes founders whose company name, domain or full name matches. If Affinity is down,
   founders stay *Discovered* and are retried next run: nothing is drafted without this check.
6. Drafts with the Evertrace email when it is valid; otherwise *Needs email*.
7. Works through the *Needs email* backlog with RocketReach (LinkedIn URL only, oldest first,
   at most `ROCKETREACH_DAILY_CAP` lookups per UTC day, never twice for the same founder, stops
   after 3 consecutive errors or when credits run out).
8. Syncs sent / replied tracking from Affinity's email data.

### Reply tracking

Sent and replied are detected automatically from Affinity's email sync: the first email from a
DSW address moves a draft to *Sent*, and the founder's first email after that moves it to
*Replied* (a reply from another of the founder's addresses counts if Affinity holds it on the same
person). The only clicks left on the Pipeline page are **Deck received** and **Declined**.

Tracking runs as the last step of every daily run, and **while the dashboard is open it checks every
`TRACKING_INTERVAL_MINUTES` (default 60)**. Outreach sent more than 21 days ago is re-checked at
most once a day, to keep Affinity API usage down. **Check for replies now** on the Pipeline page
checks everyone immediately. If Affinity has no record of the email (for example it bounced, or
was sent from a mailbox Affinity does not sync), nothing can be detected: use *Mark replied* on the
founder's page.

The watermark only advances past signals that were fully processed, and never on a dry run.

---

## Security notes

- **Secrets** live only in environment variables (Railway variables, or a local `.env` that is
  gitignored). They are held as `SecretStr` and a redacting log filter scrubs them (and any
  `Authorization` / API-key header) from every log line. Errors recorded on runs are short,
  secret-free strings: never response bodies, headers or stack traces.
- **Dashboard login**: one admin password stored only as an Argon2 hash
  (`ADMIN_PASSWORD_HASH`), signed session cookies (`SESSION_SECRET`), CSRF tokens on every POST,
  strict security headers, Jinja autoescaping for all Evertrace data.
- **Email drafts** use whitelisted `{field}` substitution only (no template engine over user
  input); recipients are validated and CR/LF is stripped from subject and recipient.
- **Outbound calls** go only to hard-coded API base URLs, with timeouts and bounded retries.
  LinkedIn slugs are validated before any RocketReach lookup.
- **Cost controls**: RocketReach daily cap + pre-run credit check + lookup cache + run lock.
- **Human in the loop**: the tool never sends email and never writes to Affinity.
- **Privacy**: minimal founder PII, no raw API payloads stored. *Forget founder* on the dashboard
  hard-deletes a founder and everything linked to them (UK GDPR erasure).

Deployment: see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

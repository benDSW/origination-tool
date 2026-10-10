# Architecture

The tool is built as **ports and adapters** (hexagonal architecture). The domain defines the
interfaces it needs (*ports*). Each external system has an *adapter* that implements one or more
ports. Services orchestrate the work using only the ports, and one composition root
(`app/container.py`) decides which adapter fills which port.

The purpose is replaceability. This MVP is the foundation for a larger product, so it must be
possible to swap Affinity-based tracking for Microsoft Graph, or add a second email finder,
without touching orchestration code.

## Layers

```
app/domain/        Pure Python: DTOs (models.py), rules (status, eligibility, matching,
                   templating, geography) and PORTS (ports.py). No I/O at all.
app/integrations/  One package per external system. Each implements domain ports and returns
                   domain DTOs, never raw JSON.
   evertrace/      SearchCatalog + SignalSource (MCP JSON-RPC transport, isolated in transport.py)
   rocketreach/    EmailFinder (lookup by LinkedIn URL, guardrails, throttling)
   affinity/       CrmGateway (read-only: list snapshot + email interactions)
   inbox/          InboxTracker, built on the CrmGateway PORT (not on the Affinity adapter)
app/persistence/   Store (SQLAlchemy 2 on SQLite or Postgres) + Alembic migrations
app/services/      Orchestration: OriginationService, TrackingService, OutreachService,
                   TemplateService, SearchService. Imports app.domain only.
app/web/           Dashboard. Imports services + domain only.
app/jobs/          CLI entrypoints (cron). Use the container.
app/container.py   Composition root: builds adapters from Settings and wires the services.
```

The rules are enforced by [`.importlinter`](../.importlinter), which runs as part of the test
suite (`tests/architecture`):

- the domain imports nothing from the app and no I/O library;
- each integration is independent of every other (inbox reaches Affinity only through the port);
- integrations and persistence import only the domain;
- services never import an adapter, the database, httpx or the web framework;
- the web layer never imports an adapter, the database or the container.

## Ports (`app/domain/ports.py`)

| Port | Methods | Today's adapter |
|---|---|---|
| `SearchCatalog` | `list_searches`, `get_search`, `create_search`, `vocabulary`, `search_cities` | `EvertraceAdapter` |
| `SignalSource` | `iter_signals(filters, since)` | `EvertraceAdapter` |
| `EmailFinder` | `find_email(linkedin_slug, full_name, company_domain)`, `remaining_credits()` | `RocketReachAdapter` |
| `CrmGateway` | `known_entities()`, `email_interactions(email, since)` | `AffinityAdapter` |
| `InboxTracker` | `events_for(email, since)` | `AffinityInboxTracker` |
| `Store` | founders, outreach, events, lookups, runs, locks, template | `SqlStore` |

Error contract: adapters raise `IntegrationError` (with a `retryable` hint) for any failure of an
external system. Its message is secret-free by contract, so services can record `str(exc)` on a
run. Services raise `ServiceError` subclasses (`NotFound`, `InvalidAction`, `ValidationFailed`,
`TemplateValidationError`, `ExternalServiceError`) whose `.message` is safe to show to users.

Every service takes a `clock: Callable[[], datetime]` returning timezone-aware UTC, so tests
control time and nothing ever produces a naive local datetime.

## Data flow of the daily run (`OriginationService.run`)

| Step | What happens | Ports used | Failure behaviour |
|---|---|---|---|
| a. Lock | `try_acquire_lock("origination-run")`. If taken, record a SKIPPED run and return | Store | Lock released in `finally`; stale locks expire after the TTL |
| b. Catalogue | `list_searches()` → `upsert_search_from_catalog` (new searches disabled; local settings never overwritten) | SearchCatalog, Store | Error recorded; continue with stored searches |
| c. Signals | For each enabled search: `get_search` (current filters), then `iter_signals(filters, since = watermark or now − lookback)`. Each signal: find founder by signal id **or** LinkedIn slug, else add founder + DISCOVERED outreach; link to the search; apply the search's location policy | SearchCatalog, SignalSource, Store | One failing search: recorded, next search runs. One failing signal: recorded, loop continues, watermark stops at that signal |
| c'. Policy | DISCOVERED + ineligible → EXCLUDED(location) unless another search already admitted them this run. EXCLUDED(location) + this search admits → re-evaluated in step d | — | — |
| d. Dedupe + draft | One `known_entities()` snapshot → `KnownIndex`. Per DISCOVERED (and re-admitted) founder: match on domain / company name / full name → EXCLUDED(in_affinity, detail). Else Evertrace email → DRAFTED (`build_draft`), else NEEDS_EMAIL | CrmGateway, Store | CRM down: error recorded, founders stay DISCOVERED (never drafted without the check) and are retried next run |
| d'. Watermarks | Saved per search (never on a dry run). Held back for searches whose re-admitted founders could not be deduped | Store | Save error recorded |
| e. RocketReach | NEEDS_EMAIL founders with a slug and no settled lookup, oldest first. Stops at the daily cap (counted per UTC day), on zero credits, after 3 consecutive errors, or on a non-retryable error. Every attempt is cached; FOUND → DRAFTED | EmailFinder, Store | ERROR results are cached (they count toward the cap) and retried on later runs |
| f. Tracking | `TrackingService.sync()` | InboxTracker, Store | Per-founder errors collected |
| g. Finish | Counts + errors saved; state SUCCEEDED, PARTIAL (errors but progress) or FAILED (unexpected exception, recorded by type name only) | Store | — |

Idempotency comes from the data model: exactly one outreach row per founder, founders matched
by signal id or slug, and only DISCOVERED / NEEDS_EMAIL / re-admitted founders can ever be
drafted. Re-running a day, or fetching the same signal from two searches, never creates a second
founder or a second draft.

### Tracking (`TrackingService.sync`)

For every DRAFTED or SENT outreach with an address, `events_for(email, since=drafted_at)` returns
SENT / REPLIED (and, in future, DECK_RECEIVED) events, oldest first. Each event moves the record
forward only if the state machine allows it from the current state (e.g. a SENT event after a
manual "Mark sent" is ignored). Each change is saved with a `StatusEvent` whose source is
`auto:inbox`.

### Dashboard actions (`OutreachService`)

All manual buttons go through `app.domain.status.transition`, so they follow the same state
machine and are audited with source `manual`. Invalid moves raise `InvalidAction` with a
readable message.

## Swapping Affinity tracking for Microsoft Graph

Tracking depends only on the `InboxTracker` port, so the change is:

1. Add `app/integrations/graph/` with a `GraphClient` (hard-coded
   `https://graph.microsoft.com/v1.0` base URL, timeouts, retries) and a `GraphInboxTracker`
   implementing `events_for(email, since) -> list[InboxEvent]`:
   - messages in *Sent Items* to `email` → `SENT`
   - messages in the inbox from `email` → `REPLIED`
   - an inbound message from `email` with a PDF / PPTX attachment → `DECK_RECEIVED`
     (`TrackingService` already honours it from SENT or REPLIED).
2. Add the package to the `integrations-are-independent` contract in `.importlinter`.
3. Add the Graph settings (tenant id, client id, client secret as `SecretStr`, mailbox) to
   `app/config.py` and include the secret in `secret_values()`.
4. In `app/container.py`, build `GraphInboxTracker(...)` instead of `AffinityInboxTracker(crm)`.
5. Test it with respx mocks, plus a fake in `tests/fakes` if services need one.

No service, web or domain code changes. Affinity stays the dedupe source (`CrmGateway`).

## Adding another email finder (RocketReach-like providers)

1. Add `EmailSource.<PROVIDER>` in the domain (a small, deliberate domain change) so cache rows
   and drafts can record where an email came from.
2. Implement `EmailFinder` in `app/integrations/<provider>/`. Keep the RocketReach contract:
   look up by LinkedIn profile only, verify the returned identity matches the requested slug,
   return `NOT_FOUND` / `REJECTED` instead of raising, raise `IntegrationError` only for
   transport or auth failures, and never put response bodies in messages.
3. To chain providers without changing services, write a `FallbackEmailFinder(finders)` that
   implements `EmailFinder` by trying each finder in order and returning the first `FOUND`.
   Wire it in the container. If each provider needs its **own** daily cap and lookup cache,
   generalise `OriginationService._rocketreach_backlog` to loop over `(EmailSource, EmailFinder,
   cap)` triples. The cache (`email_lookups`) is already keyed by `(founder, provider)`.

## Persistence

`SqlStore` implements `Store` with SQLAlchemy 2. Every method is its own transaction and returns
domain DTOs. The same code runs on SQLite (local, tests) and Postgres (Railway). The schema is
managed by Alembic (`alembic/versions`). The web service runs `alembic upgrade head` on every
deploy. `hide_parameters=True` keeps founder PII out of database error messages.

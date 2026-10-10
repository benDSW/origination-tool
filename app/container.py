"""Composition root: the one place that knows every concrete class.

It builds the adapters from :class:`Settings`, plugs them into the services as
ports, and hands back a :class:`Services` bundle. Swapping an implementation
(e.g. Affinity-based inbox tracking -> Microsoft Graph) is a change here only.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.config import MIN_SUPPRESSION_SECRET_LEN, Settings, get_settings
from app.domain.models import EmailInteraction, KnownEntities
from app.domain.ports import (
    CrmGateway,
    EmailFinder,
    InboxTracker,
    IntegrationError,
    SearchCatalog,
    SignalSource,
    Store,
)
from app.integrations.affinity import AffinityAdapter
from app.integrations.evertrace import EvertraceAdapter
from app.integrations.inbox import AffinityInboxTracker
from app.integrations.rocketreach import RocketReachAdapter, RocketReachClient
from app.persistence import SqlStore, make_engine, make_session_factory
from app.services import (
    OriginationService,
    OutreachService,
    SearchService,
    Services,
    SessionService,
    SuppressionKeys,
    TemplateService,
    TrackingService,
)
from app.services.clock import Clock, utc_now

logger = logging.getLogger(__name__)


class UnconfiguredCrm:
    """Stand-in ``CrmGateway`` when no Affinity key is set.

    Every call fails with a non-retryable IntegrationError, so the run records
    the problem and leaves founders DISCOVERED: nothing is ever drafted without
    the Affinity dedupe check.
    """

    def known_entities(self) -> KnownEntities:
        raise IntegrationError("affinity", "API key is not configured", retryable=False)

    def email_interactions(self, email: str, since: datetime) -> list[EmailInteraction]:
        raise IntegrationError("affinity", "API key is not configured", retryable=False)


def suppression_keys(settings: Settings) -> SuppressionKeys:
    """The HMAC key for erasure suppression. Production is guaranteed a real secret
    by ``check_runtime_safety``; local development falls back to a fixed dev key."""
    secret = settings.suppression_secret.get_secret_value()
    if len(secret) >= MIN_SUPPRESSION_SECRET_LEN:
        return SuppressionKeys(secret.encode())
    if settings.is_production:
        raise ValueError("SUPPRESSION_SECRET must be set in production")
    logger.warning("SUPPRESSION_SECRET is not set: using the development suppression key")
    return SuppressionKeys.development()


def build_services(settings: Settings | None = None, *, clock: Clock = utc_now) -> Services:
    """Wire the production object graph from settings (env vars / ``.env``)."""
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    store = SqlStore(make_session_factory(engine))

    evertrace = EvertraceAdapter(settings.evertrace_api_key.get_secret_value())

    finder: RocketReachAdapter | None = None
    if settings.rocketreach_api_key.get_secret_value().strip():
        finder = RocketReachAdapter(RocketReachClient(settings.rocketreach_api_key))

    crm: CrmGateway
    affinity: AffinityAdapter | None = None
    if settings.affinity_api_key.get_secret_value().strip():
        affinity = AffinityAdapter.from_api_key(
            settings.affinity_api_key,
            list_ids=[settings.affinity_origination_list_id, settings.affinity_applicant_list_id],
            dsw_email_domains=settings.dsw_email_domains,
        )
        crm = affinity
    else:
        crm = UnconfiguredCrm()
    inbox = AffinityInboxTracker(crm)

    services = build_services_from(
        store=store,
        catalog=evertrace,
        signals=evertrace,
        crm=crm,
        email_finder=finder,
        inbox=inbox,
        settings=settings,
        clock=clock,
    )
    services.closers.append(engine.dispose)
    services.closers.append(evertrace.close)
    if finder is not None:
        services.closers.append(finder.close)
    if affinity is not None:
        services.closers.append(affinity.close)
    return services


def build_services_from(
    *,
    store: Store,
    catalog: SearchCatalog,
    signals: SignalSource,
    crm: CrmGateway,
    email_finder: EmailFinder | None,
    inbox: InboxTracker,
    settings: Settings,
    clock: Clock = utc_now,
) -> Services:
    """Wire services from ready-made ports (used by tests and by :func:`build_services`)."""
    tracking = TrackingService(store, inbox, clock)
    suppression = suppression_keys(settings)
    origination = OriginationService(
        store,
        catalog,
        signals,
        crm,
        email_finder,
        tracking,
        clock,
        rocketreach_daily_cap=settings.rocketreach_daily_cap,
        lock_ttl_seconds=settings.run_lock_ttl_seconds,
        suppression=suppression,
    )
    return Services(
        origination=origination,
        tracking=tracking,
        outreach=OutreachService(
            store,
            email_finder,
            clock,
            follow_up_after_days=settings.follow_up_after_days,
            suppression=suppression,
        ),
        templates=TemplateService(store, clock),
        searches=SearchService(store, catalog, clock),
        clock=clock,
        sessions=SessionService(store, clock),
    )

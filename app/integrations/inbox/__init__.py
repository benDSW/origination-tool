"""Inbox layer: turns "what happened in the mailbox" into domain InboxEvents.

Services track outreach (sent -> replied -> deck received) through the domain
``InboxTracker`` port and never care where the evidence comes from.

Today the only implementation is :class:`AffinityInboxTracker`, which derives
SENT and REPLIED from Affinity's email sync via the injected ``CrmGateway`` port
(this package never imports the Affinity adapter directly - the independence
contract in ``.importlinter`` enforces that).

Planned drop-in: a ``GraphInboxTracker`` reading the DSW mailbox through
Microsoft Graph. It can see attachments, so it will also emit DECK_RECEIVED,
and the services layer will not need to change.
"""

from app.integrations.inbox.affinity_tracker import AffinityInboxTracker

__all__ = ["AffinityInboxTracker"]

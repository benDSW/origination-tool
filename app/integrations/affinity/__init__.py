"""Affinity CRM layer (read-only).

Implements the domain ``CrmGateway`` port: a snapshot of the watched opportunity
lists for dedupe, and email interactions for sent/replied tracking. Only GET
requests are ever made; the tool never writes to Affinity.
"""

from app.integrations.affinity.adapter import AffinityAdapter
from app.integrations.affinity.client import AffinityClient

__all__ = ["AffinityAdapter", "AffinityClient"]

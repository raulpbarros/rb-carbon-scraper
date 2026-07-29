"""Verra VCS — one registry on the S&P Platts platform.

Everything about *how* the platform is paged, partitioned and reconciled lives
in `registries/platts/api.py`, and every constraint documented there was
measured here first. This module is only Verra's identity: which registry code
and standard to send, and where its public pages are.

`standardid` is the VCS standard's internal id, observed in the browser's own
requests and confirmed against
`{cmsResources}/public/standardsByRegistry/VERRA`.
"""

from __future__ import annotations

from ... import settings
from ..platts.api import (  # re-exported: the Verra suite and docs refer to these
    ALL_RESOURCES,
    CANCELLATIONS,
    HOLDINGS,
    ISSUANCES,
    LEDGERS,
    MAX_LIMIT,
    MAX_WINDOW,
    PARTITION_KEYS,
    PROJECT,
    PROJECT_COLUMNS,
    RETIREMENTS,
    STABLE_SORT,
    PlattsAPI,
    number_equals,
    text_equals,
)

REGISTRY = settings.VERRA


class VerraAPI(PlattsAPI):
    """The Verra VCS registry. See PlattsAPI for the platform contract."""

    registry = REGISTRY
    slug = "verra"
    registry_code = "VERRA"
    standard_id = settings.VERRA_STANDARD_ID
    standard_acronym = settings.PROGRAM
    site = settings.SITE
    config_url = settings.ENVIRONMENT_CONFIG_URL
    public_search_url = settings.PUBLIC_SEARCH_URL
    detail_url_template = settings.PROJECT_DETAIL_URL
    #: A project `discover` can open to capture the detail and Units calls.
    sample_project_id = "2265"


#: Registry identity headers. Without all four every call returns a generic
#: HTTP 500 that looks like a server fault but is not.
REGISTRY_HEADERS = VerraAPI.registry_headers()

# Module-level normalisation, kept because the export tests and any future
# fixture-driven check call these directly.
normalize_project = VerraAPI.normalize_project
normalize_credit = VerraAPI.normalize_credit

__all__ = [
    "ALL_RESOURCES",
    "CANCELLATIONS",
    "HOLDINGS",
    "ISSUANCES",
    "LEDGERS",
    "MAX_LIMIT",
    "MAX_WINDOW",
    "PARTITION_KEYS",
    "PROJECT",
    "PROJECT_COLUMNS",
    "REGISTRY",
    "REGISTRY_HEADERS",
    "RETIREMENTS",
    "STABLE_SORT",
    "VerraAPI",
    "normalize_credit",
    "normalize_project",
    "number_equals",
    "text_equals",
]

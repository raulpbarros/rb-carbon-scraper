"""Plan Vivo Climate Registry — the second registry on the S&P Platts platform.

Identical backend, identical request shape, identical envelope. Three header
values differ. The whole of the paging, partitioning and reconciliation code
is inherited from `registries/platts/api.py`; what follows is Plan Vivo's
identity and the two places its published data differs from Verra's.

Measured 2026-07-28 against the live service; see docs/api-contract-planvivo.md.

Two field differences, both real and both worth stating rather than silently
mapping:

* **`sectoralScope` is null throughout; `projectType` carries the vocabulary.**
  Plan Vivo says "Afforestation / Reforestation" where Verra says "Agriculture
  Forestry and Other Land Use (AFOLU)". Per CLAUDE.md that wording is carried
  through untranslated into `Tipo Macro de Projeto`, so the derivation rules
  have to know it — `config/derivation/biome.yaml`'s `applies_when` gate in
  particular, which decides whether a row gets a biome at all.
* **`vcsProjectId` is null**, and nothing else in the record is a human-facing
  reference. `Project ID` therefore falls back to the numeric `projectId`,
  which is what the registry's own public URL uses. Nothing is invented.

`regionName` is null too, so `Continent` is derived from the country name —
the same path Cercarbono takes, using the `name-*` rules in
`config/derivation/continent.yaml`.
"""

from __future__ import annotations

from ... import settings
from ..platts.api import PROJECT_COLUMNS, PlattsAPI

REGISTRY = settings.PLAN_VIVO

#: The platform's field map, with the two Plan-Vivo differences applied.
#: Built by removing rather than shadowing: two platform fields mapping onto
#: one column would let a null overwrite a real value depending on dict order.
PV_PROJECT_COLUMNS = {
    field: column
    for field, column in PROJECT_COLUMNS.items()
    if field not in ("sectoralScope", "vcsProjectId")
}
PV_PROJECT_COLUMNS["projectType"] = "sectoral_scope"


class PlanVivoAPI(PlattsAPI):
    """Plan Vivo Climate Registry. See PlattsAPI for the platform contract."""

    registry = REGISTRY
    slug = "planvivo"
    registry_code = settings.PV_REGISTRY_CODE
    standard_id = settings.PV_STANDARD_ID
    standard_acronym = settings.PV_STANDARD_ACRONYM
    site = settings.PV_SITE
    config_url = settings.PV_ENVIRONMENT_CONFIG_URL
    public_search_url = settings.PV_PUBLIC_SEARCH_URL
    detail_url_template = settings.PV_PROJECT_DETAIL_URL
    sample_project_id = "110200000000034"

    project_columns = PV_PROJECT_COLUMNS

    # `unitClass` is where the reserve lives: an issuance is `rPVC` or
    # `Achievement Reserve`, and the registry's own "Issued Units" figure
    # counts both. Recording the class rather than the bare unit type keeps
    # the reserve identifiable without a re-scrape, exactly as Cercarbono's
    # buffer flag does. Holdings spell the same idea `unitClassName`.
    unit_type_fields = ("unitClass", "unitClassName", "unitType")

    #: Same keys as Verra's. Plan Vivo is far below the 10k window on every
    #: resource, so no split is ever attempted; if it grows, `regionName` is
    #: unpublished here and sampling will simply fall through to `countryName`.
    #: Do not add a key that has not been proven to narrow — see PlattsAPI.


#: Registry identity headers. Without all four every call returns a generic
#: HTTP 500; with a wrong `standardid` it returns HTTP 200 and no rows.
REGISTRY_HEADERS = PlanVivoAPI.registry_headers()

normalize_project = PlanVivoAPI.normalize_project
normalize_credit = PlanVivoAPI.normalize_credit

__all__ = [
    "PV_PROJECT_COLUMNS",
    "REGISTRY",
    "REGISTRY_HEADERS",
    "PlanVivoAPI",
    "normalize_credit",
    "normalize_project",
]

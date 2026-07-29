"""Verra JNR — the second Verra standard this codebase ingests.

The `VERRA` tenant on S&P publishes **six** standards, not one. `verra
standards -r verra` lists them, and until 2026-07-29 the scraper read only VCS:

| standardId | acronym | | |
|---|---|---|---|
| `150000000000001` | VCS | Verified Carbon Standard | ingested |
| `150000000000002` | JNR | Jurisdictional and Nested REDD+ Framework | **ingested, here** |
| `150000000000003` | CCBS | Climate, Community and Biodiversity | out |
| `150000000000004` | SDVISTA | Sustainable Development Verified Impact | out |
| `150000000000005` | PWRS | Plastic Waste Reduction Standard | out |
| `150000000000006` | S3S | Scope 3 Standard | out |

JNR is in because it is tCO2e and publishes the full ledger set — projects,
issuances, holdings, retirements and cancellations — so its credits are
comparable with the rest of the sheet. The others are out, and each for its
own reason rather than by omission (user's decision, 2026-07-29):

* **CCBS is a co-certification of VCS projects, not a separate project set.**
  Ingesting it would count the same project twice — the same double-counting
  risk BioCarbon poses through Cercarbono's converted-in projects. Verra
  already states it per project in `additional_certification`, which is where
  a co-certification belongs.
* **PWRS is plastic and SDVISTA is SDG impact.** Neither is tCO2e, so their
  credits do not belong in the same columns — the same call already made for
  Cercarbono's biodiversity and circular-economy standards.
* **S3S publishes projects only**, no ledgers.

Rows land under `settings.VERRA`, one registry and one checkbox, exactly as
Plan Vivo's two systems do. `standardName` is published per project by the
platform, so a JNR row says "Jurisdictional and Nested REDD+ Framework" in
`standard_name` and a VCS row says "Verified Carbon Standard" without anything
here having to assert it.
"""

from __future__ import annotations

from ... import settings
from .api import VerraAPI

REGISTRY = settings.VERRA


class VerraJNRAPI(VerraAPI):
    """Verra's Jurisdictional and Nested REDD+ Framework.

    Same registry, same platform, same everything except the standard. Three
    class attributes, which is what "adding a registry hosted on S&P Platts"
    has cost since Plan Vivo.
    """

    slug = "verra-jnr"
    standard_id = settings.VERRA_JNR_STANDARD_ID
    standard_acronym = settings.VERRA_JNR_ACRONYM
    system_label = "Verra JNR"
    #: JNR is small enough that no partition is ever attempted, but the
    #: platform's guards are inherited whole in case it grows.
    sample_project_id = ""


REGISTRY_HEADERS = VerraJNRAPI.registry_headers()

__all__ = ["REGISTRY", "REGISTRY_HEADERS", "VerraJNRAPI"]

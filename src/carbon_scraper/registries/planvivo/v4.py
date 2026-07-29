"""Plan Vivo Standard V4 — the half of Plan Vivo that is not on S&P.

`registries/planvivo/api.py` reaches the **PV Climate** registry, the Plan Vivo
Standard V5 system launched on S&P in 2025, and its 2 projects are all of it.
Plan Vivo has been certifying since 2008, and everything certified under V4 and
earlier lives on the **legacy Markit Environmental Registry** — the system S&P
inherited with IHS Markit and still runs at `mer.markit.com/br-reg/public`.

That is 30 more projects, against the 2 the sheet had. The V5 sync was never
wrong and always reconciled 2/2 exactly, because 2 is what that tenant
publishes: **the scrape was not wrong, its scope was.** Same shape as every
ignored-filter trap in this codebase, wearing a different hat.

Found the cheap way, in the end. `verra standards -r all` established that no
S&P tenant holds it — `PVCL` publishes exactly one standard — and Plan Vivo's
own registry page names Markit. The `standardId` here, `100000000000004`, is
the one PLAN.md had already recorded as "the planning guess, from a legacy
Markit URL, that returns `totalEntities: 0`". It was never a wrong id. It was
the right id for the wrong platform.

**Both systems store under one `registry`** (`settings.PLAN_VIVO`), which is
the user's decision of 2026-07-29: one Plan Vivo, one checkbox, one row set.
What keeps that honest is `standard_name`: a V4 row reads "Plan Vivo Standard
V4" and a V5 row reads "PV Climate", so the two eras stay separable in the
database and the merge can be undone in a query rather than a re-scrape. The
project id spaces do not overlap — V5's are S&P entity ids
(`110200000000034`), V4's are Markit's — so nothing collides in the primary
key.
"""

from __future__ import annotations

from ... import settings
from ..markit.api import MarkitPublicAPI

REGISTRY = settings.PLAN_VIVO


class PlanVivoV4API(MarkitPublicAPI):
    """Plan Vivo V4. See MarkitPublicAPI for the platform contract."""

    registry = REGISTRY
    slug = "planvivo-v4"
    standard_id = settings.PV4_STANDARD_ID
    #: The Standard column's own wording. Rows claiming anything else are
    #: dropped: `standardId` is a request, not a promise, and this platform
    #: answers a Social Carbon request with rows reading "No Established
    #: Standard".
    standard_names = ("Plan Vivo",)
    #: What gets stored, and the only thing distinguishing a V4 row from a V5
    #: one once both sit under PLAN_VIVO.
    standard_label = settings.PV4_STANDARD_LABEL
    system_label = "Plan Vivo V4 (Markit)"


__all__ = ["REGISTRY", "PlanVivoV4API"]

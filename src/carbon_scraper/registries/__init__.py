"""Registry adapters, looked up by name.

Adapters are imported lazily: Verra's pulls in Playwright-adjacent modules and
Gold Standard's does not, and neither should be a hard import cost for a
command that only touches the other one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from .. import settings
from .base import RegistryAdapter


def _verra() -> type:
    from .verra.api import VerraAPI

    return VerraAPI


def _verra_jnr() -> type:
    from .verra.jnr import VerraJNRAPI

    return VerraJNRAPI


def _goldstandard() -> type:
    from .goldstandard.api import GoldStandardAPI

    return GoldStandardAPI


def _cercarbono() -> type:
    from .cercarbono.api import CercarbonoAPI

    return CercarbonoAPI


def _planvivo() -> type:
    from .planvivo.api import PlanVivoAPI

    return PlanVivoAPI


def _planvivo_v4() -> type:
    from .planvivo.v4 import PlanVivoV4API

    return PlanVivoV4API


def _socialcarbon() -> type:
    from .socialcarbon.api import SocialCarbonAPI

    return SocialCarbonAPI


def _biocarbon() -> type:
    from .biocarbon.api import BioCarbonAPI

    return BioCarbonAPI


def _puro() -> type:
    from .puro.api import PuroAPI

    return PuroAPI


def _acr() -> type:
    from .acr.api import ACRAPI

    return ACRAPI


def _car() -> type:
    from .car.api import CARAPI

    return CARAPI


#: registry identifier -> the adapters that publish it, in scrape order.
#:
#: A table rather than a chain of `if`s on purpose: the old fall-through
#: returned the Gold Standard adapter for any registry it did not recognise,
#: so a registry added to ALIASES but forgotten here scraped Gold Standard and
#: stored the rows under the new registry's name — wrong data, and no error
#: anywhere to notice it by.
#:
#: **A registry can have more than one adapter**, because a registry is not
#: always one system or one standard. Plan Vivo publishes its V5 projects on
#: S&P Platts and its V4 ones on the legacy Markit registry; Verra publishes
#: VCS and JNR as two standards on one tenant. In both cases the rows belong
#: under one `registry` column and one checkbox. The first entry is the
#: primary — it is the one `discover` drives and the one whose
#: `detail_url_template` a bare row falls back to.
ADAPTERS: dict[str, tuple[Callable[[], type], ...]] = {
    settings.VERRA: (_verra, _verra_jnr),
    settings.GOLD_STANDARD: (_goldstandard,),
    settings.CERCARBONO: (_cercarbono,),
    settings.PLAN_VIVO: (_planvivo, _planvivo_v4),
    settings.SOCIAL_CARBON: (_socialcarbon,),
    settings.BIOCARBON: (_biocarbon,),
    settings.PURO: (_puro,),
    settings.ACR: (_acr,),
    settings.CAR: (_car,),
}

# --registry accepts either the stored value or a short alias.
#
# Keys are the shape `resolve()` looks them up in: lowercased with `_` already
# turned into `-`. So the stored identifiers reach here as `verra`,
# `gold-standard` and `plan-vivo`, which are the entries below — spelling
# `settings.GOLD_STANDARD.lower()` as a key of its own added `gold_standard`,
# which `resolve()` can never ask for.
ALIASES = {
    "verra": settings.VERRA,
    "vcs": settings.VERRA,
    "gs": settings.GOLD_STANDARD,
    "gold": settings.GOLD_STANDARD,
    "goldstandard": settings.GOLD_STANDARD,
    "gold-standard": settings.GOLD_STANDARD,
    "cerc": settings.CERCARBONO,
    "cercarbono": settings.CERCARBONO,
    "ecoregistry": settings.CERCARBONO,
    "pv": settings.PLAN_VIVO,
    "planvivo": settings.PLAN_VIVO,
    "plan-vivo": settings.PLAN_VIVO,
    "pvcl": settings.PLAN_VIVO,
    "sc": settings.SOCIAL_CARBON,
    "socialcarbon": settings.SOCIAL_CARBON,
    "social-carbon": settings.SOCIAL_CARBON,
    "bc": settings.BIOCARBON,
    "bcr": settings.BIOCARBON,
    "biocarbon": settings.BIOCARBON,
    "bio-carbon": settings.BIOCARBON,
    "gct": settings.BIOCARBON,
    "globalcarbontrace": settings.BIOCARBON,
    "puro": settings.PURO,
    "puro-earth": settings.PURO,
    "puroearth": settings.PURO,
    "corc": settings.PURO,
    "acr": settings.ACR,
    "american-carbon-registry": settings.ACR,
    "americancarbonregistry": settings.ACR,
    # The platform, which today has exactly one tenant here. ART is the other
    # one it serves and has no adapter, so this alias is deliberately not
    # `greentrace means every tenant`.
    "greentrace": settings.ACR,
    "car": settings.CAR,
    "reserve": settings.CAR,
    "climate-action-reserve": settings.CAR,
    "climateactionreserve": settings.CAR,
    # The platform, which today has exactly one tenant here. Climate Forward is
    # the other offset tenant it serves and has no adapter — its units are
    # ex-ante forecasts rather than issued offsets — so this alias is
    # deliberately not `apx means every tenant`. ACR was on this platform once
    # and left; `acr` above points at GreenTrace, which is where it is now.
    "apx": settings.CAR,
}

ALL = tuple(ADAPTERS)


def resolve(name: str) -> str:
    """Map user input onto a stored registry identifier."""
    key = name.strip().lower().replace("_", "-")
    resolved = ALIASES.get(key) or ALIASES.get(key.replace("-", ""))
    if resolved is None:
        raise ValueError(
            f"Unknown registry {name!r}. Choose one of: {', '.join(ALL)}"
        )
    return resolved


def adapter_classes(registry: str) -> tuple[type, ...]:
    """Every adapter class publishing `registry`, imported on demand.

    Separate from `adapters()` because `discover` and the packaging bundle need
    the classes' declared URLs and module names without constructing a client
    and opening a connection.
    """
    registry = resolve(registry)
    try:
        loaders = ADAPTERS[registry]
    except KeyError:  # pragma: no cover - resolve() rejects unknown names first
        raise ValueError(
            f"{registry} has no adapter registered in registries.ADAPTERS."
        ) from None
    return tuple(loader() for loader in loaders)


def adapter_class(registry: str) -> type:
    """The **primary** adapter class for `registry`.

    Where a registry is published across more than one system this is the first
    one listed — the tenant `discover` drives and the one a project row falls
    back to for its public URL. Anything that must cover the whole registry
    (scraping, packaging) uses `adapter_classes` instead; a caller that wants
    one system's contract wants this.
    """
    return adapter_classes(registry)[0]


def adapters(registry: str, **kwargs: Any) -> Iterator[RegistryAdapter]:
    """Construct every adapter publishing `registry`, one at a time.

    A generator, not a tuple. Each constructor opens an `httpx.Client` — a
    connection pool and an SSL context — so building all of them up front left
    Plan Vivo's second client open for the whole of the first one's run, and
    leaked it entirely if the first adapter raised or was cancelled. The GUI
    does not exit between runs, so those accumulated.
    """
    for cls in adapter_classes(registry):
        yield cls(**kwargs)


__all__ = [
    "ALL",
    "RegistryAdapter",
    "adapter_class",
    "adapter_classes",
    "adapters",
    "resolve",
]

"""Registry adapters, looked up by name.

Adapters are imported lazily: Verra's pulls in Playwright-adjacent modules and
Gold Standard's does not, and neither should be a hard import cost for a
command that only touches the other one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import settings
from .base import RegistryAdapter


def _verra() -> type:
    from .verra.api import VerraAPI

    return VerraAPI


def _goldstandard() -> type:
    from .goldstandard.api import GoldStandardAPI

    return GoldStandardAPI


def _cercarbono() -> type:
    from .cercarbono.api import CercarbonoAPI

    return CercarbonoAPI


def _planvivo() -> type:
    from .planvivo.api import PlanVivoAPI

    return PlanVivoAPI


#: registry identifier -> a callable that imports and returns its adapter class.
#: A table rather than a chain of `if`s on purpose: the old fall-through
#: returned the Gold Standard adapter for any registry it did not recognise,
#: so a registry added to ALIASES but forgotten here scraped Gold Standard and
#: stored the rows under the new registry's name — wrong data, and no error
#: anywhere to notice it by.
ADAPTERS: dict[str, Callable[[], type]] = {
    settings.VERRA: _verra,
    settings.GOLD_STANDARD: _goldstandard,
    settings.CERCARBONO: _cercarbono,
    settings.PLAN_VIVO: _planvivo,
}

# --registry accepts either the stored value or a short alias.
ALIASES = {
    "verra": settings.VERRA,
    "vcs": settings.VERRA,
    settings.VERRA.lower(): settings.VERRA,
    "gs": settings.GOLD_STANDARD,
    "gold": settings.GOLD_STANDARD,
    "goldstandard": settings.GOLD_STANDARD,
    "gold-standard": settings.GOLD_STANDARD,
    settings.GOLD_STANDARD.lower(): settings.GOLD_STANDARD,
    "cerc": settings.CERCARBONO,
    "cercarbono": settings.CERCARBONO,
    "ecoregistry": settings.CERCARBONO,
    "pv": settings.PLAN_VIVO,
    "planvivo": settings.PLAN_VIVO,
    "plan-vivo": settings.PLAN_VIVO,
    "pvcl": settings.PLAN_VIVO,
    settings.PLAN_VIVO.lower(): settings.PLAN_VIVO,
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


def adapter_class(registry: str) -> type:
    """The adapter class for `registry`, imported on demand.

    Separate from `adapter()` because `discover` needs the class's declared
    URLs without constructing a client and opening a connection.
    """
    registry = resolve(registry)
    try:
        loader = ADAPTERS[registry]
    except KeyError:  # pragma: no cover - resolve() rejects unknown names first
        raise ValueError(
            f"{registry} has no adapter registered in registries.ADAPTERS."
        ) from None
    return loader()


def adapter(registry: str, **kwargs: Any) -> RegistryAdapter:
    return adapter_class(registry)(**kwargs)


__all__ = ["ALL", "RegistryAdapter", "adapter", "adapter_class", "resolve"]

"""Registry for AI providers — the extension seam of the AI layer.

Adding an AI provider must never require editing existing code. To add one:

    1. Drop a new module into ``app/ai/providers/``.
    2. Decorate its class with ``@register_ai_provider("myvendor")``.
    3. Set ``AI_PROVIDER=myvendor`` in ``backend/.env``.

That is the whole procedure. Discovery is automatic — this module scans the
package at resolve time, so neither this file nor :mod:`app.ai.manager` nor
:mod:`app.core.config` is ever touched again. Old code is never rewritten;
new capability is only ever *added*.

This mirrors the proven ``app/search_providers/registry.py`` pattern rather
than inventing a second one (CLAUDE.md section 14: reuse before build), and it
keeps every AI transport replaceable (CLAUDE.md sections 4 and 8).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable
from typing import TypeVar

from app.ai.base import BaseAIProvider

logger = logging.getLogger(__name__)

# name -> provider class. Populated by the decorator during discovery.
_PROVIDERS: dict[str, type[BaseAIProvider]] = {}

_discovered = False

# Modules skipped during discovery: this registry itself, plus private modules.
_SKIP_MODULES = frozenset({"registry"})

P = TypeVar("P", bound=type[BaseAIProvider])


class AIProviderNotFoundError(RuntimeError):
    """Raised when ``AI_PROVIDER`` names a provider that is not registered.

    This is deliberately fatal. A misconfigured AI provider used to fall back
    to a stub that echoed the prompt back, which made a dead AI look like
    "0 qualified leads" instead of an error — the fake-live-mode failure
    CLAUDE.md section 12 forbids. Misconfiguration must be loud.
    """


def register_ai_provider(name: str) -> Callable[[P], P]:
    """Register an AI provider class under a configuration name.

    Args:
        name: Value that ``AI_PROVIDER`` must hold to select this provider.
            Matched case-insensitively.

    Returns:
        A class decorator that records the class and returns it unchanged.
    """

    def decorator(cls: P) -> P:
        key = name.strip().lower()
        existing = _PROVIDERS.get(key)
        if existing is not None and existing is not cls:
            logger.warning(
                "AI provider %r already registered as %s; overwriting with %s",
                key,
                existing.__name__,
                cls.__name__,
            )
        _PROVIDERS[key] = cls
        logger.debug("Registered AI provider: %s -> %s", key, cls.__name__)
        return cls

    return decorator


def discover() -> None:
    """Import every provider module in this package so decorators run.

    Idempotent. A provider module that fails to import is logged and skipped
    rather than taking down the whole AI layer — but if that module was the
    *selected* provider, :func:`resolve` still raises, so a broken selection
    can never pass silently.
    """
    global _discovered
    if _discovered:
        return
    # Set the flag first: a module that imports this registry during its own
    # import must not re-enter discovery.
    _discovered = True
    package = importlib.import_module(__package__)
    for module_info in pkgutil.iter_modules(package.__path__):
        name = module_info.name
        if name.startswith("_") or name in _SKIP_MODULES:
            continue
        try:
            importlib.import_module(f"{__package__}.{name}")
        except Exception as exc:  # noqa: BLE001 - one bad module must not break the rest
            logger.warning(
                "AI provider module %r failed to import and was skipped: %s", name, exc
            )


def get_provider_names() -> list[str]:
    """List the registered provider names.

    Returns:
        Sorted provider names currently available for ``AI_PROVIDER``.
    """
    discover()
    return sorted(_PROVIDERS)


def get_provider_class(name: str) -> type[BaseAIProvider] | None:
    """Look up a provider class without instantiating it.

    Args:
        name: Provider name, matched case-insensitively.

    Returns:
        The provider class, or ``None`` when nothing is registered under *name*.
    """
    discover()
    return _PROVIDERS.get((name or "").strip().lower())


def resolve(name: str) -> BaseAIProvider:
    """Instantiate the provider registered under *name*.

    Args:
        name: Provider name, normally ``settings.AI_PROVIDER``.

    Returns:
        A ready-to-use provider instance.

    Raises:
        AIProviderNotFoundError: When *name* is not registered. The message
            lists what *is* registered so the fix is obvious from the log.
    """
    cls = get_provider_class(name)
    if cls is None:
        available = get_provider_names()
        raise AIProviderNotFoundError(
            f"AI_PROVIDER={name!r} is not a registered AI provider. "
            f"Registered providers: {available or '(none)'}. "
            "Fix: set AI_PROVIDER in backend/.env to one of those names, or add a "
            "module in app/ai/providers/ whose class is decorated with "
            "@register_ai_provider(...)."
        )
    return cls()

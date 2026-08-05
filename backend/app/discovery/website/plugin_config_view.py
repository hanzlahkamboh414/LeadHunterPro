"""Plugin Configuration View.

A typed, read-only projection over ``PluginConfig.options`` so generator
implementations can declare and access their configuration in a type-safe
way. The view is a dumb accessor: it performs no validation, no coercion,
and no mutation. It exists solely to replace ``config.options["key"]``
with ``view.get("key", default)`` and ``view.require("key")``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PluginConfigView(Protocol):
    """Typed read-only view over a plugin's configuration options."""

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve a configuration value by key, with an optional default."""
        ...

    def require(self, key: str) -> Any:
        """Retrieve a required configuration value; raise if absent."""
        ...

    def to_dict(self) -> dict[str, Any]:
        """Return the full options dict for logging/metadata."""
        ...


class _DictConfigView:
    """Private reference implementation wrapping a raw dict."""

    __slots__ = ("_options",)

    def __init__(self, options: dict[str, Any]) -> None:
        """Take an immutable snapshot of *options*.

        The dict is **copied**, not aliased. Configuration is set once when
        the generator is constructed and never changes during discovery, so
        a later mutation of the caller's dict must not be observable here —
        a value that changes with no mutation method ever called is exactly
        the hidden behaviour CLAUDE.md §12 forbids. ``to_dict`` guards the
        way out; this guards the way in.
        """
        self._options = dict(options)

    def get(self, key: str, default: Any = None) -> Any:
        return self._options.get(key, default)

    def require(self, key: str) -> Any:
        if key not in self._options:
            raise KeyError(f"Configuration key '{key}' is required but not provided")
        return self._options[key]

    def to_dict(self) -> dict[str, Any]:
        return dict(self._options)

    # Explicitly no __setitem__, __delitem__, update, pop, clear


def make_config_view(options: dict[str, Any]) -> PluginConfigView:
    """Construct a config view from a raw options dict.

    Args:
        options: Raw plugin-specific options dict. May be empty.

    Returns:
        A read-only view conforming to :class:`PluginConfigView`.
    """
    return _DictConfigView(options)
"""AI provider implementations.

Every module in this package is auto-imported by
:mod:`app.ai.providers.registry` at resolve time, so a new provider needs no
entry here and no edit anywhere else — drop the module in, decorate its class
with ``@register_ai_provider("name")``, and set ``AI_PROVIDER=name`` in
``backend/.env``.

This file is intentionally import-free: the registry imports the package to
enumerate it, so eager imports here would defeat the lazy discovery.
"""

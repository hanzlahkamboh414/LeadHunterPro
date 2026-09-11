"""Runtime key overlay — the admin screen's API-key management layer.

Why this exists
---------------
The admin role needs to manage API keys (Tavily, the AI/router provider, Brave,
the Leads API key) WITHOUT editing ``.env`` by hand and WITHOUT this software
ever echoing a secret (CLAUDE.md §6 + the env → provider sourcing rules). This
module is a tiny, inspectable, gitignored overlay file:

* it is written by the admin endpoints (``set`` / ``clear``);
* ``config.py`` merges it over ``.env`` at startup (``apply``), so the rest of
  the app keeps reading ``settings.<KEY>`` unchanged — no caller changes;
* the admin UI only ever sees *configured + masked tail*, never the full value;
* the file lives under ``backend/output/`` so gitignoring it is inherited from
  the real-lead-data rule — keys never reach the repository.

Key changes apply LIVE, without a backend restart (the admin endpoint calls
:meth:`RuntimeKeyStore.apply_live` + re-registers the affected search
provider): the overlay is written to disk AND merged into the running
``settings`` object in place, so every module holding a ``settings`` reference
sees the new value. This is a real switch, not a fake one — search provider
instances are REBUILT with the new key (a new object replaces the old one in
the registry), and AI clients are constructed per-use from ``settings`` so the
next call picks the new key up. Only searches already in flight on the old
instance complete with the old key — that is logged, never hidden (CLAUDE.md
§6).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

#: The only editable secret keys. Anything else (URLs, model names, flags) is
#: ordinary config and stays in .env; the admin screen reads those read-only.
KNOWN_KEY_NAMES: tuple[str, ...] = (
    "AI_API_KEY",
    "AI_API_KEY_2",
    "AI_API_KEY_3",
    "BRAVE_SEARCH_API_KEY",
    "TAVILY_SEARCH_API_KEY",
    "LEADS_API_KEY",
)


def default_overlay_path() -> str:
    """``backend/output/runtime_keys.json`` — under output/, so gitignored."""
    return os.path.join(os.path.dirname(__file__), "..", "..", "output", "runtime_keys.json")


class RuntimeKeyStore:
    """A small JSON overlay of secret API keys.

    Thread-safe for the local tool shape (a lock + atomic file replace), same
    convention as the other stores in this repo. All file errors degrade to
    "no overlay" — a broken overlay must never brick the backend, only leave
    the keys at their .env values.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = str(path or default_overlay_path())
        self._lock = threading.Lock()

    # -- file I/O -----------------------------------------------------------

    def load(self) -> dict[str, str]:
        try:
            with self._lock:
                if not os.path.exists(self.path):
                    return {}
                raw = open(self.path, encoding="utf-8").read()
            if not raw.strip():
                return {}
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                logger.warning("RUNTIME KEYS %s: not a JSON object; ignoring overlay", self.path)
                return {}
            return {str(k): str(v) for k, v in parsed.items() if k in KNOWN_KEY_NAMES and v}
        except (OSError, ValueError) as exc:
            logger.warning("RUNTIME KEYS %s unreadable (%s); ignoring overlay", self.path, exc)
            return {}

    def _write(self, overlay: dict[str, str]) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(overlay, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)  # atomic: readers never see a half-written file

    # -- ops -----------------------------------------------------------------

    def set(self, name: str, value: str, *, applier=None) -> None:
        """Persist a key override; ``value=""`` clears it. Unknown names ignored."""
        if name not in KNOWN_KEY_NAMES:
            return
        overlay = self.load()
        value = (value or "").strip()
        if value:
            overlay[name] = value
        else:
            overlay.pop(name, None)
        with self._lock:
            self._write(overlay)
        logger.info("RUNTIME KEYS %s set=%s", self.path, name)  # name only, never value
        if applier is not None:
            applier()

    def is_set(self, name: str, env_value: str = "") -> bool:
        """True when the key is configured — from the overlay OR the env."""
        if self.load().get(name):
            return True
        return bool((env_value or "").strip())

    def masked(self, name: str, env_value: str = "") -> str:
        """``••••<last4>`` when configured, else ``""`` — never the full value."""
        value = self.load().get(name) or env_value or ""
        if not value:
            return ""
        tail = value[-4:] if len(value) > 4 else value
        return f"••••{tail}"

    def apply(self, settings: Any) -> Any:
        """Return a copy of ``settings`` with runtime key overrides merged.

        Pydantic's ``model_copy(update=...)`` copies the settings object with
        fields replaced; the copy keeps the ``repr=False`` field metadata (so
        the keys can never leak through a repr), and ``config`` reassigns its
        module-level ``settings`` to it BEFORE anything imports it — so every
        provider/registration read sees the overlay transparently.
        """
        overlay = self.load()
        if not overlay:
            return settings
        try:
            return settings.model_copy(update=overlay)
        except (TypeError, AttributeError, ValueError) as exc:
            logger.warning("RUNTIME KEYS: overlay merge failed (%s); using env values", exc)
            return settings

    def apply_live(self, settings: Any, env_values: dict[str, str]) -> list[str]:
        """Merge the overlay into the RUNNING ``settings`` object, in place.

        The boot-time ``apply`` works on a copy because nothing has imported
        ``settings`` yet — the hot-reload path cannot do that: modules hold
        references to the live object, so a fresh copy would leave every
        existing reader on the old values. Mutating in place is the only way
        ``setattr`` reaches all of them.

        A key absent from the overlay falls back to its ``env_values`` entry
        (the .env snapshot taken at boot), so CLEARING an overlay key reverts
        to the .env value — the exact state a restart would produce.

        Args:
            settings: The live settings object (``app.core.config.settings``).
            env_values: Boot-time .env-only values (``config._ENV_KEY_VALUES``).

        Returns:
            The names whose live value actually changed (for the honest log
            line — a no-op change is not reported as an applied one).
        """
        overlay = self.load()
        changed: list[str] = []
        for name in KNOWN_KEY_NAMES:
            value = overlay.get(name) or (env_values.get(name) or "")
            try:
                if getattr(settings, name, None) != value:
                    setattr(settings, name, value)
                    changed.append(name)
            except (TypeError, ValueError) as exc:
                # Pydantic assignment failure — key NAME only, never the value.
                logger.warning("RUNTIME KEYS live-apply failed for %s (%s)", name, exc)
        return changed
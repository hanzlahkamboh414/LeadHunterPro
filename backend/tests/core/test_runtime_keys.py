"""Tests for the RuntimeKeyStore overlay layer.

WHY THIS FILE EXISTS — the admin screen manages API keys through this module.
Every key operation (set, clear, mask, apply) must be correct and safe: values
must never leak in plain text, unknown keys must be silently ignored, and
corrupt/missing files must never crash the backend.

Run: python -m pytest tests/core/test_runtime_keys.py -q
"""

from __future__ import annotations

import json
import os

from app.core.runtime_keys import KNOWN_KEY_NAMES, RuntimeKeyStore


# ---------------------------------------------------------------------------
# Fake settings used by test_apply_merges_into_settings
# ---------------------------------------------------------------------------

class _FakeSettings:
    """Minimal stand-in for the pydantic Settings object.

    ``model_copy(update=...)`` is the only pydantic method the overlay calls,
    so we replicate just that behaviour.
    """

    def __init__(self) -> None:
        self.AI_API_KEY = ""
        self.AI_API_KEY_2 = ""
        self.AI_API_KEY_3 = ""
        self.BRAVE_SEARCH_API_KEY = ""
        self.TAVILY_SEARCH_API_KEY = ""
        self.LEADS_API_KEY = ""
        self.AI_PROVIDER = "test"

    def model_copy(self, *, update: dict) -> "_FakeSettings":
        for k, v in update.items():
            setattr(self, k, v)
        return self


def _store(tmp_path, name: str = "test_keys.json") -> RuntimeKeyStore:
    """Convenience: a fresh RuntimeKeyStore backed by a tmp file."""
    return RuntimeKeyStore(path=str(tmp_path / name))


# ---------------------------------------------------------------------------
# 1. Fresh store has empty overlay
# ---------------------------------------------------------------------------

def test_fresh_store_has_empty_overlay(tmp_path) -> None:
    """A brand-new store with no file on disk loads as empty dict."""
    store = _store(tmp_path)
    assert store.load() == {}


# ---------------------------------------------------------------------------
# 2. set + load roundtrip
# ---------------------------------------------------------------------------

def test_set_and_load_roundtrip(tmp_path) -> None:
    """set() writes to disk; load() reads it back with the same value."""
    store = _store(tmp_path)
    store.set("TAVILY_SEARCH_API_KEY", "tvly-123456")
    overlay = store.load()
    assert overlay == {"TAVILY_SEARCH_API_KEY": "tvly-123456"}


# ---------------------------------------------------------------------------
# 3. set empty clears the key
# ---------------------------------------------------------------------------

def test_set_empty_clears_the_key(tmp_path) -> None:
    """Setting a value to '' removes it from the overlay file."""
    store = _store(tmp_path)
    store.set("TAVILY_SEARCH_API_KEY", "tvly-abc")
    assert "TAVILY_SEARCH_API_KEY" in store.load()

    store.set("TAVILY_SEARCH_API_KEY", "")
    overlay = store.load()
    assert "TAVILY_SEARCH_API_KEY" not in overlay


# ---------------------------------------------------------------------------
# 4. masked never returns full value
# ---------------------------------------------------------------------------

def test_masked_never_returns_full_value(tmp_path) -> None:
    """masked() must show only the last four characters, prefixed with bullets."""
    store = _store(tmp_path)
    secret = "tvly-super-secret-key-1234"
    store.set("TAVILY_SEARCH_API_KEY", secret)

    masked = store.masked("TAVILY_SEARCH_API_KEY")
    assert secret not in masked, "full value must never appear in masked output"
    assert masked == f"••••{secret[-4:]}"


def test_masked_returns_empty_when_unset(tmp_path) -> None:
    """masked() returns '' when neither overlay nor env_value is set."""
    store = _store(tmp_path)
    assert store.masked("TAVILY_SEARCH_API_KEY") == ""


def test_masked_falls_back_to_env_value(tmp_path) -> None:
    """masked() uses env_value when the overlay has no entry for the key."""
    store = _store(tmp_path)
    result = store.masked("TAVILY_SEARCH_API_KEY", env_value="tvly-env-tail")
    assert result == "••••tail"
    assert "tvly-env-tail" not in result


# ---------------------------------------------------------------------------
# 5. is_set checks overlay first
# ---------------------------------------------------------------------------

def test_is_set_true_when_overlay_has_key(tmp_path) -> None:
    """is_set returns True when the overlay stores a value for the key."""
    store = _store(tmp_path)
    store.set("TAVILY_SEARCH_API_KEY", "tvly-999")
    assert store.is_set("TAVILY_SEARCH_API_KEY") is True


def test_is_set_true_from_env_value(tmp_path) -> None:
    """is_set returns True when env_value is non-empty, even without overlay."""
    store = _store(tmp_path)
    assert store.is_set("TAVILY_SEARCH_API_KEY", env_value="from-env") is True


def test_is_set_false_when_neither_present(tmp_path) -> None:
    """is_set returns False when both overlay and env_value are empty."""
    store = _store(tmp_path)
    assert store.is_set("TAVILY_SEARCH_API_KEY") is False
    assert store.is_set("TAVILY_SEARCH_API_KEY", env_value="") is False


# ---------------------------------------------------------------------------
# 6. apply merges into settings
# ---------------------------------------------------------------------------

def test_apply_merges_overlay_into_settings(tmp_path) -> None:
    """apply() copies settings with overlay values merged in."""
    store = _store(tmp_path)
    store.set("TAVILY_SEARCH_API_KEY", "tvly-overlay")
    store.set("AI_API_KEY", "sk-overlay")

    original = _FakeSettings()
    assert original.AI_API_KEY == ""
    assert original.TAVILY_SEARCH_API_KEY == ""

    updated = store.apply(original)

    # The returned object has overlay values.
    assert updated.TAVILY_SEARCH_API_KEY == "tvly-overlay"
    assert updated.AI_API_KEY == "sk-overlay"
    # Unset fields keep their original defaults.
    assert updated.BRAVE_SEARCH_API_KEY == ""
    assert updated.AI_PROVIDER == "test"


def test_apply_returns_settings_unchanged_when_empty(tmp_path) -> None:
    """apply() returns the same settings object when overlay is empty."""
    store = _store(tmp_path)
    original = _FakeSettings()
    result = store.apply(original)
    assert result is original


# ---------------------------------------------------------------------------
# 7. Unknown key name is silently ignored
# ---------------------------------------------------------------------------

def test_unknown_key_name_silently_ignored(tmp_path) -> None:
    """set() with a name not in KNOWN_KEY_NAMES does nothing."""
    store = _store(tmp_path)
    store.set("UNKNOWN_KEY", "val")
    overlay = store.load()
    assert overlay == {}


# ---------------------------------------------------------------------------
# 8. Corrupt JSON file is handled
# ---------------------------------------------------------------------------

def test_corrupt_json_file_returns_empty(tmp_path) -> None:
    """A file containing garbage JSON is treated as an empty overlay."""
    p = tmp_path / "corrupt.json"
    p.write_text("{not valid json {{{", encoding="utf-8")

    store = RuntimeKeyStore(path=str(p))
    assert store.load() == {}


# ---------------------------------------------------------------------------
# 9. Non-existent file is handled
# ---------------------------------------------------------------------------

def test_nonexistent_file_returns_empty(tmp_path) -> None:
    """A path that doesn't exist on disk loads as empty overlay."""
    store = RuntimeKeyStore(path=str(tmp_path / "no_such_file.json"))
    assert store.load() == {}


# ---------------------------------------------------------------------------
# Bonus: KNOWN_KEY_NAMES contract
# ---------------------------------------------------------------------------

def test_known_key_names_contains_expected_entries() -> None:
    """KNOWN_KEY_NAMES must include the six current secret keys."""
    assert len(KNOWN_KEY_NAMES) == 6
    for name in (
        "AI_API_KEY",
        "AI_API_KEY_2",
        "AI_API_KEY_3",
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_SEARCH_API_KEY",
        "LEADS_API_KEY",
    ):
        assert name in KNOWN_KEY_NAMES


def test_apply_live_mutates_settings_in_place_and_reports_changed() -> None:
    """apply_live updates the RUNNING settings object in place.

    The hot-reload path depends on in-place mutation: modules hold references
    to the live settings object, so a copy would leave every existing reader
    on the old values. It must also report WHICH keys changed (a no-op is not
    an applied change — honest logging, CLAUDE.md §6).
    """
    path = os.path.join(os.path.dirname(__file__), "_overlay_live_test.json")
    try:
        store = RuntimeKeyStore(path)
        s = _FakeSettings()
        env_values = {"TAVILY_SEARCH_API_KEY": "tvly-env", "BRAVE_SEARCH_API_KEY": ""}

        # Overlay a Tavily key over its .env value.
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"TAVILY_SEARCH_API_KEY": "tvly-hot"}, fh)
        changed = store.apply_live(s, env_values)
        assert s.TAVILY_SEARCH_API_KEY == "tvly-hot"  # overlay wins
        assert changed == ["TAVILY_SEARCH_API_KEY"]

        # Overlay removed -> falls back to the .env value, like a restart.
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({}, fh)
        changed = store.apply_live(s, env_values)
        assert s.TAVILY_SEARCH_API_KEY == "tvly-env"
        assert changed == ["TAVILY_SEARCH_API_KEY"]

        # Nothing left to change -> no keys reported.
        assert store.apply_live(s, env_values) == []
    finally:
        if os.path.exists(path):
            os.remove(path)

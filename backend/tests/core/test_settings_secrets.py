"""Guards that credentials never reach a ``repr`` of ``Settings``.

WHY THIS FILE EXISTS — a real incident, not a hypothetical:

On 2026-08-19 an unrelated test failure printed the live AI router key and the
live Tavily key dozens of times, and they travelled from the terminal into a
pasted chat log. Nothing had printed them on purpose. The path was:

    pydantic's generated Settings.__repr__ includes EVERY field value
      -> pytest embeds that repr in the AttributeError message
      -> any failing test that merely touches `settings` leaks every secret

So the leak needs no bug of its own; it rides along on *someone else's*
failure. That makes it a design defect, fixed by declaring credential fields
``repr=False`` in ``app/core/config.py`` (CLAUDE.md section 7: permanent fix at
the root cause, never a reminder to be careful).

These tests are the regression guard. The second one is the important one: it
enforces the RULE for fields that do not exist yet, so a credential added in six
months is covered without anyone remembering this incident.

No real credential appears here. ``_env_file=None`` keeps the real
``backend/.env`` out of the constructed object entirely, so even a failing
assertion cannot print a live key.

Run: python -m pytest tests/core/test_settings_secrets.py -q
"""

from __future__ import annotations

from app.core.config import Settings

# Obvious fakes. If one of these ever shows up in output, the guard has failed.
_FAKE_AI_KEY = "sk-fake-ai-key-not-a-real-credential"
_FAKE_TAVILY_KEY = "tvly-fake-key-not-a-real-credential"
_FAKE_BRAVE_KEY = "brave-fake-key-not-a-real-credential"

# A field whose name ends with one of these holds a credential. Naming-based on
# purpose: the rule has to apply to fields nobody has written yet, and every
# credential in this project is already named this way.
_SECRET_NAME_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_KEY")

# Names that end in a secret-looking suffix but are NOT credentials. Kept as an
# explicit allow-list so adding one is a visible, reviewable decision rather
# than a silent hole in the rule above.
_NOT_SECRETS: frozenset[str] = frozenset()


def _build_settings() -> Settings:
    """Construct Settings with fake secrets and no .env involvement.

    Returns:
        A fully-populated Settings instance carrying only fake credentials.
    """
    return Settings(
        _env_file=None,
        DATABASE_URL="sqlite:///./test-not-real.db",
        AI_API_KEY=_FAKE_AI_KEY,
        TAVILY_SEARCH_API_KEY=_FAKE_TAVILY_KEY,
        BRAVE_SEARCH_API_KEY=_FAKE_BRAVE_KEY,
    )


def _secret_field_names() -> list[str]:
    """Every declared field whose name marks it as a credential.

    Returns:
        Sorted field names that the repr rule must cover.
    """
    return sorted(
        name
        for name in Settings.model_fields
        if name not in _NOT_SECRETS
        and name.upper().endswith(_SECRET_NAME_SUFFIXES)
    )


def test_repr_does_not_leak_any_credential() -> None:
    """The exact failure from 2026-08-19: repr(settings) must be secret-free.

    This is the behavioural test — it reproduces the leak path rather than
    inspecting metadata, so it fails even if the fix is implemented some other
    way and then broken.
    """
    text = repr(_build_settings())

    for fake in (_FAKE_AI_KEY, _FAKE_TAVILY_KEY, _FAKE_BRAVE_KEY):
        assert fake not in text, (
            f"credential leaked into repr(Settings): {fake!r}. Declare that field "
            "as Field(default='', repr=False) in app/core/config.py."
        )


def test_str_does_not_leak_any_credential() -> None:
    """``str()`` must be as safe as ``repr()``.

    Logging usually goes through ``str``/``%s``, not ``repr``, so a fix that
    only covered ``repr`` would still leak through every log statement.
    """
    text = str(_build_settings())

    for fake in (_FAKE_AI_KEY, _FAKE_TAVILY_KEY, _FAKE_BRAVE_KEY):
        assert fake not in text, f"credential leaked into str(Settings): {fake!r}"


def test_every_credential_field_is_excluded_from_repr() -> None:
    """THE RULE, applied to fields that do not exist yet.

    The two tests above only cover the three credentials known today. This one
    enforces the invariant by name, so adding ``FOO_API_KEY`` without
    ``repr=False`` fails here instead of leaking on some future bad day.
    """
    secrets = _secret_field_names()
    assert secrets, "no credential fields found — has the naming rule drifted?"

    exposed = [
        name for name in secrets if Settings.model_fields[name].repr is not False
    ]
    assert not exposed, (
        f"credential field(s) still included in repr: {exposed}. Declare each as "
        "Field(default='', repr=False) in app/core/config.py, or add the name to "
        "_NOT_SECRETS here if it genuinely holds no credential."
    )


def test_secrets_are_still_readable_normally() -> None:
    """``repr=False`` must hide the value, not break reading it.

    Guards against a future "fix" that switches these fields to ``SecretStr``
    (or similar) without updating the read sites, which would silently send a
    masked placeholder to the AI router as if it were the key.
    """
    settings = _build_settings()

    assert settings.AI_API_KEY == _FAKE_AI_KEY
    assert settings.TAVILY_SEARCH_API_KEY == _FAKE_TAVILY_KEY
    assert settings.BRAVE_SEARCH_API_KEY == _FAKE_BRAVE_KEY
    # A plain str, so provider code can pass it straight to an SDK.
    assert isinstance(settings.AI_API_KEY, str)


def test_non_secret_settings_stay_visible_in_repr() -> None:
    """Only credentials are hidden; diagnosability must survive.

    A repr that hid everything would close the leak and destroy the reason the
    repr exists — knowing which endpoint and model a failing run actually used
    is exactly how the 2026-08-19 transport bugs were diagnosed.
    """
    text = repr(_build_settings())

    assert "AI_PROVIDER" in text
    assert "AI_BASE_URL" in text
    assert "AI_MODEL" in text

"""LocalPartMatcher — deterministic level table."""

from __future__ import annotations

from app.person_research.models import LocalPartMatchLevel
from app.person_research.scoring import LocalPartMatcher


def _level(local: str, name: str) -> str:
    return LocalPartMatcher().level(local, name).value


def test_exact_joined():
    assert _level("johnsmith", "John Smith") == "exact"


def test_exact_dotted():
    assert _level("john.smith", "John Smith") == "exact"


def test_exact_dashed():
    assert _level("john-smith", "John Smith") == "exact"


def test_first_last_middle_initial():
    assert _level("john.m.smith", "John Smith") == "first_last"


def test_initial_last_joined():
    assert _level("jsmith", "John Smith") == "initial_last"


def test_initial_last_dotted():
    assert _level("j.smith", "John Smith") == "initial_last"


def test_first_only():
    assert _level("john", "John Smith") == "first"


def test_reject_unrelated():
    assert _level("bob.jones", "John Smith") == "reject"


def test_reject_single_token_mismatch():
    assert _level("smith", "John Smith") == "reject"


def test_single_token_name_exact():
    assert _level("cher", "Cher") == "exact"


def test_case_insensitive():
    assert _level("JohnSmith", "john smith") == "exact"


def test_empty_name_rejects():
    assert _level("johnsmith", "") == "reject"


def test_empty_local_rejects():
    assert _level("", "John Smith") == "reject"


def test_role_title_not_name():
    # A role word alone must never be treated as a bindable person.
    assert _level("pm", "Project Manager") == "reject"

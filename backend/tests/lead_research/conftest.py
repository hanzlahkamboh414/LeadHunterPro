"""Shared fixtures for lead_research tests."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock


def make_fake_ai(response_data: dict[str, Any]) -> Any:
    """Create a fake AI function that returns JSON response_data."""
    raw = json.dumps(response_data)

    def fake_ai_ask(prompt: str) -> str:
        return raw

    return fake_ai_ask


def make_fake_search(results: list[dict[str, str]] | None = None) -> Any:
    """Create a fake search function that returns fixed results."""
    if results is None:
        results = [
            {"url": "https://example.com/about", "title": "About Us", "snippet": "Construction company"},
            {"url": "https://example.com/contact", "title": "Contact", "snippet": "info@example.com"},
        ]

    def fake_search(query: str) -> list[dict[str, str]]:
        return results

    return fake_search


def make_fake_fetch(pages: dict[str, str] | None = None) -> Any:
    """Create a fake fetch function that returns fixed HTML content."""
    if pages is None:
        pages = {
            "https://example.com": "<html><body>Acme Construction</body></html>",
            "https://example.com/about": "<html><body>About Acme Construction - Estimating Services</body></html>",
            "https://example.com/contact": "<html><body>Contact: info@example.com</body></html>",
        }

    def fake_fetch(url: str) -> Any:
        html = pages.get(url, "")
        mock = MagicMock()
        mock.ok = bool(html)
        mock.html = html
        return mock

    return fake_fetch


def make_fake_refine(domain: str = "example.com") -> Any:
    """Create a fake domain refinement function."""
    def fake_refine(raw_domain: str) -> str:
        return domain
    return fake_refine

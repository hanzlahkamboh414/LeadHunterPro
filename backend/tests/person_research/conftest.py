"""Shared fixtures for person_research tests — fully deterministic, no network."""

from __future__ import annotations

import pytest

from app.person_research.sources import PageFetch


def team_page(person_name: str, role: str, email: str) -> str:
    """A single people page: one person card with a region-bound mailto."""
    return f"""<html><head><title>About ACME</title></head><body>
      <div class="team">
        <div>
          {person_name}
          {role}
          <a href="mailto:{email}">Email</a>
        </div>
      </div>
    </body></html>"""


def homepage(*links: str) -> str:
    """Homepage with links to people-identification pages."""
    anchors = "".join(f'<a href="{href}">{label}</a>' for href, label in links)
    return f"""<html><head><title>ACME Build</title></head><body>{anchors}</body></html>"""


def make_fetch(pages: dict[str, str]):
    """Return a fetch_page callable over a static url->html map.

    Any url not present (including robots.txt) returns ``ok=False``.
    """
    def fetch(url: str) -> PageFetch:
        html = pages.get(url)
        if html is None:
            return PageFetch(url=url, html="", ok=False)
        return PageFetch(url=url, html=html, ok=True)

    return fetch


@pytest.fixture
def acme_pages() -> dict[str, str]:
    """A site whose team page binds johnsmith@acmebuild.com to 'John Smith'."""
    return {
        "https://acmebuild.com": homepage(("/team", "Team"), ("/contact", "Contact")),
        "https://acmebuild.com/team": team_page("John Smith", "Project Manager", "johnsmith@acmebuild.com"),
    }

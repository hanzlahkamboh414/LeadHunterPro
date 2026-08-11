"""AI summarizer smoke test — guarded so the offline suite never hits the network.

This file originally ran a LIVE Anthropic call at module import time. Because
pytest collects every ``test*.py`` before ``-m "not network"`` markers apply,
that import-time call could hang an "offline" run. The live call now lives in
a ``@pytest.mark.network`` test (deselected offline); the manual script path is
preserved under ``python backend/test_summary.py``.
"""

from __future__ import annotations

import pytest

from app.ai.summarizer import CompanySummarizer


def _fact_sheet() -> dict:
    """A minimal input shape for the summarizer (see CompanySummarizer)."""
    return {
        "title": "OpenAI",
        "description": "Artificial Intelligence Research Company",
        "emails": [],
        "phones": [],
        "linkedin": ["https://linkedin.com/company/openai"],
    }


@pytest.mark.network
def test_summary_network_smoke():
    """The AI summarizer returns a non-empty summary for a sample fact sheet."""
    summary = CompanySummarizer().summarize(_fact_sheet())
    assert isinstance(summary, str)
    assert summary.strip()


if __name__ == "__main__":
    # Manual-run equivalent (python backend/test_summary.py) — no pytest context.
    print(CompanySummarizer().summarize(_fact_sheet()))
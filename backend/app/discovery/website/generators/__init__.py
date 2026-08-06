"""Candidate generators for website discovery.

Generators produce website URLs (Candidate objects) for the discovery pipeline.
Each generator implements the CandidateGenerator interface from Unit 6.

Available generators are registered here and selected by the orchestration plugin.
"""

from __future__ import annotations

from app.discovery.website.generators.brave import BraveSearchGenerator
from app.discovery.website.generators.fixture import FixtureGenerator
from app.discovery.website.generators.searx import SearXNGGenerator

__all__ = [
    "BraveSearchGenerator",
    "SearXNGGenerator",
    "FixtureGenerator",
]

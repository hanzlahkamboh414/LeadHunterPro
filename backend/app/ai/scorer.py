"""AI-powered lead qualification and scoring."""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

from app.ai.gateway import AIGateway
from app.ai.prompts.lead_score import (
    build_lead_qualification_prompt,
    build_lead_score_prompt,
)

logger = logging.getLogger(__name__)


class CompanyScorer:
    """Score companies using rule-based heuristics or AI qualification.

    ``calculate()`` is the deterministic information-richness score (0-100).
    ``qualify()`` computes the deterministic score first, then — when AI is
    enabled — asks the configured provider to evaluate lead *quality* and
    blends both into a final normalized score. AI failure always falls back
    to the deterministic score, never blocking the pipeline.
    """

    def __init__(self, use_ai: bool = False) -> None:
        self._use_ai = use_ai
        self._gateway = AIGateway() if use_ai else None

    def calculate(self, data: dict) -> int:
        """Calculate a deterministic lead score for the given company data.

        Args:
            data: Crawled website data dictionary.

        Returns:
            Integer score between 0 and 100.
        """
        if self._use_ai and self._gateway is not None:
            prompt = build_lead_score_prompt(data)
            response = self._gateway.ask(prompt)
            try:
                result = json.loads(response)
                return int(result.get("score", 0))
            except (json.JSONDecodeError, ValueError, KeyError):
                logger.warning(
                    "AI scoring failed; falling back to heuristic", exc_info=True
                )

        return self._deterministic_score(data)

    def _deterministic_score(self, data: dict) -> int:
        """Information-richness score shared by ``calculate`` and ``qualify``."""
        score = 0
        if data.get("title"):
            score += 20
        if data.get("description"):
            score += 10
        if data.get("linkedin"):
            score += 20
        if data.get("emails"):
            score += 15
        if data.get("phones"):
            score += 10
        if data.get("contact_page"):
            score += 15
        if data.get("about_page"):
            score += 10
        return min(score, 100)

    def qualify(self, company: dict, query: dict | None = None) -> dict:
        """Hybrid lead qualification: deterministic + AI, deterministic fallback.

        Always computes the deterministic score first. When AI is enabled it
        asks the configured provider to evaluate lead quality and blends both
        into the final score; any AI failure (raise, timeout, malformed JSON,
        missing/invalid score) falls back to the deterministic score.

        Args:
            company: Supplied company evidence (evaluated by AI).
            query: Query context with optional ``industry`` and ``location``.

        Returns:
            Qualification metadata dict with keys:

            - ``score``: final normalized 0-100 (deterministic when AI unavailable)
            - ``deterministic_score``: rule-based information-richness score
            - ``ai_score``: AI quality score (0-100) or ``None``
            - ``ai_confidence``: evidence-backed confidence 0-100 (Lead gate
              rule #6). The AI's stated confidence when supplied and valid;
              falls back to ``score`` when omitted, to ``deterministic_score``
              when AI is unused or fails.
            - ``justification``: written reasoning citing the specific evidence
              used (falls back to ``qualification`` when omitted; empty when AI
              unavailable). The Lead gate requires it non-empty.
            - ``qualified``: AI's own verdict (bool) or ``None`` when AI unused
            - ``qualification``: AI one-line fit summary (``""`` when unused)
            - ``reasons`` / ``strengths`` / ``concerns``: AI lists (``[]`` when unused)
            - ``ai_used``: whether the AI result was applied
            - ``error``: failure reason or ``None``
        """
        deterministic = self._deterministic_score(company)
        result: dict[str, Any] = {
            "score": deterministic,
            "deterministic_score": deterministic,
            "ai_score": None,
            "ai_confidence": deterministic,
            "justification": "",
            "qualified": None,
            "qualification": "",
            "reasons": [],
            "strengths": [],
            "concerns": [],
            "ai_used": False,
            "error": None,
        }
        if not (self._use_ai and self._gateway is not None):
            return result

        try:
            prompt = build_lead_qualification_prompt(company, query)
            response = self._gateway.ask(prompt)
            parsed = self._parse_ai_response(response)
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "AI qualification failed; using deterministic score: %s",
                result["error"],
            )
            return result

        ai_score = self._coerce_score(parsed.get("score")) if parsed else None
        if ai_score is None:
            result["error"] = "malformed AI response (no valid score)"
            logger.warning(
                "AI qualification returned malformed output; using deterministic score"
            )
            return result

        final_score = round(0.5 * deterministic + 0.5 * ai_score)
        confidence = self._coerce_score(parsed.get("confidence"))
        result.update(
            {
                "score": final_score,
                "ai_score": ai_score,
                "qualified": bool(parsed.get("qualified")),
                "qualification": str(parsed.get("qualification") or ""),
                "reasons": self._as_str_list(parsed.get("reasons")),
                "strengths": self._as_str_list(parsed.get("strengths")),
                "concerns": self._as_str_list(parsed.get("concerns")),
                # The Lead gate consumes these two (rule #6). ai_confidence is
                # the AI's evidence-backed 0-100 when supplied, else the blended
                # score; justification falls back to the one-line qualification.
                "ai_confidence": confidence if confidence is not None else final_score,
                "justification": str(parsed.get("justification") or "")
                or str(parsed.get("qualification") or ""),
                "ai_used": True,
                "error": None,
            }
        )
        return result

    @staticmethod
    def _coerce_score(raw: Any) -> int | None:
        """Coerce *raw* to a 0-100 int, or ``None`` when not a finite number."""
        if isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        return max(0, min(100, round(value)))

    @staticmethod
    def _parse_ai_response(text: str) -> dict | None:
        """Parse provider text into a dict, tolerating ```json code fences.

        Only complete, valid JSON objects are accepted. The whole first-``{``
        to last-``}`` span is tried first (the common single-object case); if
        that does not decode — e.g. prose braces before the object, or trailing
        text after it — candidate balanced ``{...}`` regions are tried from the
        end. Truncated JSON (missing its closing brace) never decodes, so it
        still falls back to the deterministic score.
        """
        if not text:
            return None
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[a-zA-Z0-9]*\n?", "", stripped)
            stripped = re.sub(r"\n?```\s*$", "", stripped).strip()
        if not stripped:
            return None

        def _loads(candidate: str) -> dict | None:
            try:
                obj = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                return None
            return obj if isinstance(obj, dict) else None

        # Fast path: the whole span between the first { and the last }.
        first = stripped.find("{")
        last = stripped.rfind("}")
        if first != -1 and last > first:
            parsed = _loads(stripped[first : last + 1])
            if parsed is not None:
                return parsed

        # Defensive path: for each closing brace (from the end) find its
        # matching opening brace and accept the first region that decodes as a
        # complete JSON object. Prefers the last complete object in the text.
        def _matched_start(end: int) -> int:
            depth = 0
            for i in range(end, -1, -1):
                ch = stripped[i]
                if ch == "}":
                    depth += 1
                elif ch == "{":
                    depth -= 1
                    if depth == 0:
                        return i
            return -1

        for end in range(len(stripped) - 1, -1, -1):
            if stripped[end] != "}":
                continue
            start = _matched_start(end)
            if start == -1:
                continue
            parsed = _loads(stripped[start : end + 1])
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _as_str_list(raw: Any) -> list[str]:
        """Normalize *raw* to a bounded list of non-empty strings."""
        if isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, (list, tuple)):
            items = list(raw)
        else:
            items = []
        cleaned = [str(i).strip() for i in items if str(i).strip()]
        return cleaned[:8]

"""Confirmation diagnostic — prove max_tokens exhaustion on real demo leads.

Feeds the exact companies from the real demo run (``backend/output/companies.json``)
to ``CompanyScorer.qualify()`` and captures, per response:

- ``message.stop_reason``            -> "max_tokens" proves truncation
- ``message.content`` block count
- per-block type, ``thinking_len`` / ``text_len``
- whether the text block ends on a JSON-closing token ("ends normally")
- whether ``_parse_ai_response()`` succeeds

``max_tokens=1024`` is kept identical to ``AnthropicProvider.generate()`` so the
run reproduces the exact demo conditions. No API key/token or full prompt is
ever printed. Read-only w.r.t. production code: no production file is imported
with side effects beyond what the app already does. Run from ``backend/``:

    python diagnose_ai_response.py
"""

from __future__ import annotations

from app.ai.providers.anthropic_provider import AnthropicProvider
from app.ai.prompts.lead_score import build_lead_qualification_prompt
from app.ai.scorer import CompanyScorer
from app.core.config import settings

print("=== CONFIG (no secrets) ===")
print("AI_PROVIDER:", settings.AI_PROVIDER)
print("ANTHROPIC_BASE_URL:", settings.ANTHROPIC_BASE_URL)
print("ANTHROPIC_MODEL:", settings.ANTHROPIC_MODEL)
print("ANTHROPIC_API_KEY present:", bool(settings.ANTHROPIC_API_KEY))

# Capture the exact raw message for the exact text qualify() parses.
_captured: dict[str, object] = {}


def _capturing_generate(self, prompt: str) -> str:  # type: ignore[no-untyped-def]
    # Same max_tokens as AnthropicProvider.generate -> reproduces the demo.
    message = self._client.messages.create(
        model=self._model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    _captured[prompt] = message
    for block in message.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise RuntimeError("Anthropic response contained no text block.")


AnthropicProvider.generate = _capturing_generate  # type: ignore[assignment]

# Real companies from the real demo run (backend/output/companies.json),
# reconstructed the same way demo.stage_scoring() builds the data dict.
# (name, industry, location, description, website, source_url)
SAMPLES = [
    ("Atlas Roofing Corporation", "Roofing", "Dallas Texas",
     "Commercial roofing contractor",
     "https://www.atlasroofing.com", "https://www.atlasroofing.com/about"),
    ("Owens Corning", "Roofing", "Dallas Texas",
     "Roofing materials and shingles manufacturer",
     "https://www.owenscorning.com", "https://www.owenscorning.com/en-us/home"),
    ("American Construction Roofing", "Roofing", "Dallas Texas",
     "Commercial and residential roofing services",
     "https://www.americanroofingservices.com",
     "https://www.americanroofingservices.com/services"),
    ("Southwest Roofing Pros", "Roofing", "Dallas Texas",
     "Southwest-style roofing and storm damage repair",
     "https://www.southwestroofingpros.com",
     "https://www.southwestroofingpros.com/contact"),
    ("Star Roofing Contractors", "Roofing", "Dallas Texas",
     "Licensed roofing contractor for residential and commercial",
     "https://www.starroofingcontractors.com",
     "https://www.starroofingcontractors.com/locations"),
]


def _build_data(name, industry, location, desc, website, source_url):
    return {
        "title": name,
        "company_name": name,
        "website": website,
        "city": "Dallas",
        "state": "TX",
        "trade_category": "Roofing",
        "industry_focus": desc,
        "description": desc,
        "source": "texas_procurement",
        "source_url": source_url,
        "emails": [],
        "phones": [],
    }


scorer = CompanyScorer(use_ai=True)
summary: list[tuple[str, str | None, bool]] = []
for name, industry, location, desc, website, source_url in SAMPLES:
    data = _build_data(name, industry, location, desc, website, source_url)
    query = {"industry": industry, "location": location}
    prompt = build_lead_qualification_prompt(data, query)
    result = scorer.qualify(data, query)
    ok = result["ai_used"] and result["ai_score"] is not None
    msg = _captured.get(prompt)
    stop_reason = getattr(msg, "stop_reason", None) if msg is not None else None
    summary.append((name, stop_reason, ok))

    print(
        "\n=== %s -> ai_used=%s ai_score=%s error=%r"
        % (name, result["ai_used"], result["ai_score"], result["error"])
    )
    if msg is None:
        print("  (no raw message captured)")
        continue
    print("  prompt chars: %d (content not printed)" % len(prompt))
    print("  RAW stop_reason: %r" % stop_reason)
    content = getattr(msg, "content", None)
    print("  RAW message.content: %d block(s)" % (len(content) if content else 0))
    text_parts: list[str] = []
    for i, block in enumerate(content or []):
        btype = getattr(block, "type", None)
        txt = getattr(block, "text", None)
        th = getattr(block, "thinking", None)
        if txt is not None:
            text_parts.append(txt)
            print(
                "    block[%d] type=%r text_len=%d head=%r tail=%r"
                % (i, btype, len(txt), txt[:120], txt[-120:])
            )
        elif th is not None:
            print(
                "    block[%d] type=%r thinking_len=%d head=%r"
                % (i, btype, len(th), str(th)[:120])
            )
        else:
            print("    block[%d] type=%r (no text/thinking attr)" % (i, btype))
    joined = "".join(text_parts)
    tail = joined.strip()[-80:]
    print(
        "  text joins to %d chars; ends_normally=%s tail=%r"
        % (len(joined), tail.endswith("}"), tail)
    )
    parsed = CompanyScorer._parse_ai_response(joined)
    print("  _parse_ai_response(joined) -> %r" % (parsed,))
    if parsed:
        print(
            "    has score=%r  _coerce_score -> %r"
            % (parsed.get("score"), CompanyScorer._coerce_score(parsed.get("score")))
        )
    if not ok and text_parts:
        first = text_parts[0]
        print(
            "  first-text-block len=%d -> _parse_ai_response=%r"
            % (len(first), CompanyScorer._parse_ai_response(first))
        )

print("\n=== SUMMARY ===")
for name, stop_reason, ok in summary:
    print("  %-32s ok=%-5s stop_reason=%r" % (name, ok, stop_reason))
malformed = [s for s in summary if not s[2]]
print(
    "  parsed OK: %d/%d; malformed: %d"
    % (len(summary) - len(malformed), len(summary), len(malformed))
)
for name, stop_reason, ok in malformed:
    print("  MALFORMED %-32s stop_reason=%r" % (name, stop_reason))

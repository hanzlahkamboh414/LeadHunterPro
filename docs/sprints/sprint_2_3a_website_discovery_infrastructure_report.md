# Sprint 2.3A — Direct Website Discovery Infrastructure Report

**Date:** 2026-08-05
**Type:** Implementation (infrastructure only)
**Status:** IN PROGRESS — Units 1–3 verified, Unit 4 awaiting verification

---

## Scope

Phase 2.3A builds **only** the infrastructure required by the first Direct
Website Discovery Plugin. It performs no crawling, no extraction and no
discovery.

Explicitly out of scope, by instruction: HTTP requests, `aiohttp`,
`requests`, Playwright, Selenium, BeautifulSoup/selectolax parsing,
robots handling, HTML parsing, extraction, scoring algorithms, and the
Government / AI / Directory plugins.

Implementation proceeds one unit at a time. No unit begins before the
previous one is verified by a pasted terminal result.

---

## Verified Test Counts

Counts below are **verified**: each is a pasted `python -m pytest` result,
not an estimate.

| Unit | Component | Spec Item | Command | Result |
|---|---|---|---|---|
| 1 | URL Normalizer | 3 | `python -m pytest tests/discovery/website/test_url_normalizer.py -q` | **56 passed** ✓ |
| 2 | Duplicate URL Filter | 4 | `python -m pytest tests/discovery/website/test_url_filter.py -q` | **42 passed** ✓ |
| 3 | Confidence Model | 9 | `python -m pytest tests/discovery/website/test_confidence.py -q` | **44 passed** ✓ |
| 4 | Evidence Model | 8 | `python -m pytest tests/discovery/website/test_evidence.py -q` | **90 passed** ✓ |
| 5 | Extractor Interfaces | 7 | `python -m pytest tests/discovery/website/test_extractors.py -q` | *awaiting verification* |

**Phase total so far: 232 verified.**

**Correction:** the Unit 3 implementation report stated 41 tests. The
verified result is **44 passed**. The figure of 41 was an estimate written
before execution and was wrong; 44 is the executed count. No code was
affected — every test passed in both cases. This table is now the single
source of truth for test counts in this phase.

**Verification invocation:** `python -m pytest` from `backend/`. Bare
`pytest` fails with `ModuleNotFoundError: No module named 'app'` — the repo
checks in no pytest configuration, so nothing puts `backend/` on
`sys.path`; `python -m` prepends the working directory, the console script
does not.

---

## Files Created

### Production (`backend/app/discovery/website/`)

| File | Unit | Description |
|---|---|---|
| `__init__.py` | 1 | Package init, public API exports |
| `url_normalizer.py` | 1 | URL canonicalization — fetchable form and dedup key |
| `url_filter.py` | 2 | `DuplicateURLFilter` + `FilterStats` |
| `confidence.py` | 3 | `Confidence` value object, model only |
| `evidence.py` | 4 | `ExtractedField`, `FieldEvidence`, `EvidenceSet` |
| `extractors.py` | 5 | `PageContent` Protocol, `ExtractionResult`, `FieldExtractor` + 7 field interfaces |

### Tests (`backend/tests/discovery/website/`)

| File | Unit |
|---|---|
| `test_url_normalizer.py` | 1 |
| `test_url_filter.py` | 2 |
| `test_confidence.py` | 3 |
| `test_evidence.py` | 4 |
| `test_extractors.py` | 5 |

### Documentation

| File | Description |
|---|---|
| `docs/sprints/sprint_2_3a_website_discovery_infrastructure_report.md` | This report |

---

## Files Modified

| File | Change |
|---|---|
| `backend/app/discovery/website/__init__.py` | Re-exports extended as each unit landed |

No file outside `app/discovery/website/` has been modified in this phase.

## Files Deleted / Renamed

**None.** No file has been deleted or renamed in Phase 2.3A.

---

## Dependency Graph

```
app/discovery/website/
│
├── url_normalizer.py        (leaf — stdlib only: re, urllib.parse)
│       ▲
│       ├──── url_filter.py          (canonical_key → dedup identity)
│       └──── evidence.py            (normalize_url → page_url canonicalization)
│
├── confidence.py            (leaf — stdlib only: math, dataclasses)
│       ▲
│       └──── evidence.py            (Confidence → per-field belief)
│
├── evidence.py              (depends on url_normalizer + confidence)
│       ▲
│       └──── extractors.py          (ExtractedField, FieldEvidence)
│
└── extractors.py            (depends on evidence only)
                              PageContent is structural — no import of
                              app.crawlers, so no aiohttp is pulled in
```

Placement within the wider architecture:

```
SourceOrchestrator
   └── PluginSource            (Phase 2.2 adapter)
         └── PluginManager
               └── DirectWebsiteDiscoveryPlugin      ← Unit 8, not yet built
                     ├── CandidateGenerator          ← Unit 6, not yet built
                     │     └── DuplicateURLFilter    ← Unit 2 ✓
                     │           └── url_normalizer  ← Unit 1 ✓
                     └── Extractors                  ← Unit 5
                           └── FieldEvidence         ← Unit 4 ✓
                                 ├── url_normalizer  ← Unit 1 ✓
                                 └── Confidence      ← Unit 3 ✓
```

The package is a **leaf**: it imports nothing from `app.discovery.plugins`,
`app.discovery.sources`, `app.engines`, or `app.crawlers`. Nothing outside
the package imports it yet. Dependency direction is therefore one-way and
cycle-free by construction.

---

## Complexity Analysis

`n` = URL length, `m` = URLs in a batch, `t` = total observations,
`f` = distinct fields, `j` = observations for one field.

| Operation | Time (avg) | Time (worst) | Space |
|---|---|---|---|
| `normalize_url` / `canonical_key` / `extract_host` | O(n) | O(n) | O(n) |
| `DuplicateURLFilter.accept` | O(n) | O(n + m)¹ | O(1) amortized |
| `DuplicateURLFilter.filter` (m URLs) | O(m·n) | O(m·n + m²)¹ | O(m·n) |
| `DuplicateURLFilter.is_duplicate` | O(n) | O(n + m)¹ | O(n) |
| `Confidence(...)` construction | O(1) | O(1) | O(1) |
| `Confidence.meets` / predicates / `to_dict` | O(1) | O(1) | O(1) |
| `normalize_field` | O(k) | O(k) | O(k) |
| `FieldEvidence(...)` construction | O(n) | O(n) | O(n) |
| `FieldEvidence.to_dict` / `from_dict` | O(1) | O(1) | O(1) |
| `EvidenceSet.add` | O(1) | O(1) amortized² | O(1) |
| `EvidenceSet.for_field` | O(j) | O(t)³ | O(j) |
| `EvidenceSet.has` / `__contains__` | O(1) | O(1) | O(1) |
| `EvidenceSet.__len__` | O(f) | O(f) | O(1) |
| `EvidenceSet.__iter__` / `to_dict` | O(t) | O(t) | O(t) |
| `isinstance(page, PageContent)` | O(1) — 10 `hasattr` checks | O(1) | O(1) |
| `ExtractionResult(...)` | O(v) — one `strip` | O(v) | O(v) |
| `ExtractionResult.to_dict` | O(a) — attribute count | O(a) | O(a) |
| `FieldExtractor.describe` / `__repr__` | O(1) | O(1) | O(1) |

¹ Degenerate dict-collision case only. Python's hash randomization makes
this unreachable for adversarial URL sets in practice; treated as O(1).
² Amortized over dict/list growth.
³ When every observation belongs to one field.

**Worst case, whole subsystem.** Bounded by input size, not by
pathological behaviour: no operation backtracks, recurses, or rescans.
`_SCHEME_RE` is anchored and linear, so no regex blowup is possible.

**Average case.** Dominated by `normalize_url`, a single linear pass over
the URL. Everything downstream is constant-time dictionary work.

**Space.** `DuplicateURLFilter` retains two strings per distinct URL (key
and normalized form) — the deliberate cost of first-seen-wins semantics,
which cannot be achieved without remembering what was first seen.
`EvidenceSet` retains every observation, because collapsing duplicate
observations would destroy exactly what evidence exists to record.

---

## Public API

### Unit 1 — `url_normalizer.py`

| Symbol | Kind | Contract |
|---|---|---|
| `DEFAULT_SCHEME` | `str` | `"https"`, assumed for scheme-less candidates |
| `ALLOWED_SCHEMES` | `frozenset[str]` | `{"http", "https"}` |
| `normalize_url(url)` | function | Canonical **fetchable** URL, or `None`. Scheme preserved, never upgraded |
| `canonical_key(url)` | function | Scheme-**insensitive** identity key, or `None`. Not a URL; never fetch it |
| `extract_host(url)` | function | Canonical host, or `None` |

### Unit 2 — `url_filter.py`

| Symbol | Kind | Contract |
|---|---|---|
| `FilterStats` | frozen dataclass | `total`, `accepted`, `duplicates`, `invalid`, `to_dict()` |
| `DuplicateURLFilter` | class | First-seen-wins URL deduplication |
| `.accept(url)` | method | Normalized URL if new, else `None`. Mutates state |
| `.filter(urls)` | method | Accepted URLs, input order preserved |
| `.is_duplicate(url)` | method | Non-mutating check; does not count |
| `.reset()` | method | Clears seen URLs and counters |
| `.stats` / `.accepted` | property | `FilterStats` / accepted URLs, first-seen order |
| `__len__` / `__contains__` / `__iter__` / `__repr__` | dunder | Container protocol over distinct accepted URLs |

Not thread-safe by documented contract.

### Unit 3 — `confidence.py`

| Symbol | Kind | Contract |
|---|---|---|
| `MIN_SCORE` / `MAX_SCORE` | `float` | `0.0` / `1.0` |
| `Confidence` | frozen, ordered dataclass | Validated belief; immutable, hashable, comparable |
| `.score` | `float` | Always `float`, always within bounds |
| `.is_unknown` / `.is_certain` | property | Exactly `0.0` / exactly `1.0` |
| `.meets(threshold)` | method | `score >= threshold` |
| `.to_dict()` | method | `{"score": float}` |
| `.from_value(value)` | classmethod | Accepts `Confidence` (identity) or real number |
| `Confidence.CERTAIN` / `.UNKNOWN` | ClassVar sentinel | `1.0` / `0.0` |

Out-of-range input **raises** rather than clamping: a score of `1.7` means
the producer has a bug, and clamping would hide it behind a plausible value.

### Unit 4 — `evidence.py`

| Symbol | Kind | Contract |
|---|---|---|
| `ExtractedField` | `str` Enum | `COMPANY_NAME`, `PHONE`, `EMAIL`, `ADDRESS`, `LEADERSHIP`, `SOCIAL`, `SERVICES`. **Open** — any string is a valid field |
| `normalize_field(field)` | function | Canonical lowercase field name; the single comparison choke point |
| `FieldEvidence` | frozen dataclass | Provenance for **one** field |
| `.field` | `str` | Canonicalized on construction |
| `.page_url` | `str` | Required, canonicalized via `normalize_url`; non-web URLs rejected |
| `.selector` | `str` | Element locator; `""` when the method has none |
| `.method` | `str` | Free-form (`"tel_link"`, `"jsonld"`, `"regex"`) |
| `.confidence` | `Confidence` | Coerced via `Confidence.from_value`; defaults to `UNKNOWN` |
| `.snippet` / `.extracted_at` | `str` | Audit text / ISO-8601 timestamp; never auto-filled |
| `.to_dict()` / `.from_dict(data)` | method / classmethod | Round-trip serialization |
| `EvidenceSet` | class | Every observation for one company, grouped by field |
| `.add(e)` / `.extend(es)` | method | Record observations; rejects non-`FieldEvidence` |
| `.for_field(field)` | method | Observations in insertion order, as a tuple |
| `.has(field)` / `.fields` | method / property | Presence / fields in first-seen order |
| `.to_dict()` / `.from_dict(data)` | method / classmethod | Round-trip serialization |
| `__len__` / `__iter__` / `__contains__` / `__repr__` | dunder | Container protocol over observations |

`EvidenceSet` deliberately exposes **no** "best evidence" accessor.
Choosing which of three observed phone numbers is correct is extraction
policy and arrives with the extractors.

### Unit 5 — `extractors.py`

| Symbol | Kind | Contract |
|---|---|---|
| `PageContent` | runtime-checkable Protocol | A parsed page as an extractor sees it. 10 members mirroring `ParsedPage`: `url`, `title`, `description`, `emails`, `phones`, `social_links`, `text_content`, `links`, `h1_texts`, `meta_keywords` |
| `ExtractionResult` | frozen dataclass | One found value bound to its evidence |
| `.value` | `str` | Stripped, never empty |
| `.evidence` | `FieldEvidence` | Mandatory — a value without provenance is unrepresentable |
| `.attributes` | `Mapping[str, str]` | Per-field detail: `"title"` for leadership, `"platform"` for social |
| `.field_name` | property | Taken from the evidence, so the two can never disagree |
| `.to_dict()` | method | `{field, value, evidence, attributes}` |
| `FieldExtractor` | ABC | One field per extractor |
| `.field` | ClassVar | The `ExtractedField` produced |
| `.extract(page)` | abstractmethod | `list[ExtractionResult]`, never `None`; one result per occurrence; synchronous |
| `.extractor_name` | property | Defaults to the class name |
| `.describe()` | method | `{"name", "field"}` for diagnostics, mirroring `BaseDiscoveryPlugin.describe` |
| `CompanyNameExtractor`, `PhoneExtractor`, `EmailExtractor`, `AddressExtractor`, `LeadershipExtractor`, `SocialExtractor`, `ServicesExtractor` | abstract subclasses | Seven field contracts. Each fixes `.field` and implements nothing |
| `EXTRACTOR_INTERFACES` | `tuple` | All seven, in spec order, so Unit 8 iterates rather than hard-coding names |

`extract` is **synchronous**: the page is already fetched and parsed, the
remaining work is CPU-bound over in-memory data, and `HTMLParser.parse` is
itself synchronous. `async` would add coroutine overhead and buy nothing.

---

## Future Consumers

| Module | Unit | Expected consumers |
|---|---|---|
| `url_normalizer` | 1 | `url_filter` (built); Candidate Generator (Unit 6); Direct Website Discovery Plugin (Unit 8); `app/crawlers/http_crawler.py` if URL canonicalization is later centralized; company-record deduplication in `app/engines/source_connectors/deduplicator.py`, which currently reimplements host normalization |
| `url_filter` | 2 | Candidate Generator (Unit 6) — per-run candidate deduplication; Direct Website Discovery Plugin (Unit 8) — crawl-budget enforcement |
| `confidence` | 3 | `evidence` (built); every extractor interface (Unit 5); the scoring phase (2.3B+); ranking and validation stages |
| `evidence` | 4 | All seven extractor interfaces (Unit 5); Direct Website Discovery Plugin (Unit 8) — evidence attached to returned company dicts; company validation, which needs provenance to justify accept/reject; API responses surfacing why a field was believed |
| `extractors` | 5 | The seven concrete extractor implementations (Phase 2.3B); Direct Website Discovery Plugin (Unit 8), which iterates `EXTRACTOR_INTERFACES`; a future extractor registry; the scoring phase, which consumes `ExtractionResult` batches |

---

## Internal Dependencies

### Depends On

| Module | Depends on | Why |
|---|---|---|
| `url_normalizer` | stdlib `re`, `urllib.parse` | Pure string canonicalization |
| `url_filter` | `url_normalizer` | `canonical_key` is the dedup identity |
| `confidence` | stdlib `math`, `dataclasses` | Leaf value object |
| `evidence` | `url_normalizer`, `confidence` | Canonical `page_url`; per-field belief |
| `extractors` | `evidence` | `ExtractedField`, `FieldEvidence` |

Nothing in the package depends on `app.discovery.plugins`,
`app.discovery.sources`, `app.engines` or `app.crawlers`. The dependency
graph is a DAG with two roots (`url_normalizer`, `confidence`) and one
sink (`extractors`).

### Uses

| Consumed | From | How |
|---|---|---|
| `ParsedPage` shape | `app.crawlers.html_parser` | **Structurally only.** `PageContent` mirrors its members; no import, so no `aiohttp` |
| `PluginCapability` open-enum pattern | `app.discovery.plugins.base_plugin` | Pattern reuse — `ExtractedField` + `normalize_field` copy the str-enum-plus-choke-point design |
| `PluginConfig.options` pattern | `app.discovery.plugins.plugin_config` | Pattern reuse — `ExtractionResult.attributes` is free-form for the same reason |
| `describe()` convention | `BaseDiscoveryPlugin` | `FieldExtractor.describe()` matches, per the CLAUDE.md §6 logging standard |

No runtime import of any of the above. Every reuse above is of a *shape*
or *convention*, which is what keeps the package a leaf.

### Provides

| Provided | To |
|---|---|
| `normalize_url`, `canonical_key`, `extract_host` | Candidate Generator (Unit 6), Plugin (Unit 8) |
| `DuplicateURLFilter`, `FilterStats` | Candidate Generator (Unit 6) |
| `Confidence` | `evidence`, all extractors, the scoring phase |
| `ExtractedField`, `FieldEvidence`, `EvidenceSet` | All extractors, Plugin (Unit 8), validation, API output |
| `PageContent`, `ExtractionResult`, `FieldExtractor`, 7 interfaces, `EXTRACTOR_INTERFACES` | Phase 2.3B implementations, Plugin (Unit 8) |

Currently provided to **nothing** — no production code path imports this
package yet. That is intentional until Unit 8 registers a plugin.

---

## Known Limitations

| Limitation | Why it is acceptable now |
|---|---|
| No component performs discovery | Phase 2.3A is infrastructure by instruction. The package is inert until Unit 8 |
| `DuplicateURLFilter` is not thread-safe | Documented in its contract. Single-threaded per discovery run; concurrency arrives with the crawler, not before |
| `Confidence` has no combination rule | Deliberate. How two observations merge is a scoring decision with several defensible answers |
| `EvidenceSet` has no "best value" accessor | Deliberate. Selection policy belongs with the extractors |
| `PageContent` requires all 10 `ParsedPage` members | An extractor needing an 11th member forces a Protocol change and a parser change together — which is the honest coupling, not a hidden one |
| `ExtractionResult` is not hashable | `attributes` is a mapping. Results are compared and serialized, never used as dict keys |
| `ExtractionResult.attributes` is untyped free-form | Avoids seven bespoke result models before a single extractor exists. Conventions (`"title"`, `"platform"`) are documented, not enforced |
| `extracted_at` is never auto-filled | A model that stamps itself is not a model, and auto-timestamping makes tests non-deterministic |

### Current Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Spec item 1 arrived garbled (`DFbYeRs2NDmaT1pcTMNzQmtrvhURGhLmg3`) and was interpreted as the plugin skeleton | Medium | Flagged when received and not corrected since. Only Unit 8 is affected; every other unit is independent of the reading |
| `PageContent` conformance is structural, so a rename in `ParsedPage` breaks extractors silently at runtime rather than at import | Medium | `test_extractors.py` asserts the real `ParsedPage` satisfies the Protocol, converting a silent break into a test failure |
| That conformance test is `importorskip`-guarded, so it is skipped where `aiohttp`/`bs4` are absent | Low | Skips are visible in pytest output. CI with full dependencies exercises it |
| **Pre-existing defect, not introduced here:** `app/crawlers/__init__.py` line 56 lists `"RetriedEngine"` in `__all__`, but the imported name is `RetryEngine`. `from app.crawlers import *` raises `AttributeError`, and `RetryEngine` is unreachable via star-import | Low | Reported, not fixed — `app/crawlers/` is out of scope this phase. Recommend a one-line correction in a dedicated pass |
| 7 pre-existing `ruff` errors remain in `plugin_manager.py` (369, 378, 452, 457) and `source_orchestrator.py` (149, 155, 178) | Low | All Phase 2.1-or-earlier code, none on a line touched here. Recommend a dedicated lint pass |
| No `ruff` or `pytest` configuration is checked in | Medium | Lint and test invocation are not reproducible for a fresh clone; `python -m pytest` is required. Recommend adding `pyproject.toml` |

### Future Improvements

| Improvement | Phase |
|---|---|
| Implement the seven extractors against these interfaces | 2.3B |
| Scoring logic — where `Confidence` values actually come from | 2.3B |
| Evidence-aware company validation and ranking | 2.3B+ |
| Migrate `Deduplicator._extract_domain_key` onto `url_normalizer.extract_host`, removing duplicated host normalization | Cleanup |
| Fix the `RetriedEngine` typo in `app/crawlers/__init__.py` | Cleanup |
| Add `pyproject.toml` with `ruff` and `pytest` configuration | Cleanup |
| Clear the 7 pre-existing `ruff` errors | Cleanup |

---

## Reuse Proof

Per CLAUDE.md §14, each new module exists only because the repository was
searched first and no equivalent was found.

| Candidate for reuse | Location | Why it does not serve |
|---|---|---|
| `Evidence` | `app/engines/source_connectors/evidence.py` | Record-level provenance: one `source_url` and one `confidence` for an entire company. Website extraction needs **per-field** provenance — name and phone routinely come from different pages by different methods. Widening it would change a public model shipped connectors already depend on. Left untouched |
| `Deduplicator` | `app/engines/source_connectors/deduplicator.py` | Deduplicates **`CompanyResult` objects** by domain or name, after discovery. Unit 2 deduplicates **candidate URL strings**, before fetching. Different input type, different pipeline stage |
| `normalizer.py` | `app/engines/source_connectors/normalizer.py` | Normalizes company **names** (corporate suffixes). No URL handling |
| `HTMLParser` | `app/crawlers/html_parser.py` | Parses HTML. Phase 2.3A performs no parsing. Its `ParsedPage` output shape is reused structurally by Unit 5's `PageContent` Protocol — satisfied with no adapter, no subclassing, no modification |
| `PluginConfig` | `app/discovery/plugins/plugin_config.py` | Reused as-is in Unit 7. Its free-form `options` dict is the extension point; a typed view over it is planned, not a replacement |
| `BaseDiscoveryPlugin` | `app/discovery/plugins/base_plugin.py` | Reused as-is in Unit 8. Supplies `name`, `description`, `priority`, `enabled`, `capabilities`. Only `version` is absent |

**Pattern reuse.** `ExtractedField` follows the open-enum pattern
established by `PluginCapability` (str enum + a single `normalize_*` choke
point), so an unknown field behaves exactly like a declared one.

---

## Architecture Impact

**None yet, by design.** No production code path imports
`app.discovery.website`. The package is inert infrastructure until Unit 8
registers a plugin.

This satisfies CLAUDE.md §1 (no fixture involvement), §3/§8 (no search
provider is touched), and §4 (no provider dependency introduced). The
discovery pipeline is byte-for-byte unchanged.

---

## Assumptions

1. **Spec item 1 was garbled** in the source instruction
   (`DFbYeRs2NDmaT1pcTMNzQmtrvhURGhLmg3`). From its description —
   "registerable plugin, must implement the existing plugin contract, must
   return `(SourceStatus, companies, metadata)`" — it is interpreted as the
   **Direct Website Discovery Plugin skeleton** (Unit 8). Flagged at the
   time; not corrected since. If the intent differs, Unit 8 changes.
2. `extracted_at` is caller-supplied, never auto-stamped. A model that
   stamps itself is not a model, and auto-timestamping would make tests
   non-deterministic.
3. Field names are lowercase snake_case, matching `PluginCapability`.
4. Evidence always originates from a web page, so `page_url` is mandatory
   and must be a usable `http`/`https` URL.

---

## Backward Compatibility Proof

- No existing module imports `app.discovery.website`.
- No existing file has been modified outside the new package.
- No existing public signature has changed.
- No dependency has been added to `requirements`; the package uses the
  standard library only.
- Full suite before this phase: **329 passed**. The phase adds tests and
  removes none.

---

## Rollback Plan

Delete `backend/app/discovery/website/`, `backend/tests/discovery/website/`
and this report. Nothing else references them, so removal is complete and
requires no other edit.

Per CLAUDE.md §13, no git operation (reset, restore, checkout, revert,
clean) will be run without explicit approval.

---

## Remaining Units

| Unit | Component | Spec Item | Status |
|---|---|---|---|
| 6 | Candidate Generator Interface | 2 | Not started |
| 7 | Plugin Configuration | 5 | Not started |
| 8 | Plugin + Plugin Metadata | 1, 6 | Not started |

Phase 2.3B does **not** begin until Phase 2.3A is complete and approved.

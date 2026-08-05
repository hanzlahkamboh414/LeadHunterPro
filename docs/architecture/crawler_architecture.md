"""Crawler architecture documentation for LeadHunter Pro.

This document describes the Universal Crawler Infrastructure built in Sprint 2.2B.
"""

# ---------------------------------------------------------------------------
# Architecture Overview
# ---------------------------------------------------------------------------
#
# The crawler package provides reusable HTTP fetching, parsing, and
# compliance tools for all source connectors. It is a leaf dependency:
# crawlers/ depends on nothing above it in the dependency tree.
#
# Component Dependency Graph:
#
#   HTTPCrawler (orchestrator)
#   ├── SessionManager      (aiohttp session lifecycle)
#   ├── RateLimiter         (per-host + global rate limiting)
#   ├── RetryEngine         (exponential backoff retry)
#   ├── ResponseCache       (TTL-based in-memory cache)
#   ├── RobotsManager       (robots.txt compliance)
#   ├── HTMLParser          (BeautifulSoup extraction)
#   └── CrawlerConfig       (centralized configuration)
#
# All components are independently testable. No component imports from
# engines/, connectors/, or api/.
#
# ---------------------------------------------------------------------------
# Component Specifications
# ---------------------------------------------------------------------------

## 1. CrawlRequest

Immutable dataclass controlling each crawl operation.

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | `str` | *(required)* | Full URL to fetch |
| `method` | `str` | `"GET"` | HTTP method |
| `headers` | `dict[str, str]` | `{}` | Extra headers |
| `timeout` | `int` | `30` | Request timeout in seconds |
| `follow_redirects` | `bool` | `True` | Follow HTTP redirects |
| `respect_robots` | `bool` | `True` | Check robots.txt before fetch |
| `session_id` | `str \| None` | `None` | Optional session identifier |

## 2. CrawlResponse

Immutable dataclass returned by every crawl operation.

| Field | Type | Description |
|---|---|---|
| `url` | `str` | Final URL after redirects |
| `status_code` | `int` | HTTP status code |
| `content` | `bytes` | Raw response body |
| `headers` | `dict[str, str]` | Response headers |
| `successful` | `bool` | True if 2xx status |
| `robots_compliant` | `bool` | True if allowed by robots.txt |
| `cached` | `bool` | True if served from cache |
| `error` | `str` | Error message if failed |
| `response_time_ms` | `float` | Wall-clock time in milliseconds |

Computed property:
- `text` — UTF-8 decoded string (falls back to latin-1 on decode error)

## 3. CrawlerConfig

Frozen dataclass with all tuning parameters. No magic numbers.

| Parameter | Default | Description |
|---|---|---|
| `default_timeout` | `30` | HTTP request timeout (seconds) |
| `max_retries` | `3` | Maximum retry attempts |
| `retry_backoff_base` | `2.0` | Exponential backoff base (seconds) |
| `retry_backoff_max` | `30.0` | Maximum backoff cap (seconds) |
| `rate_limit_per_host_seconds` | `0.5` | Min interval between requests to same host |
| `rate_limit_global_seconds` | `0.1` | Min interval between any requests |
| `user_agent_pool` | 3 UAs | Rotating User-Agent strings |
| `respect_robots_txt` | `True` | Enforce robots.txt rules |
| `robots_cache_ttl` | `3600` | robots.txt cache TTL (seconds) |
| `session_timeout` | `35` | Internal aiohttp client timeout |

## 4. ResponseCache

TTL-based in-memory cache with Redis-ready interface.

**Cache layers:**

| Layer | Key | TTL | Purpose |
|---|---|---|---|
| robots.txt | `host` | 1 hour | Avoid re-parsing robots.txt |
| HTTP response | `url` | 10 minutes | Avoid re-fetching same page |
| Domain health | `domain` | 24 hours | Avoid repeated HEAD checks |

**Interface:**
```python
cache = ResponseCache(default_ttl=600)
cache.set("key", value, ttl_seconds=600)
result = cache.get("key")           # Returns None if missing/expired
cache.delete("key")                  # Remove specific key
cache.clear()                        # Remove all entries
removed = cache.cleanup()            # Remove expired entries
rate = cache.hit_rate                # Cache hit percentage
size = cache.size                    # Non-expired entry count
```

## 5. RobotsManager

RFC 9309-compliant robots.txt checker.

**Behavior:**
- Fetches `https://host/robots.txt` on first request per host
- Caches parsed rules for 1 hour
- Supports user-agent-specific rules
- Allow rules take precedence over disallow rules
- Missing/malformed robots.txt → allow by default
- Fetch failure → allow by default (never block on error)

**Path matching:**
- Exact match: `/admin` matches `/admin` and `/admin/settings`
- Wildcard: `/admin/*` matches `/admin/page`
- Empty disallow → allows all paths

## 6. RateLimiter

Token-bucket algorithm with per-host and global limits.

**Algorithm:**
```
Each acquire() call:
  1. Wait until (last_request_to_host + per_host_delay) ≤ now
  2. Wait until (last_global_request + global_delay) ≤ now
  3. Update both timestamps
```

**Thread safety:** Uses `time.monotonic()` for accurate measurements.
Blocking sleep is used (not async yield) to maintain simple semantics.

## 7. RetryEngine

Exponential backoff retry with configurable parameters.

**Retry logic:**
```
Attempt 1: Immediate
Attempt 2: Backoff base^1 seconds
Attempt 3: Backoff base^2 seconds
Attempt 4: Backoff min(base^3, max_cap) seconds
```

**Retries on:** `ConnectionError`, `TimeoutError`, `OSError`, 5xx responses
**Does NOT retry:** 4xx client errors, non-matching exceptions

Decorator variant (`@connector_retry`):
```python
@connector_retry(max_retries=3, backoff_base=2.0)
async def fetch_page(url: str) -> str:
    ...
```

## 8. SessionManager

aiohttp ClientSession lifecycle management.

**Features:**
- Single session per instance (connection pooling)
- Automatic recreation after close
- Async context manager support
- No explicit session creation required

## 9. HTMLParser

BeautifulSoup-based extraction utilities.

**Extracted fields:**
- `title` — Page title from `<title>` tag
- `description` — Meta description content
- `meta_keywords` — Meta keywords content
- `emails` — Email addresses (regex + mailto: links)
- `phones` — Phone numbers (regex, validated as all digits)
- `social_links` — Social media profile URLs
- `links` — All absolute URLs (excluding anchors and javascript)
- `h1_texts` — H1 heading texts
- `text_content` — Normalized plain text

**Parser engines:** `html.parser` (default), `lxml` (optional)

## 10. HTTPCrawler

Main orchestrator combining all components.

**Request flow:**
```
1. Check robots.txt → raise CrawlerRobotsBlocked if denied
2. Acquire rate limiter permit
3. Check cache → return cached response if hit
4. Fetch via SessionManager + RetryEngine
5. Parse with HTMLParser (connector decides what to extract)
6. Cache successful responses
7. Return CrawlResponse
```

**Error handling:**
- Transient failures (connection, timeout, 5xx) → automatic retry
- Permanent failures (4xx, robots blocked) → logged and raised
- All exceptions caught at connector boundary → metadata returned

**Configuration:**
- Per-connector timeout (default 30s)
- Per-host rate limiting (default 0.5s interval)
- Global rate limiting (default 0.1s interval)
- User-Agent rotation (3 agents in pool)

# ---------------------------------------------------------------------------
# Lifecycle Management
# ---------------------------------------------------------------------------

## Crawler Lifecycle

```python
# Creation
crawler = HTTPCrawler(config=CrawlerConfig(...))

# Usage (context manager recommended)
async with crawler as c:
    response = await c.crawl(CrawlRequest(url="https://example.com"))
    if response.successful:
        parsed = c.parser.parse(response.text, response.url)

# Cleanup
await crawler.close()  # Releases aiohttp session
```

## Cache Lifecycle

- Entries expire automatically based on TTL
- `cleanup()` removes expired entries on demand
- No persistence between process restarts (in-memory only)
- Redis replacement requires only swapping the backend implementation

## Session Lifecycle

- Session created on first access
- Session reused across requests (connection pooling)
- Session closed on `crawler.close()` or `SessionManager.close()`
- New session auto-created if accessed after close

# ---------------------------------------------------------------------------
# Exception Hierarchy
# ---------------------------------------------------------------------------

```
CrawlerError (base)
├── CrawlerTimeout        — Request exceeded timeout
├── CrawlerHTTPError      — HTTP error (4xx/5xx)
│   ├── status_code: int
│   └── url: str
├── CrawlerRetryExceeded  — All retries exhausted
│   ├── url: str
│   └── last_error: Exception
└── CrawlerRobotsBlocked  — robots.txt denied access
    ├── url: str
    └── reason: str
```

All exceptions inherit from `CrawlerError`, enabling broad catching:
```python
try:
    response = await crawler.crawl(request)
except CrawlerError as exc:
    logger.warning("Crawl failed: %s", exc)
```

# ---------------------------------------------------------------------------
# Testing Strategy
# ---------------------------------------------------------------------------

All external dependencies are mocked:
- HTTP requests → `unittest.mock.patch` on `aiohttp.ClientSession`
- Cache → In-memory `ResponseCache` instance
- Rate limiter → Tested with real timing (no mocking needed)
- Robots.txt → Mocked `is_allowed()` method

Test coverage targets:
- Core components: 90%+
- Exception hierarchy: 95%+
- Integration (HTTPCrawler): 85%+

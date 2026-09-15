"""Application configuration using Pydantic Settings.

This module defines the application settings loaded from environment
variables and/or a `.env` file. All settings are typed and validated
at startup.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

# Every credential field below is declared with ``repr=False``. This is a real
# leak fix, not tidiness. Pydantic's generated ``__repr__`` prints EVERY field
# value, and pytest embeds that repr into an AttributeError message -- so on
# 2026-08-19 a single unrelated test failure splattered the live AI and Tavily
# keys across the terminal dozens of times, and from there into a pasted chat
# log. The leak path is any accidental repr of ``settings``: an exception, a
# logged object, a debugger frame. Excluding the field from the repr closes that
# whole class of accident at the source, while normal reads
# (``settings.AI_API_KEY``) keep working unchanged.
#
# Deliberately NOT using ``SecretStr``: it is stronger (masks even a direct
# print) but forces every read site to call ``.get_secret_value()``. Worth
# revisiting if a credential ever needs to survive being logged deliberately.
#
# Each field gets its OWN ``Field(...)`` call rather than a shared module-level
# instance: pydantic attaches per-field metadata to the FieldInfo object, so
# reusing one instance across fields is not safe.


class Settings(BaseSettings):
    """Top-level application settings."""

    APP_NAME: str = "LeadHunter Pro AI"
    APP_VERSION: str = "0.1.0"

    # Accuracy-first execution mode (CLAUDE.md §1):
    #   live — real sources only, fixtures COMPLETELY excluded
    #   demo — fixtures allowed and clearly labeled as bridge data
    #   test — fixtures allowed (automated tests / offline dev)
    # Default is live; a run may never silently fall back to fixtures.
    LEADHUNTER_ENV: Literal["live", "demo", "test"] = "live"

    DATABASE_URL: str

    API_PREFIX: str = "/api"

    LOG_LEVEL: str = "INFO"

    # ------------------------------------------------------------------
    # AI transport — vendor-neutral by design.
    #
    # These four settings are the ONLY place an AI vendor is named, and
    # backend/.env is the only file that changes to switch vendor. Nothing under
    # app/ mentions a vendor at all: RouterProvider speaks the OpenAI-compatible
    # protocol that every major vendor and router exposes, so OpenAI /
    # NaraRouter / OmniRoute / OpenRouter / Groq / Ollama are each reachable
    # through the same three values (CLAUDE.md sections 4 and 8 — providers are
    # replaceable infrastructure, never the architecture).
    #
    # AI_PROVIDER selects a registered provider class by name; "router" is the
    # generic OpenAI-compatible one. An unregistered name raises at startup
    # instead of degrading into a stub (CLAUDE.md section 12).
    # ------------------------------------------------------------------
    AI_PROVIDER: str = "router"

    # Defaults describe the transport proven live (agnes-2.0-flash on 2026-08-19;
    # qwen3.8-27b on 2026-09-05; agnes-2.5-flash confirmed 200 + 6.1s on the
    # same realistic prompt vs qwen27b's 17.9s that same day, so flash became
    # the lift default because the 27B pace makes a 100-lead run impractical).
    # qwen3.8-flash/-max/-alibaba and qwen3.7-flash were all 402/429 out of
    # credit at the time; qwen3.8-27b stays the quality fallback.
    # 2026-09-15 per-lane split (user directive): the MAIN pipeline (screening,
    # refine, person, intent, scoring) is routine research and stays on the
    # fast agnes-2.5-flash (3-7s/call); the deep-thinking lanes moved to
    # AI_MODEL_DEEP below. agnes-3-flash ran the whole pipeline for a few
    # hours that day — clean JSON, but 25-40s/call made every lead ~4-5x
    # slower, which is why it lives on the deep lane only.
    # The real key is loaded at runtime from the gitignored backend/.env;
    # never hardcode one.
    AI_BASE_URL: str = "https://router.bynara.id/v1"
    AI_MODEL: str = "agnes-2.5-flash"
    AI_API_KEY: str = Field(default="", repr=False)  # secret — see note above
    # Second AI key for the deep-research stage. Lets one key carry the main
    # pipeline (screening/refine/person/intent/scoring) while a second key
    # carries the deep-dive growth research, so they do not throttle each other.
    # Optional: falls back to AI_API_KEY when unset.
    AI_API_KEY_2: str = Field(default="", repr=False)  # secret

    # Model for the DEEP lanes — every caller of ``make_ai_ask()`` (the
    # deep-research stage, discovery query expansion, Phase H template
    # generation, source-scout proposals/probation). These stages do the deep
    # thinking and parse structured JSON, so they run agnes-3-flash: verified
    # 200 and returns CLEAN JSON (no markdown fences — 2.5 wraps output in
    # ```json) at 25-40s/call. That latency is acceptable here because the
    # deep lanes are low-volume and never block a lead's main pipeline stages.
    # Falls back to AI_MODEL when unset, so an old .env keeps working.
    AI_MODEL_DEEP: str = "agnes-3-flash"

    # Third AI key for the discovery dork/TEMPLATE-GENERATION lane (Phase H,
    # wired into live runs by Sprint2.11). A separate key lets the 3rd AI
    # propose new search angles while the main and deep lanes run, without
    # sharing their rate limit. Falls back to AI_API_KEY_2, then AI_API_KEY,
    # when unset — so an old .env without it keeps working unchanged.
    AI_API_KEY_3: str = Field(default="", repr=False)  # secret

    # Hard per-request cap for the AI transport. Without it the OpenAI SDK's
    # default is 600s (10 min): a slow router window or a congested model host
    # makes EVERY LLM call block ~10 min, and each lead makes 3 calls (company,
    # person, intent) — the measured cause of 7-19 min per lead. A bounded
    # timeout turns that into a fast per-stage failure (stage try/except
    # already records it and continues) so a 100-lead run can never crawl.
    AI_TIMEOUT_S: float = 60

    # Per-run lead research concurrency. Each lead makes 3+ LLM calls plus
    # searches and website crawls; processing leads serially means a 100-lead
    # run is the sum of every lead's latency. A small bounded thread pool
    # overlaps the network-bound research stages so a 100-lead run finishes
    # ~3x faster wall-clock without hammering the router or the SQLite store
    # (writes are serialized under a lock inside run_research). 1 = serial.
    LEADS_CONCURRENCY: int = 3

    # Search Provider Configuration
    SEARXNG_URL: str = ""
    # Per-request aiohttp cap for SearXNG. The instance itself caps its
    # engine fan-out at ~3s (deploy/searxng/settings.yml outgoing.request_timeout),
    # so 10s is 7s of headroom: a slow-but-healthy instance completes instead
    # of racing the cap — a single request timeout trips the circuit breaker
    # and opens a 5-minute Tavily blackout (measured under 2-user load).
    SEARXNG_TIMEOUT: int = 10
    BRAVE_SEARCH_API_KEY: str = Field(default="", repr=False)  # secret
    TAVILY_SEARCH_API_KEY: str = Field(default="", repr=False)  # secret

    # ------------------------------------------------------------------
    # Persistent search cache (app/search_providers/cache.py).
    #
    # Root cause of the recurring provider quota exhaustion: a lead costs
    # 15-20 provider calls and NOTHING was cached, so a re-run, a top-up
    # round, or a second lead at the same company paid for byte-identical
    # queries again. The cache is disk-backed (credits burn across process
    # restarts, so an in-memory cache cannot fix it) and lives at the
    # RegistryIndexedSearch seam, so it is provider-agnostic (§3/§4).
    #
    # Only NON-EMPTY answers are cached: an empty result at that layer may be
    # a provider error/quota rejection, and freezing that in would be a
    # silent fake negative (§1/§12).
    # ------------------------------------------------------------------
    SEARCH_CACHE_ENABLED: bool = True
    SEARCH_CACHE_TTL_DAYS: int = 14
    SEARCH_CACHE_EXTRACT_TTL_DAYS: int = 30
    # Empty = backend/output/search_cache.db (a dedicated file, so deleting the
    # cache can never risk a dossier).
    SEARCH_CACHE_DB: str = ""

    # Homepage-first client-fit pre-verdict (Phase 3 demand fix). 61% of
    # leads burned the 4 screening search queries BEFORE the fit verdict
    # said "not our client". The pre-verdict crawls the company's own site
    # (direct HTTP — zero search-engine load) and skips the searches only
    # on a GROUNDED AI "no"; "unsure" always falls through to full
    # research, so this can never cause a wrong skip. Kill-switch if the
    # verdict quality ever regresses.
    HOMEPAGE_PRE_VERDICT: bool = True

    # Phase 4 load management — bounded CONCURRENT pipelines. Every submitted
    # job used to spawn its worker immediately, so 10 simultaneous users meant
    # 10 streaming pipelines (10 discovery producers + 30 research consumer
    # threads) all at once: aligned search bursts outran the 5 qps global
    # SearXNG throttle's 30s queue budget (the measured breaker-trip → Tavily
    # blackout failure mode), and the AI router saw 30+ concurrent calls.
    # Admission control bounds the ACTIVE pipelines; extra jobs stay honestly
    # `queued` (FIFO) and auto-start the moment a slot frees. 4 slots ≈ the
    # throttle's drain capacity (each active job demands ~1 qps post-pre-
    # verdict), so searches stay under budget. 0 = unlimited (old behavior).
    MAX_ACTIVE_JOBS: int = 4

    # P6 background harvester — the 24/7 stocker (big-bang plan). One
    # daemon pass every HARVESTER_INTERVAL_S under admission control (it
    # only works when user searches leave pipeline budget spare): the
    # phones lane stocks trade×state pools with free license-board SODA
    # fetches, the emails lane runs harvest-time AI research (SHARED
    # dossiers, user_id='') — but ONLY for pairs users have actually
    # searched, so the AI lane never spends without a demand signal. Daily
    # quotas are the big-bang numbers: 5,000 stocked phone rows and 2,000
    # researched email leads per day. HARVESTER_ENABLED=false is the dev
    # kill-switch (set in the local .env so a dev machine never spends).
    HARVESTER_ENABLED: bool = True
    HARVESTER_INTERVAL_S: float = 300.0
    HARVEST_PHONE_BATCH: int = 250
    HARVEST_EMAIL_BATCH: int = 25
    HARVEST_PAIR_COOLDOWN_S: float = 21600.0  # one pair every 6h max
    HARVEST_MIN_POOL_FLOOR: int = 100         # per trade×state pool target
    HARVEST_STALENESS_DAYS: int = 30
    # Wall-clock budget for ONE emails-lane pass (soft stop via run_full's
    # cancel seam, checked between passes and between researched leads).
    # Live evidence 2026-09-14: an electrician/TX discovery loop ground on
    # for 2.5 HOURS — no budget meant a blocked harvester thread and hours
    # of discovery data accumulating in RAM (the OOM curve's slow half).
    # 45 min keeps a pass a pass; whatever is stocked by then is banked and
    # the next pass resumes with fresh cooldown — the demand-gated lane
    # never loses a pair, it just splits the work.
    HARVEST_EMAIL_BUDGET_S: float = 2700.0
    DAILY_PHONE_QUOTA: int = 5000
    DAILY_EMAIL_QUOTA: int = 2000

    # Optional lightweight auth for the Leads API (M12 baseline). When set,
    # requests must carry `X-API-Key: <key>`. When empty, the Leads API is
    # open (localhost/dev). Full user auth is a later phase.
    LEADS_API_KEY: str = Field(default="", repr=False)  # secret

    # ------------------------------------------------------------------
    # Auth (JWT-based user authentication).
    # ------------------------------------------------------------------
    AUTH_SECRET_KEY: str = Field(
        default="leadhunter-dev-secret-change-in-production",
        repr=False,  # secret — production must override via .env
    )
    AUTH_TOKEN_EXPIRE_HOURS: int = 24

    # ------------------------------------------------------------------
    # Gmail OAuth (Phase E2 — email outreach).
    #
    # Credentials come from the operator's OWN Google Cloud project (see
    # docs/email_oauth_setup.md): OAuth consent screen (External, Testing is
    # fine for private use) + an OAuth Client ID of type "Web application"
    # whose redirect URI is {PUBLIC_BASE_URL}/api/v1/email-accounts/google/callback.
    # While these are empty the Connect Gmail flow answers 503 with a clear
    # setup pointer — never a silent failure.
    # ------------------------------------------------------------------
    GOOGLE_CLIENT_ID: str = Field(default="", repr=False)  # secret
    GOOGLE_CLIENT_SECRET: str = Field(default="", repr=False)  # secret
    #: The public origin the OAuth redirect URI is built from (no trailing /).
    PUBLIC_BASE_URL: str = "https://leadhuntarpro.online"
    #: Optional dedicated Fernet key for token encryption at rest. Empty =
    #: derive one from AUTH_SECRET_KEY (documented: rotating AUTH_SECRET_KEY
    #: makes stored OAuth tokens unreadable — set EMAIL_TOKEN_KEY to decouple).
    EMAIL_TOKEN_KEY: str = Field(default="", repr=False)  # secret

    # ------------------------------------------------------------------
    # Pending-lead re-enrichment cooldown.
    #
    # Root cause it fixes: a lead whose AI research raises (transient AI/router
    # error, a pathological domain, a provider timeout) is NOT saved to
    # dossiers and NOT removed from the discovery cache — so the VERY NEXT
    # run serves it again, researches it again, fails again. A stubborn lead
    # could burn the same full research cost on EVERY Execute click with no
    # progress (the user's recurring "credits khatam + same leads repeat").
    #
    # Fix: PendingLeadsStore records attempted_at/attempt_count on failure,
    # and take() skips rows re-attempted within this window. The lead stays in
    # the cache (a real retry after the cooldown is exactly what we want) but
    # a broken lead is never re-crawled every button click. 0 = disabled.
    # ------------------------------------------------------------------
    LEAD_REENRICHMENT_COOLDOWN_SECONDS: int = 86400  # 24h

    model_config = SettingsConfigDict(
        env_file=_ENV_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

# Env-only snapshot of the managed secret keys, taken BEFORE the runtime overlay
# is merged — this is the fallback the HOT-RELOAD path (admin PUT /keys) reverts
# a key to when its overlay entry is cleared, so "clear" means back to .env,
# matching what a restart would produce.
try:
    from app.core.runtime_keys import KNOWN_KEY_NAMES

    _ENV_KEY_VALUES: dict[str, str] = {
        name: getattr(settings, name, "") for name in KNOWN_KEY_NAMES
    }
except Exception:  # noqa: BLE001 — the snapshot is an optimization, never fatal
    _ENV_KEY_VALUES = {}

# Runtime key overlay (admin API-key management). A key the admin screen sets
# in backend/output/runtime_keys.json overrides .env for this process WITHOUT
# touching .env and WITHOUT ever echoing a secret — merged here, before any
# provider imports, so every registration/read sees the overlay transparently.
# Changes made at RUNTIME are applied live (RuntimeKeyStore.apply_live +
# search-provider re-registration in the admin endpoint) — no restart needed.
try:
    from app.core.runtime_keys import RuntimeKeyStore

    settings = RuntimeKeyStore().apply(settings)
except Exception:  # noqa: BLE001 — a broken overlay must never block startup
    pass
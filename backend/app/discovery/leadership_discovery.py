import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.discovery.people_parser import PeopleParser, PersonRecord
from app.discovery.result_cleaner import ResultCleaner
from app.email.domain_verifier import verify_email_domains

logger = logging.getLogger(__name__)

# Statuses meaning "the server understood us and refused" — a client-identity
# problem (bot filtering, WAF, rate limit, overload), not a missing page. 5xx is
# not listed here but is counted as blocked by
# :attr:`LeadershipScanStats.blocked_pages`, because a site that answers 500 on
# EVERY path is refusing this client, not coincidentally broken nine times.
_BLOCKED_STATUSES: frozenset[int] = frozenset({401, 402, 403, 405, 406, 407, 429})

# Statuses meaning "this path does not exist here" — a path-coverage problem,
# fixable by widening CANDIDATE_PAGES or by following the site's own nav.
_MISSING_STATUSES: frozenset[int] = frozenset({404, 410})


@dataclass
class LeadershipScanStats:
    """Per-website diagnostics for one leadership scan (roadmap D20).

    Exists because the scan previously failed in total silence: non-200 responses
    were dropped by a bare ``continue`` and transport errors were logged at DEBUG,
    while logging is unconfigured, so nothing reached the terminal. The 2026-08-19
    run reported "no named decision-maker" for 3 of 5 companies with no way to
    tell whether the pages were missing, blocked, or parsed badly — three problems
    with three different fixes. These counters separate them.

    Attributes:
        website: The site that was scanned.
        pages_attempted: Candidate paths tried.
        pages_ok: Paths that returned HTTP 200.
        status_counts: HTTP status code -> number of paths that returned it.
        error_counts: Exception class name -> number of paths that raised it.
        candidates: Person-like records the parser produced, before dedup.
        people: Unique decision-makers returned, after dedup and merge.
    """

    website: str
    pages_attempted: int = 0
    pages_ok: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)
    error_counts: dict[str, int] = field(default_factory=dict)
    candidates: int = 0
    people: int = 0

    @property
    def blocked_pages(self) -> int:
        """Paths the server refused (see :data:`_BLOCKED_STATUSES` and 5xx)."""
        return sum(
            count
            for status, count in self.status_counts.items()
            if status in _BLOCKED_STATUSES or status >= 500
        )

    @property
    def missing_pages(self) -> int:
        """Paths the server reported as non-existent."""
        return sum(
            count
            for status, count in self.status_counts.items()
            if status in _MISSING_STATUSES
        )

    @property
    def verdict(self) -> str:
        """Name the most likely cause of this scan's outcome.

        Deliberately a *diagnosis*, not a fix: it says which of the three failure
        modes the numbers point at, so the next change is chosen from evidence
        instead of guessed at (CLAUDE.md §7).

        Returns:
            A short lowercase phrase, e.g. ``"blocked"`` or ``"parser"``, with a
            one-clause explanation.
        """
        if self.people:
            return "ok"
        if self.pages_ok and not self.candidates:
            return (
                f"parser: {self.pages_ok} page(s) fetched, "
                "no person-like block matched"
            )
        if self.candidates and not self.people:
            return f"filtered: {self.candidates} candidate(s) dropped after parsing"
        if self.blocked_pages:
            return (
                f"blocked: server refused this client on "
                f"{self.blocked_pages}/{self.pages_attempted} page(s)"
            )
        if self.error_counts and not self.pages_ok:
            return f"transport: no page reachable ({self.error_counts})"
        if self.missing_pages:
            return (
                f"missing: {self.missing_pages}/{self.pages_attempted} path(s) absent "
                "— widen candidate paths or follow the site's own nav"
            )
        return "unknown: no status, no error, no candidate recorded"

    def as_dict(self) -> dict:
        """Return the counters as a plain dict for reports and assertions.

        Returns:
            A JSON-serialisable snapshot including the derived verdict.
        """
        return {
            "website": self.website,
            "pages_attempted": self.pages_attempted,
            "pages_ok": self.pages_ok,
            "status_counts": dict(self.status_counts),
            "error_counts": dict(self.error_counts),
            "candidates": self.candidates,
            "people": self.people,
            "verdict": self.verdict,
        }


class LeadershipDiscovery:

    CANDIDATE_PAGES = (
        "/",
        "/about",
        "/about-us",
        "/team",
        "/our-team",
        "/leadership",
        "/management",
        "/company",
        "/contact",
    )

    def __init__(self):

        self.people_parser = PeopleParser()
        self.cleaner = ResultCleaner()
        # Diagnostics from the most recent discover() call. Kept on the instance
        # so a caller (run_leads, a test, a future report) can read WHY a scan
        # found nobody without re-running it. None until the first scan.
        self.last_scan: LeadershipScanStats | None = None

    def discover(
        self,
        website: str,
    ):

        leaders: dict[tuple[str, str], PersonRecord] = {}
        stats = LeadershipScanStats(website=website)

        for page in self.CANDIDATE_PAGES:

            url = urljoin(
                website,
                page,
            )
            stats.pages_attempted += 1

            try:

                response = requests.get(
                    url,
                    timeout=10,
                    headers={"User-Agent": "Mozilla/5.0"},
                    allow_redirects=True,
                )

                status = response.status_code
                stats.status_counts[status] = stats.status_counts.get(status, 0) + 1

                if status != 200:
                    continue

                stats.pages_ok += 1

                soup = BeautifulSoup(
                    response.text,
                    "html.parser",
                )

                records = self.people_parser.extract_candidates(
                    soup,
                    page_url=url,
                )
                stats.candidates += len(records)

                for record in records:
                    record.emails = verify_email_domains(record.emails)
                    # The same person appears on several candidate pages
                    # (/, /about, /team, ...). Keep ONE decision-maker and
                    # MERGE its emails, so the contact-page address is not
                    # dropped just because the homepage saw the name first.
                    key = (record.person.name.lower(), record.person.role.lower())
                    prev = leaders.get(key)
                    if prev is None:
                        leaders[key] = record
                    else:
                        existing = {email.email for email in prev.emails}
                        prev.emails.extend(
                            email
                            for email in record.emails
                            if email.email not in existing
                        )

            except requests.RequestException as exc:
                # Transport/HTTP failure on ONE candidate path — skip it and
                # keep sweeping (404/5xx/timeouts must never kill discovery).
                # Any other exception (parser bug, ...) is a REAL defect and
                # is allowed to surface, never silently swallowed (§7).
                name = type(exc).__name__
                stats.error_counts[name] = stats.error_counts.get(name, 0) + 1
                logger.debug("Skipping leader scan of %s: %s", url, name)
                continue

        stats.people = len(leaders)
        self.last_scan = stats

        # A scan that returns nobody is the exact failure blocking qualification,
        # so it is reported at WARNING: logging is currently unconfigured, and
        # Python's last-resort handler only emits WARNING and above, so an INFO
        # line here would be invisible in precisely the case that matters.
        # Successes stay at INFO — visible once logging is configured, quiet now.
        log = logger.warning if stats.people == 0 else logger.info
        log(
            "LeadershipDiscovery %s: pages=%d ok=%d statuses=%s errors=%s "
            "candidates=%d people=%d -> %s",
            website,
            stats.pages_attempted,
            stats.pages_ok,
            stats.status_counts or "{}",
            stats.error_counts or "{}",
            stats.candidates,
            stats.people,
            stats.verdict,
        )

        return [record.to_dict() for record in leaders.values()]

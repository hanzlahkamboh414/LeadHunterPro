"""Email discovery — harvest emails from a company website.

Extraction reuses the canonical format pattern and the existing format
validators in ``app.email`` (rule #14 — no new validation logic), so only
plausible, deduplicated, lowercased addresses leave this layer.
"""

from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.email.email_cleaner import EMAIL_CLEAN_PATTERN, clean_emails
from app.email.email_validator import is_valid_email


class EmailDiscovery:

    CANDIDATE_PAGES = [
        "/",
        "/contact",
        "/contact-us",
        "/about",
        "/about-us",
    ]

    def discover(
        self,
        website: str,
    ) -> dict:

        emails = set()

        headers = {
            "User-Agent": "Mozilla/5.0"
        }

        for page in self.CANDIDATE_PAGES:

            url = urljoin(
                website,
                page,
            )

            try:

                response = requests.get(
                    url,
                    timeout=10,
                    headers=headers,
                )

                if response.status_code != 200:
                    continue

                soup = BeautifulSoup(
                    response.text,
                    "html.parser",
                )

                html = response.text

                # Regex Emails (canonical pattern from app/email/email_cleaner)
                found = EMAIL_CLEAN_PATTERN.findall(html)

                for email in found:
                    emails.add(email.lower())

                # mailto: — reject malformed single addresses (format tier)
                for a in soup.find_all(
                    "a",
                    href=True,
                ):

                    href = a.get("href")

                    if href.startswith("mailto:"):

                        email = (
                            href.replace(
                                "mailto:",
                                "",
                            )
                            .split("?")[0]
                            .strip()
                            .lower()
                        )

                        if is_valid_email(email):
                            emails.add(email)

            except Exception:
                continue

        # Batch format check + dedupe + lowercase (format tier)
        cleaned = clean_emails(sorted(emails))

        return {
            "emails": cleaned,
            "count": len(cleaned),
        }
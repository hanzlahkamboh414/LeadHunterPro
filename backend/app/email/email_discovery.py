import re

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin


class EmailDiscovery:

    CANDIDATE_PAGES = [
        "/",
        "/contact",
        "/contact-us",
        "/about",
        "/about-us",
    ]

    EMAIL_PATTERN = re.compile(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    )

    def discover(
        self,
        website: str,
    ):

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

                # Regex Emails
                found = self.EMAIL_PATTERN.findall(html)

                for email in found:
                    emails.add(email.lower())

                # mailto:
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

                        emails.add(email)

            except Exception:
                continue

        return {
            "emails": sorted(emails),
            "count": len(emails),
        }
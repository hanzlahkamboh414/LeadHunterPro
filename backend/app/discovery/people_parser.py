import re

from bs4 import BeautifulSoup


class PeopleParser:

    TITLES = [
        "Chief Executive Officer",
        "CEO",
        "Founder",
        "Co-Founder",
        "Owner",
        "President",
        "Managing Director",
        "Director",
        "Principal",
        "Partner",
        "Vice President",
        "COO",
        "CTO",
        "CFO",
    ]

    NAME_PATTERN = re.compile(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b"
    )

    def extract_candidates(
        self,
        soup: BeautifulSoup,
    ):

        candidates = []

        tags = soup.find_all(
            [
                "section",
                "article",
                "div",
                "li",
            ]
        )

        for tag in tags:

            text = tag.get_text(
                " ",
                strip=True,
            )

            if len(text) < 20:
                continue

            for title in self.TITLES:

                if title.lower() not in text.lower():
                    continue

                name = ""

                match = self.NAME_PATTERN.search(text)

                if match:
                    name = match.group(1)

                candidates.append(
                    {
                        "name": name,
                        "title": title,
                        "text": text,
                    }
                )

                break

        return candidates
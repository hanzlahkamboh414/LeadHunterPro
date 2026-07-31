from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


class WebsiteCrawler:

    COMMON_PAGES = [
        "",
        "about",
        "contact",
        "services",
        "team",
        "leadership",
        "projects",
        "portfolio",
        "careers",
    ]

    def crawl(self, base_url: str):

        discovered = []

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "LeadHunterPro/1.0"
            )
        }

        for page in self.COMMON_PAGES:

            url = urljoin(base_url + "/", page)

            try:

                response = requests.get(
                    url,
                    headers=headers,
                    timeout=15,
                )

                if response.status_code == 200:

                    soup = BeautifulSoup(
                        response.text,
                        "html.parser",
                    )

                    discovered.append(
                        {
                            "url": url,
                            "title": soup.title.get_text(strip=True)
                            if soup.title else "",
                        }
                    )

            except Exception:
                pass

        return discovered
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

class WebsiteParser:

    def parse(self, html: str, base_url: str):

        soup = BeautifulSoup(html, "html.parser")

        title = ""
        description = ""

        if soup.title:
            title = soup.title.get_text(strip=True)

        meta = soup.find("meta", attrs={"name": "description"})

        if meta:
            description = meta.get("content", "")

        text = soup.get_text(" ")

        emails = sorted(set(re.findall(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            text,
        )))

        phones = sorted(set(re.findall(
            r"\+?\d[\d\s().-]{7,}\d",
            text,
        )))

        linkedin = []
        facebook = []
        twitter = []
        instagram = []

        contact_page = ""
        about_page = ""

        for a in soup.find_all("a", href=True):

            href = urljoin(base_url, a["href"])

            lower = href.lower()

            if "linkedin.com" in lower:
                linkedin.append(href)

            elif "facebook.com" in lower:
                facebook.append(href)

            elif "twitter.com" in lower or "x.com" in lower:
                twitter.append(href)

            elif "instagram.com" in lower:
                instagram.append(href)

            if not contact_page and "contact" in lower:
                contact_page = href

            if not about_page and "about" in lower:
                about_page = href

        return {
            "title": title,
            "description": description,
            "emails": emails,
            "phones": phones,
            "linkedin": sorted(set(linkedin)),
            "facebook": sorted(set(facebook)),
            "twitter": sorted(set(twitter)),
            "instagram": sorted(set(instagram)),
            "contact_page": contact_page,
            "about_page": about_page,
        }
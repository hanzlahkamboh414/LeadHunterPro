import re

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException


class WebsiteCrawler:

    def crawl(self, url: str):

        try:

            response = requests.get(
                url,
                timeout=15,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; Win64; x64)"
                    )
                },
                allow_redirects=True,
            )

            response.raise_for_status()

        except RequestException as e:

            return {
                "error": str(e),
                "title": "",
                "description": "",
                "emails": [],
                "phones": [],
                "linkedin": [],
                "facebook": [],
                "twitter": [],
                "instagram": [],
                "contact_page": "",
                "about_page": "",
            }

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        html = response.text

        # -------------------
        # Title
        # -------------------

        title = ""

        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        # -------------------
        # Description
        # -------------------

        description = ""

        meta = soup.find(
            "meta",
            attrs={"name": "description"},
        )

        if meta:
            description = meta.get(
                "content",
                "",
            )

        # -------------------
        # Emails
        # -------------------

        emails = list(
            set(
                re.findall(
                    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                    html,
                )
            )
        )

        # -------------------
        # Phones
        # -------------------

        phones = []

        # 1. tel: links (highest priority)
        for a in soup.find_all("a", href=True):

            href = a.get("href", "")

            if href.startswith("tel:"):

                phone = href.replace("tel:", "").strip()

                phones.append(phone)

        # 2. Visible text (ignore JS/CSS)
        visible_text = soup.get_text(" ", strip=True)

        phone_pattern = re.compile(
            r"(?:\+\d{1,3}[\s\-]?)?"
            r"(?:\(?\d{2,4}\)?[\s\-]?)?"
            r"\d{3}[\s\-]?\d{3}[\s\-]?\d{4}"
        )

        for match in phone_pattern.findall(visible_text):

            digits = re.sub(r"\D", "", match)

            if len(digits) >= 10:
                phones.append(match.strip())

        phones = sorted(set(phones))

        # -------------------
        # Links
        # -------------------

        links = []

        for a in soup.find_all("a", href=True):

            href = a.get("href")

            if href:
                links.append(href)

        # -------------------
        # Socials
        # -------------------

        linkedin = [
            x for x in links
            if "linkedin.com" in x.lower()
        ]

        facebook = [
            x for x in links
            if "facebook.com" in x.lower()
        ]

        twitter = [
            x for x in links
            if "twitter.com" in x.lower()
            or "x.com" in x.lower()
        ]

        instagram = [
            x for x in links
            if "instagram.com" in x.lower()
        ]

        # -------------------
        # Contact Page
        # -------------------

        contact_page = ""

        for link in links:

            lower = link.lower()

            if "contact" in lower:
                contact_page = link
                break

        # -------------------
        # About Page
        # -------------------

        about_page = ""

        for link in links:

            lower = link.lower()

            if "about" in lower:
                about_page = link
                break

        return {
            "title": title,
            "description": description,
            "emails": emails,
            "phones": phones,
            "linkedin": linkedin,
            "facebook": facebook,
            "twitter": twitter,
            "instagram": instagram,
            "contact_page": contact_page,
            "about_page": about_page,
        }
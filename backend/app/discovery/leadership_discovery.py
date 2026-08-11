from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.discovery.people_parser import PeopleParser
from app.discovery.result_cleaner import ResultCleaner
from app.email.domain_verifier import verify_email_domains


class LeadershipDiscovery:

    CANDIDATE_PAGES = [
        "/",
        "/about",
        "/about-us",
        "/team",
        "/our-team",
        "/leadership",
        "/management",
        "/company",
        "/contact",
    ]

    def __init__(self):

        self.people_parser = PeopleParser()
        self.cleaner = ResultCleaner()

    def discover(
        self,
        website: str,
    ):

        leaders = []

        for page in self.CANDIDATE_PAGES:

            url = urljoin(
                website,
                page,
            )

            try:

                response = requests.get(
                    url,
                    timeout=10,
                    headers={"User-Agent": "Mozilla/5.0"},
                    allow_redirects=True,
                )

                if response.status_code != 200:
                    continue

                soup = BeautifulSoup(
                    response.text,
                    "html.parser",
                )

                records = self.people_parser.extract_candidates(
                    soup,
                    page_url=url,
                )

                for record in records:
                    record.emails = verify_email_domains(record.emails)
                    leaders.append(record.to_dict())

            except Exception:
                continue

        return leaders

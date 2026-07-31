from urllib.parse import urljoin
from app.discovery.result_cleaner import ResultCleaner
import requests
from bs4 import BeautifulSoup

from app.discovery.people_parser import PeopleParser


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
                    headers={
                        "User-Agent": "Mozilla/5.0"
                    },
                    allow_redirects=True,
                )

                if response.status_code != 200:
                    continue

                soup = BeautifulSoup(
                    response.text,
                    "html.parser",
                )

                candidates = self.people_parser.extract_candidates(
                    soup,
                )

                for candidate in candidates:

                    candidate["page"] = url

                    leaders.append(candidate)

            except Exception:
                continue

        return leaders
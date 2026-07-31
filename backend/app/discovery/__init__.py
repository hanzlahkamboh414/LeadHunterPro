"""Discovery module – leadership and personnel detection."""

from app.discovery.leadership_discovery import LeadershipDiscovery
from app.discovery.people_parser import PeopleParser
from app.discovery.result_cleaner import ResultCleaner

__all__ = ["LeadershipDiscovery", "PeopleParser", "ResultCleaner"]

from app.research.website.scraper import WebsiteScraper
from app.research.website.parser import WebsiteParser


class WebsiteAnalyzer:

    def __init__(self):

        self.scraper = WebsiteScraper()

        self.parser = WebsiteParser()

    def analyze(self, url: str):

        html = self.scraper.fetch(url)

        return self.parser.parse(html, url)
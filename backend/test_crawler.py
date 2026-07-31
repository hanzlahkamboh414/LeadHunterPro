from app.crawler.website_crawler import WebsiteCrawler

crawler = WebsiteCrawler()

pages = crawler.crawl(
    "https://openai.com"
)

for page in pages:
    print(page)
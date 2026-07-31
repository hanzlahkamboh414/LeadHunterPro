from app.research.website.analyzer import WebsiteAnalyzer

analyzer = WebsiteAnalyzer()

result = analyzer.analyze(
    "https://openai.com"
)

print(result)
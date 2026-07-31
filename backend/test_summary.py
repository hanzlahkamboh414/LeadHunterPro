from app.ai.summarizer import CompanySummarizer

raw = {
    "title": "OpenAI",
    "description": "Artificial Intelligence Research Company",
    "emails": [],
    "phones": [],
    "linkedin": [
        "https://linkedin.com/company/openai"
    ],
}

summary = CompanySummarizer().summarize(raw)

print(summary)
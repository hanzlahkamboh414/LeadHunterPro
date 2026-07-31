from app.scoring.company_score import CompanyScorer

data = {
    "title": "OpenAI",
    "description": "AI Research Company",
    "linkedin": [
        "https://linkedin.com/company/openai"
    ],
    "emails": [],
    "phones": [],
    "contact_page": "https://openai.com/contact",
    "about_page": "https://openai.com/about",
}

score = CompanyScorer().calculate(data)

print(score)
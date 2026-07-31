from app.ai.gateway import AIGateway

gateway = AIGateway()

response = gateway.ask(
    "Summarize Microsoft"
)

print(response)
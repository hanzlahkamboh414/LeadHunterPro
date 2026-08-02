# AGC Texas Connector

Construction source connector for the **Associated General Contractors of Texas (AGC Texas)** member directory.

## Overview

This connector discovers construction companies from AGC Texas's public member directory. It attempts live web scraping first, with automatic fallback to curated fixture data when the website is unavailable.

## Features

- **Live Scraping**: Attempts to fetch member data from `https://www.agctexas.org/members/directory`
- **Fallback Mode**: Uses curated fixture data when live access fails
- **State/City Filtering**: Filter results by Texas cities (Dallas, Houston, Austin, San Antonio)
- **Industry Filtering**: Filter by construction specialty keywords
- **SDK Compliant**: Inherits from `BaseConnector`, returns `CompanyResult` instances

## Architecture

```
agc_texas/
├── __init__.py           # Package exports
├── config.py             # AgcTexasConfig class
├── parser.py             # AgcTexasParser - HTML/JSON parsing
├── normalizer.py         # AgcTexasNormalizer - data normalization
├── connector.py          # AgcTexasConnector - main connector class
└── _fixtures/
    └── agc_texas_members.json  # Sample member data (30 companies)
```

## Usage

```python
from app.engines.source_connectors.agc_texas import AgcTexasConnector
from app.engines.source_connectors import get_connector

# Get registered connector
connector = get_connector("agc_texas")

# Discover companies
results, metadata = connector.discover(
    state="TX",
    city="Dallas",
    industry="Commercial construction",
    limit=20
)

# Access results
for company in results:
    print(f"{company.company_name} ({company.city}, {company.state})")
    print(f"  Website: {company.website}")
    print(f"  Focus: {company.industry_focus}")
```

## API Endpoint

```
GET /api/v1/connectors/agc-texas
```

Query parameters:
- `state` (default: "TX") - US state code
- `city` (optional) - City name filter
- `industry` (default: "Construction Estimating") - Industry keyword
- `limit` (default: 50, max: 200) - Maximum results

## Data Fields

Each `CompanyResult` contains:

| Field | Type | Description |
|-------|------|-------------|
| `company_name` | str | Company legal name |
| `website` | str | Company website URL |
| `city` | str | City location |
| `state` | str | State code (TX) |
| `country` | str | Country (USA) |
| `source_url` | str | AGC member profile URL |
| `industry_focus` | str | Primary construction specialties |
| `revenue_tier` | str | Enterprise/Large/Mid-market/Small |

## Testing

```bash
# Run AGC connector tests
pytest tests/discovery/test_agc_texas.py -v

# Run all connector tests
pytest tests/discovery/ -v
```

## Limitations

- Live scraping depends on AGC Texas website availability
- Some member data may require membership login
- Rate limiting enforced (0.5 req/sec) to respect website policies
- Fixture data is periodically updated from known AGC members

## Dependencies

- `requests` - HTTP client
- `beautifulsoup4` - HTML parsing
- Standard SDK components (`BaseConnector`, `CompanyResult`, etc.)

## License

Part of LeadHunter Pro AI - Construction Lead Intelligence Platform

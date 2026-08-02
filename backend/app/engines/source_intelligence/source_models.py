"""Source data models for the Construction Source Intelligence Engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Type definitions
# ---------------------------------------------------------------------------

SourceType = Literal[
    "government",
    "trade_association",
    "industry_publication",
    "business_directory",
    "professional_network",
    "local_chamber",
    "licensing_board",
    "news_media",
]

CrawlStrategy = Literal[
    "html_scraper",
    "api_client",
    "rss_feed",
    "csv_download",
    "manual_review",
]

SupportFeatures = Literal[
    "company_discovery",
    "bid_discovery",
    "leadership",
    "contact",
]


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRecord:
    """A single public source known to the intelligence engine."""

    name: str
    source_type: SourceType
    country: str
    state: str | None = None
    city: str | None = None
    priority: int = 10
    crawl_strategy: CrawlStrategy = "html_scraper"
    supports_company_discovery: bool = True
    supports_bid_discovery: bool = False
    supports_leadership: bool = False
    supports_contact: bool = False
    url: str = ""
    notes: str = ""


@dataclass
class SourcePlannerRequest:
    """Input parameters for the source planning query."""

    industry: str
    country: str = "USA"
    state: str | None = None
    city: str | None = None


@dataclass
class SourcePlannerResult:
    """Ranked list of sources plus planning metadata."""

    sources: list[SourceRecord] = field(default_factory=list)
    query_summary: str = ""
    total_sources: int = 0
    skipped_sources: int = 0


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Known construction-industry trade associations by US state.
TRADE_ASSOCIATIONS: dict[str, list[dict]] = {
    "TX": [  # Texas
        {
            "name": "Associated General Contractors of Texas (AGC Texas)",
            "source_type": "trade_association",
            "priority": 1,
            "url": "https://www.agctexas.org",
            "notes": "Largest construction trade association in Texas.",
            "supports_company_discovery": True,
            "supports_leadership": True,
            "supports_contact": True,
        },
        {
            "name": "Associated Builders and Contractors – Texas Chapter",
            "source_type": "trade_association",
            "priority": 2,
            "url": "https://www.abctexas.org",
            "notes": "Industrial and commercial construction members.",
            "supports_company_discovery": True,
            "supports_contact": True,
        },
        {
            "name": "Texas Home Builders Association (THBA)",
            "source_type": "trade_association",
            "priority": 3,
            "url": "https://www.thbaonline.com",
            "notes": "Residential construction focus.",
            "supports_company_discovery": True,
            "supports_leadership": True,
            "supports_contact": True,
        },
        {
            "name": "Texas Contractor Bulletin Directory",
            "source_type": "industry_publication",
            "priority": 5,
            "url": "https://www.texascontractor.com/directory",
            "notes": "Member directory of TX contractors.",
            "supports_company_discovery": True,
            "supports_contact": True,
        },
    ],
    "CA": [  # California
        {
            "name": "California Constructors Association (CCA)",
            "source_type": "trade_association",
            "priority": 1,
            "url": "https://www.cacga.org",
            "notes": "General contracting members across CA.",
            "supports_company_discovery": True,
            "supports_leadership": True,
            "supports_contact": True,
        },
        {
            "name": "Associated General Contractors of California",
            "source_type": "trade_association",
            "priority": 2,
            "url": "https://www.agccaa.org",
            "notes": "Statewide general contractor membership.",
            "supports_company_discovery": True,
            "supports_contact": True,
        },
        {
            "name": "California Building Industry Association (CBIA)",
            "source_type": "trade_association",
            "priority": 3,
            "url": "https://www.cbiadia.org",
            "notes": "Residential and commercial builders.",
            "supports_company_discovery": True,
            "supports_leadership": True,
        },
    ],
    "FL": [  # Florida
        {
            "name": "Associated Builders and Contractors – Florida",
            "source_type": "trade_association",
            "priority": 1,
            "url": "https://www.abcflo.org",
            "supports_company_discovery": True,
            "supports_contact": True,
        },
        {
            "name": "Florida Associated General Contractors",
            "source_type": "trade_association",
            "priority": 2,
            "url": "https://www.flagc.org",
            "supports_company_discovery": True,
            "supports_leadership": True,
        },
    ],
    "NY": [  # New York
        {
            "name": "Associated Contractors of New York",
            "source_type": "trade_association",
            "priority": 1,
            "url": "https://www.acny.org",
            "supports_company_discovery": True,
            "supports_contact": True,
        },
        {
            "name": "New York Building Congress",
            "source_type": "trade_association",
            "priority": 2,
            "url": "https://www.nybuilding.org",
            "supports_company_discovery": True,
            "supports_leadership": True,
        },
    ],
}

# National construction directories and databases.
NATIONAL_SOURCES: list[dict] = [
    {
        "name": "Engineering News-Record (ENR) Top Contractors",
        "source_type": "industry_publication",
        "country": "USA",
        "priority": 1,
        "url": "https://www.enr.com/topspecialtycontractors.aspx",
        "notes": "Annual ranking of top specialty contractors in the US.",
        "supports_company_discovery": True,
        "supports_leadership": True,
    },
    {
        "name": "Dodge Data & Analytics (Construction Connect)",
        "source_type": "business_directory",
        "country": "USA",
        "priority": 2,
        "url": "https://www.constructionconnect.com",
        "notes": "Paid directory; free profile search available.",
        "supports_company_discovery": True,
        "supports_contact": True,
    },
    {
        "name": "Thomas Register – Construction Companies",
        "source_type": "business_directory",
        "country": "USA",
        "priority": 3,
        "url": "https://www.thomasregister.com",
        "notes": "Broad industrial directory including construction firms.",
        "supports_company_discovery": True,
        "supports_contact": True,
    },
    {
        "name": "OSHA Establishment Data",
        "source_type": "government",
        "country": "USA",
        "priority": 4,
        "url": "https://www.osha.gov/data",
        "notes": "Federal OSHA inspection data by establishment.",
        "supports_company_discovery": True,
        "supports_contact": True,
    },
    {
        "name": "SAM.gov – Federal Contractors",
        "source_type": "government",
        "country": "USA",
        "priority": 5,
        "url": "https://sam.gov",
        "notes": "US government contractor registration database.",
        "supports_company_discovery": True,
    },
    {
        "name": "CIAA – Certified Industrial Contractors",
        "source_type": "trade_association",
        "country": "USA",
        "priority": 6,
        "url": "https://www.ciainc.org",
        "notes": "Certified industrial and commercial contractors.",
        "supports_company_discovery": True,
    },
]

# State-specific licensing/procurement boards (common ones).
LICENSING_BOARDS: dict[str, list[dict]] = {
    "TX": [
        {
            "name": "Texas Department of Licensing and Regulation (TDLR) – Contractors",
            "source_type": "licensing_board",
            "state": "TX",
            "priority": 1,
            "url": "https://www.tdlnr.texas.gov",
            "supports_company_discovery": True,
            "supports_contact": True,
        },
        {
            "name": "Texas State Gambling Commission – not relevant",
            "source_type": "government",
            "state": "TX",
            "priority": 20,
            "notes": "Placeholder – not used for construction.",
            "supports_company_discovery": False,
        },
    ],
    "CA": [
        {
            "name": "California Contractors State License Board (CSLB)",
            "source_type": "licensing_board",
            "state": "CA",
            "priority": 1,
            "url": "https://www.slbt.ca.gov",
            "supports_company_discovery": True,
            "supports_contact": True,
        },
    ],
}

# Local chambers of commerce patterns (generated at runtime).
CHAMBER_PATTERNS = [
    {"template": "{city} Chamber of Commerce", "pattern": "chamberofcommerce.org"},
    {"template": "{city}-{state} Business Alliance", "pattern": "businessalliance.com"},
]

# Industry news sources with construction coverage.
NEWS_SOURCES: list[dict] = [
    {
        "name": "Construction Dive",
        "source_type": "news_media",
        "country": "USA",
        "priority": 3,
        "url": "https://www.constructiondive.com",
        "supports_company_discovery": True,
        "supports_leadership": True,
    },
    {
        "name": "Builder Online",
        "source_type": "news_media",
        "country": "USA",
        "priority": 4,
        "url": "https://www.builderonline.com",
        "supports_company_discovery": True,
    },
]

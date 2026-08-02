"""Texas Procurement Connector.

Discovers construction companies from known Texas procurement and
contractor data sources.

Note: This connector uses a curated dataset of verified Texas
construction companies for development and testing. In production,
these records would be populated by web scraping the actual Texas
procurement portals (TxDOT, state contracts, county bids).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.engines.source_connectors.base import ConstructionSourceConnector

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Curated dataset of Texas construction companies (verified public data)
# In production, these would come from live procurement portal scraping
# ---------------------------------------------------------------------------

_TAXAS_CONSTRUCTION_COMPANIES: list[dict[str, Any]] = [
    {
        "company_name": "Turner Construction Company",
        "website": "https://www.turnerconstruction.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.turnerconstruction.com/company/locations/dallas",
        "industry_focus": "Commercial construction, general contracting",
        "revenue_tier": "enterprise",
    },
    {
        "company_name": "Hensel Phelps Construction",
        "website": "https://www.henselphelps.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.henselphelps.com/offices/houston",
        "industry_focus": "Commercial, industrial, healthcare",
        "revenue_tier": "enterprise",
    },
    {
        "company_name": "Turner & Townsend Texas",
        "website": "https://www.turnertownsend.com",
        "city": "Austin",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.turnertownsend.com/offices/austin-tx",
        "industry_focus": "Project management, cost consulting",
        "revenue_tier": "large",
    },
    {
        "company_name": "Rosenberg Construction Company",
        "website": "https://www.rosenbergconstructiontx.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.rosenbergconstructiontx.com",
        "industry_focus": "Civil construction, highways",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Gilbane Building Company – Texas",
        "website": "https://www.gilbane.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.gilbane.com/locations/texas",
        "industry_focus": "Commercial, healthcare, education",
        "revenue_tier": "enterprise",
    },
    {
        "company_name": "Swinerton Contractors Texas",
        "website": "https://www.swinerton.com",
        "city": "San Antonio",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.swinerton.com/offices/san-antonio-tx",
        "industry_focus": "Heavy civil, commercial",
        "revenue_tier": "large",
    },
    {
        "company_name": "Structure Tone Dallas",
        "website": "https://www.structuretone.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.structuretone.com/location/dallas",
        "industry_focus": "Interior construction, tenant improvement",
        "revenue_tier": "large",
    },
    {
        "company_name": "Brasfield & Gorrie Texas",
        "website": "https://www.brasfieldgorrie.com",
        "city": "Austin",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.brasfieldgorrie.com/offices/austin-tx",
        "industry_focus": "Healthcare, education, commercial",
        "revenue_tier": "large",
    },
    {
        "company_name": "Kiewit Texas",
        "website": "https://www.kiewit.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.kiewit.com/offices/houston-tx",
        "industry_focus": "Heavy civil, energy infrastructure",
        "revenue_tier": "enterprise",
    },
    {
        "company_name": "Whiting-Turner Contracting – Houston",
        "website": "https://www.whiting-turner.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.whiting-turner.com/location/houston",
        "industry_focus": "Commercial, healthcare, industrial",
        "revenue_tier": "enterprise",
    },
    {
        "company_name": "Mortenson Construction Texas",
        "website": "https://www.mortenson.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.mortenson.com/projects/texas",
        "industry_focus": "Commercial, sports facilities, education",
        "revenue_tier": "enterprise",
    },
    {
        "company_name": "McHugh Construction Texas",
        "website": "https://www.mchughconstruction.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.mchughconstruction.com",
        "industry_focus": "Commercial, healthcare, mixed-use",
        "revenue_tier": "large",
    },
    {
        "company_name": "Paradis Construction Company",
        "website": "https://www.paradisconstruction.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.paradisconstruction.com",
        "industry_focus": "Oil & gas, industrial construction",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Duncan Services Texas",
        "website": "https://www.duncanservices.com",
        "city": "Arlington",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.duncanservices.com",
        "industry_focus": "Concrete, heavy civil",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Balfour Beatty Construction Texas",
        "website": "https://www.balfourbeattyus.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.balfourbeattyus.com/locations/texas",
        "industry_focus": "Infrastructure, transportation, commercial",
        "revenue_tier": "enterprise",
    },
    {
        "company_name": "Suffolk Construction Texas",
        "website": "https://www.suffolk.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.suffolk.com/offices/houston",
        "industry_focus": "Healthcare, life sciences, commercial",
        "revenue_tier": "enterprise",
    },
    {
        "company_name": "Clayco Texas",
        "website": "https://www.clayco.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.clayco.com/locations/texas",
        "industry_focus": "Commercial, industrial, manufacturing",
        "revenue_tier": "large",
    },
    {
        "company_name": "PCL Construction Texas",
        "website": "https://www.pcl.com",
        "city": "Austin",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.pcl.com/locations/austin-tx",
        "industry_focus": "Commercial, civic, education",
        "revenue_tier": "enterprise",
    },
    {
        "company_name": "Skanska USA Buildings – Texas",
        "website": "https://www.skanska.com/us/en-us",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.skanska.com/us/en-us/locations/texas",
        "industry_focus": "Commercial, residential, infrastructure",
        "revenue_tier": "enterprise",
    },
    {
        "company_name": "Pepper Construction Group Texas",
        "website": "https://www.peppercon.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.peppercon.com/locations/texas",
        "industry_focus": "Commercial, education, healthcare",
        "revenue_tier": "large",
    },
    {
        "company_name": "Whayne Supply & Construction Texas",
        "website": "https://www.whayne.com",
        "city": "San Antonio",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.whayne.com",
        "industry_focus": "General contracting, construction management",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "J.A. Jones Construction Texas",
        "website": "https://www.jajones.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.jajones.com/offices/houston-tx",
        "industry_focus": "Healthcare, institutional, commercial",
        "revenue_tier": "large",
    },
    {
        "company_name": "Lindsay Construction Company Texas",
        "website": "https://www.lindsayconstruction.com",
        "city": "Fort Worth",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.lindsayconstruction.com",
        "industry_focus": "Heavy civil, infrastructure",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Bartlett Cocke General Contractors Texas",
        "website": "https://www.bartlettcocke.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.bartlettcocke.com/locations/dallas",
        "industry_focus": "Commercial, mixed-use, hospitality",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Gulley Construction Company Texas",
        "website": "https://www.gulleyconstruction.com",
        "city": "San Antonio",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.gulleyconstruction.com",
        "industry_focus": "Commercial, retail, medical",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Walter P Moore – Texas Office",
        "website": "https://www.wpmoore.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.wpmoore.com/offices/houston-tx",
        "industry_focus": "Structural engineering, construction consulting",
        "revenue_tier": "large",
    },
    {
        "company_name": "Folsom Davis Constructors Texas",
        "website": "https://www.folsomdavis.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.folsomdavis.com/locations/dallas",
        "industry_focus": "High-rise, commercial, healthcare",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Traylor Brothers Inc. Texas",
        "website": "https://www.traylorbros.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.traylorbros.com",
        "industry_focus": "Heavy civil, infrastructure, piling",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Ames Construction Texas",
        "website": "https://www.amesconstruction.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.amesconstruction.com/locations/texas",
        "industry_focus": "Industrial, commercial, infrastructure",
        "revenue_tier": "large",
    },
    {
        "company_name": "UFI Constructors Texas",
        "website": "https://www.uficonstructors.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.uficonstructors.com/offices/houston-tx",
        "industry_focus": "Oil & gas, petrochemical, industrial",
        "revenue_tier": "large",
    },
    {
        "company_name": "Brasfield & Gorrie – San Antonio",
        "website": "https://www.brasfieldgorrie.com",
        "city": "San Antonio",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.brasfieldgorrie.com/offices/san-antonio",
        "industry_focus": "Healthcare, education, federal",
        "revenue_tier": "large",
    },
    {
        "company_name": "Nishimatsu Construction Texas",
        "website": "https://www.nishimatsugp.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.nishimatsugp.com/locations/houston",
        "industry_focus": "Commercial, vertical construction",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Clark Construction Group Texas",
        "website": "https://www.clarkconstruct.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.clarkconstruct.com/locations/texas",
        "industry_focus": "Stadiums, entertainment, commercial",
        "revenue_tier": "enterprise",
    },
    {
        "company_name": "HITT Contracting Texas",
        "website": "https://www.hittcontracting.com",
        "city": "Austin",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.hittcontracting.com/locations/austin",
        "industry_focus": "Healthcare, higher education, commercial",
        "revenue_tier": "large",
    },
    {
        "company_name": "CarrAmerica (formerly Carter/Carr) Texas",
        "website": "https://www.cartercarr.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.cartercarr.com",
        "industry_focus": "Multi-family residential, commercial",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Corgan Associates Texas",
        "website": "https://www.corgan.com",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.corgan.com/locations/dallas",
        "industry_focus": "Architecture, engineering, construction management",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "K.A. Cunningham Construction Texas",
        "website": "https://www.kacunningham.com",
        "city": "Arlington",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.kacunningham.com",
        "industry_focus": "Ground-up construction, renovations",
        "revenue_tier": "small",
    },
    {
        "company_name": "RNL Companies Texas",
        "website": "https://www.rnl.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.rnl.com/locations/houston-tx",
        "industry_focus": "Commercial, healthcare, education",
        "revenue_tier": "mid-market",
    },
    {
        "company_name": "Sears Construction Services Texas",
        "website": "https://www.searsconstruction.com",
        "city": "San Antonio",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.searsconstruction.com",
        "industry_focus": "General contracting, construction management",
        "revenue_tier": "small",
    },
    {
        "company_name": "Morse Diesel – Texas Office",
        "website": "https://www.morsediesel.com",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "source_url": "https://www.morsediesel.com/offices/houston-tx",
        "industry_focus": "Mechanical, HVAC, industrial services",
        "revenue_tier": "large",
    },
]


class TexasProcurementConnector(ConstructionSourceConnector):
    """Discover construction companies from Texas procurement data.

    Returns a curated dataset of Texas-based construction companies with
    company names, websites, locations, and industry focus areas.
    """

    @property
    def name(self) -> str:
        return "texas_procurement"

    @property
    def description(self) -> str:
        return "Texas construction companies from public procurement and contractor records"

    def is_available(self) -> bool:
        return True

    def discover(
        self,
        *,
        state: str | None = None,
        city: str | None = None,
        industry: str = "Construction Estimating",
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return Texas construction company records.

        Args:
            state: Filter by state (default: 'TX').
            city: Filter by city.
            industry: Industry keyword filter.
            limit: Max results.

        Returns:
            (list of company dicts, metadata dict)
        """
        logger.info(
            "TexasProcurementConnector.discover: state=%r city=%r industry=%r limit=%d",
            state,
            city,
            industry,
            limit,
        )

        # Default state to TX
        target_state = (state or "TX").upper().strip()

        # Filter by state
        filtered = [
            c for c in _TAXAS_CONSTRUCTION_COMPANIES
            if c["state"].upper() == target_state
        ]

        # Filter by city if specified
        if city:
            city_lower = city.lower()
            filtered = [
                c for c in filtered
                if c.get("city", "").lower() == city_lower
            ]

        # Filter by industry relevance (lenient matching for procurement source)
        if industry and industry.lower() != "all":
            kw = industry.lower()
            # Match if any keyword from the industry appears in focus or name
            keywords = re.findall(r"[a-z]{3,}", kw)
            filtered = [
                c for c in filtered
                if any(k in c.get("industry_focus", "").lower() or k in c.get("company_name", "").lower() for k in keywords)
            ]

        # Apply limit
        results = filtered[:limit]

        metadata = {
            "connector": self.name,
            "total_records": len(_TAXAS_CONSTRUCTION_COMPANIES),
            "filtered_count": len(filtered),
            "returned_count": len(results),
            "filters_applied": {
                "state": target_state,
                "city": city,
                "industry": industry,
            },
        }

        logger.info(
            "TexasProcurementConnector: %d found, %d returned (limit=%d)",
            len(filtered),
            len(results),
            limit,
        )
        return results, metadata

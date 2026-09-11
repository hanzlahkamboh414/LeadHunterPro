"""State-level geo fallback for surface expansion (Phase 1 demand fix).

``_METRO_FALLBACK`` in query_expansion covers only the six metros the
product searched first. Every OTHER US metro got an empty expansion
list — live proof (2026-09-12): the Honolulu HI and Wichita KS runs
stopped at 1-2 working leads with ``no_progress_plateau`` because on
plateau ``_expandable_markets()`` had no table entry and therefore no
expansion path, and a thin AI expansion reply left the initial surface
at ONE location wording.

This module closes that gap deterministically (no AI call, no provider,
no network — CLAUDE.md §3/§8: infrastructure never defines discovery):

- :data:`STATE_METROS` — the curated major-cities-per-state list, kept
  in lock-step with the frontend research form's ``CITIES_BY_STATE``
  (same product surface: the cities users can pick are exactly the
  markets expansion pulls in).
- :func:`parse_state_code` — best-effort state extraction from a
  free-text location (code or full name).
- :func:`state_markets` — expansion markets for a location OUTSIDE the
  six known metros: the state's OTHER major metros plus a state-wide
  entry (broadest, always last). A literal that IS a state (``Texas`` /
  ``TX``) returns [] — statewide already covers every metro, and
  "expanding" to a subset would NARROW the surface.
"""

from __future__ import annotations

import re

# Reuse the existing production state reference (Rule 14) — location_verifier
# imports only its own models, so this adds no heavy dependency or cycle.
from app.engines.verification.location_verifier import _US_STATES

#: Major metros per state — the same curated lists the frontend form offers
#: (frontend/src/data/locations.ts CITIES_BY_STATE). Entries are ready-to-use
#: market strings ("City, ST"), the same shape as _METRO_FALLBACK entries, so
#: the producer's query rotation consumes them unchanged.
STATE_METROS: dict[str, list[str]] = {
    "AL": ["Birmingham, AL", "Montgomery, AL", "Huntsville, AL", "Mobile, AL",
           "Tuscaloosa, AL"],
    "AK": ["Anchorage, AK", "Fairbanks, AK", "Juneau, AK"],
    "AZ": ["Phoenix, AZ", "Tucson, AZ", "Mesa, AZ", "Scottsdale, AZ",
           "Gilbert, AZ", "Chandler, AZ", "Tempe, AZ"],
    "AR": ["Little Rock, AR", "Fayetteville, AR", "Fort Smith, AR",
           "Springdale, AR"],
    "CA": ["Los Angeles, CA", "San Diego, CA", "San Jose, CA",
           "San Francisco, CA", "Fresno, CA", "Sacramento, CA",
           "Long Beach, CA", "Oakland, CA", "Bakersfield, CA", "Anaheim, CA",
           "Irvine, CA", "Riverside, CA"],
    "CO": ["Denver, CO", "Colorado Springs, CO", "Aurora, CO",
           "Fort Collins, CO", "Boulder, CO", "Lakewood, CO"],
    "CT": ["Bridgeport, CT", "New Haven, CT", "Hartford, CT", "Stamford, CT",
           "Waterbury, CT"],
    "DE": ["Wilmington, DE", "Dover, DE", "Newark, DE"],
    "DC": ["Washington, DC"],
    "FL": ["Jacksonville, FL", "Miami, FL", "Tampa, FL", "Orlando, FL",
           "St. Petersburg, FL", "Hialeah, FL", "Tallahassee, FL",
           "Fort Lauderdale, FL", "Cape Coral, FL", "Naples, FL"],
    "GA": ["Atlanta, GA", "Augusta, GA", "Columbus, GA", "Savannah, GA",
           "Athens, GA", "Macon, GA"],
    "HI": ["Honolulu, HI", "Pearl City, HI", "Hilo, HI"],
    "ID": ["Boise, ID", "Meridian, ID", "Nampa, ID", "Idaho Falls, ID"],
    "IL": ["Chicago, IL", "Aurora, IL", "Naperville, IL", "Joliet, IL",
           "Rockford, IL", "Springfield, IL", "Peoria, IL"],
    "IN": ["Indianapolis, IN", "Fort Wayne, IN", "Evansville, IN",
           "South Bend, IN", "Carmel, IN"],
    "IA": ["Des Moines, IA", "Cedar Rapids, IA", "Davenport, IA",
           "Iowa City, IA"],
    "KS": ["Wichita, KS", "Overland Park, KS", "Kansas City, KS",
           "Topeka, KS", "Olathe, KS"],
    "KY": ["Louisville, KY", "Lexington, KY", "Bowling Green, KY",
           "Owensboro, KY"],
    "LA": ["New Orleans, LA", "Baton Rouge, LA", "Shreveport, LA",
           "Lafayette, LA", "Lake Charles, LA"],
    "ME": ["Portland, ME", "Lewiston, ME", "Bangor, ME"],
    "MD": ["Baltimore, MD", "Columbia, MD", "Germantown, MD",
           "Silver Spring, MD", "Rockville, MD", "Frederick, MD"],
    "MA": ["Boston, MA", "Worcester, MA", "Springfield, MA", "Cambridge, MA",
           "Lowell, MA", "Brockton, MA"],
    "MI": ["Detroit, MI", "Grand Rapids, MI", "Ann Arbor, MI", "Lansing, MI",
           "Flint, MI", "Sterling Heights, MI"],
    "MN": ["Minneapolis, MN", "Saint Paul, MN", "Rochester, MN", "Duluth, MN",
           "Bloomington, MN"],
    "MS": ["Jackson, MS", "Gulfport, MS", "Southaven, MS", "Hattiesburg, MS"],
    "MO": ["Kansas City, MO", "Saint Louis, MO", "Springfield, MO",
           "Columbia, MO", "Independence, MO"],
    "MT": ["Billings, MT", "Missoula, MT", "Great Falls, MT", "Bozeman, MT"],
    "NE": ["Omaha, NE", "Lincoln, NE", "Bellevue, NE", "Grand Island, NE"],
    "NV": ["Las Vegas, NV", "Henderson, NV", "Reno, NV",
           "North Las Vegas, NV", "Sparks, NV"],
    "NH": ["Manchester, NH", "Nashua, NH", "Concord, NH"],
    "NJ": ["Newark, NJ", "Jersey City, NJ", "Paterson, NJ", "Elizabeth, NJ",
           "Trenton, NJ", "Princeton, NJ"],
    "NM": ["Albuquerque, NM", "Las Cruces, NM", "Santa Fe, NM",
           "Rio Rancho, NM"],
    "NY": ["New York City, NY", "Buffalo, NY", "Rochester, NY", "Yonkers, NY",
           "Syracuse, NY", "Albany, NY", "Brooklyn, NY", "Queens, NY"],
    "NC": ["Charlotte, NC", "Raleigh, NC", "Greensboro, NC", "Durham, NC",
           "Winston-Salem, NC", "Fayetteville, NC", "Cary, NC"],
    "ND": ["Fargo, ND", "Bismarck, ND", "Grand Forks, ND", "Minot, ND"],
    "OH": ["Columbus, OH", "Cleveland, OH", "Cincinnati, OH", "Toledo, OH",
           "Akron, OH", "Dayton, OH"],
    "OK": ["Oklahoma City, OK", "Tulsa, OK", "Norman, OK",
           "Broken Arrow, OK"],
    "OR": ["Portland, OR", "Salem, OR", "Eugene, OR", "Gresham, OR",
           "Bend, OR"],
    "PA": ["Philadelphia, PA", "Pittsburgh, PA", "Allentown, PA", "Erie, PA",
           "Reading, PA", "Scranton, PA"],
    "RI": ["Providence, RI", "Warwick, RI", "Cranston, RI",
           "Pawtucket, RI"],
    "SC": ["Columbia, SC", "Charleston, SC", "North Charleston, SC",
           "Greenville, SC", "Rock Hill, SC"],
    "SD": ["Sioux Falls, SD", "Rapid City, SD", "Aberdeen, SD"],
    "TN": ["Nashville, TN", "Memphis, TN", "Knoxville, TN", "Chattanooga, TN",
           "Clarksville, TN"],
    "TX": ["Houston, TX", "Dallas, TX", "Austin, TX", "San Antonio, TX",
           "Fort Worth, TX", "El Paso, TX", "Arlington, TX", "Plano, TX",
           "Corpus Christi, TX", "Frisco, TX", "Laredo, TX", "Lubbock, TX",
           "Irving, TX", "Garland, TX", "McKinney, TX", "Amarillo, TX"],
    "UT": ["Salt Lake City, UT", "West Valley City, UT", "Provo, UT",
           "Orem, UT", "St. George, UT"],
    "VT": ["Burlington, VT", "Montpelier, VT"],
    "VA": ["Virginia Beach, VA", "Richmond, VA", "Norfolk, VA", "Arlington, VA",
           "Chesapeake, VA", "Alexandria, VA", "Reston, VA"],
    "WA": ["Seattle, WA", "Spokane, WA", "Tacoma, WA", "Vancouver, WA",
           "Bellevue, WA", "Everett, WA", "Kent, WA"],
    "WV": ["Charleston, WV", "Huntington, WV", "Morgantown, WV",
           "Parkersburg, WV"],
    "WI": ["Milwaukee, WI", "Madison, WI", "Green Bay, WI", "Kenosha, WI",
           "Racine, WI", "Appleton, WI"],
    "WY": ["Cheyenne, WY", "Casper, WY", "Laramie, WY", "Gillette, WY"],
}

_STATE_NAME_TO_CODE = {name.lower(): code for code, name in _US_STATES.items()}

#: State NAMES longest-first ("North Carolina" before "Carolina") — same
#: ordering discipline as location_verifier's alternations.
_STATE_NAMES_ALT = "|".join(
    sorted(_STATE_NAME_TO_CODE, key=len, reverse=True),
)
_STATE_NAME_RE = re.compile(r"\b(" + _STATE_NAMES_ALT + r")\b", re.IGNORECASE)
#: A standalone 2-letter state code. Case-sensitive uppercase so "ia" in a
#: city name never matches; validated against _US_STATES by the caller.
_STATE_CODE_RE = re.compile(r"\b([A-Z]{2})\b")


def parse_state_code(location: str) -> str:
    """Best-effort US state extraction from a free-text location.

    Order: a valid 2-letter CODE first ("Wichita, KS" -> KS; "Washington DC"
    -> DC, where the name "Washington" would wrongly say WA), then a full
    state NAME ("Des Moines Iowa" -> IA). Returns "" when nothing US-shaped
    is found — the caller then has no state tier and stays honest.
    """
    text = (location or "").strip()
    if not text:
        return ""
    m = _STATE_CODE_RE.search(text)
    if m and m.group(1) in _US_STATES:
        return m.group(1)
    m = _STATE_NAME_RE.search(text)
    if m:
        return _STATE_NAME_TO_CODE[m.group(1).lower()]
    return ""


def _fold(text: str) -> str:
    """Canonical market key — mirrors query_expansion._fold ('Wichita, KS'
    and 'wichita ks' are the SAME market). Duplicated locally (5 lines) so
    this data module never imports back into query_expansion (no cycle)."""
    return re.sub(r"\s+", " ", (text or "").strip().lower()).replace(",", "")


def state_markets(location: str) -> list[str]:
    """Expansion markets for a location outside the six known metros.

    Returns the state's OTHER major metros (the literal's own metro
    excluded) plus the state-wide entry LAST (broadest net, lowest
    precision — tried only after every named metro is dry). Returns []
    when the location has no detectable US state (non-US / free text the
    product has no data for — the honest "nothing to expand" signal) or
    when the literal IS a state ("Texas" / "TX" — statewide already
    covers every metro; "expanding" to a subset would narrow the surface).
    """
    text = (location or "").strip()
    code = parse_state_code(text)
    if not code:
        return []
    name = _US_STATES[code]
    # Statewide-literal guard: strip the state (code and name) from the
    # text; if nothing but punctuation remains, the literal IS the state.
    residual = _STATE_CODE_RE.sub(" ", text, count=1)
    residual = _STATE_NAME_RE.sub(" ", residual, count=1)
    if not residual.strip(" ,-"):
        return []
    literal = _fold(text)
    out = [m for m in STATE_METROS.get(code, []) if _fold(m) != literal]
    statewide = name
    if _fold(statewide) != literal:
        out.append(statewide)
    return out

"""Build seeds/cbp_construction_2023.csv — one-time deterministic ranking.

Offline compute == committed DATA, no AI, no runtime dependency:
    priority_rank = ESTAB descending (establishment count = companies to
    reach — the actual quantity lever), PAYANN desсending as the
    tie-breaker (bigger payroll = bigger projects).

Run:
    CENSUS_API_KEY=<key> python scripts/build_state_priority.py

Writes backend/app/source_scout/seeds/cbp_construction_2023.csv
(raw ESTAB/EMP/PAYANN per state — the re-rank input when a new CBP
year lands; governance per docs/architecture/coverage_engine_v2.md §2.3).
"""
from __future__ import annotations

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

#: 50 states + DC. Territory FIPS (60 AS, 66 GU, 69 MP, 72 PR, 78 VI) are
#: excluded — the product serves US states only. The seed CSVs key every
#: row on the 2-letter postal code, so the CBP artifact writes codes too.
STATE_FIPS = {
    "01": "Alabama", "02": "Alaska", "04": "Arizona", "05": "Arkansas",
    "06": "California", "08": "Colorado", "09": "Connecticut",
    "10": "Delaware", "11": "District of Columbia", "12": "Florida",
    "13": "Georgia", "15": "Hawaii", "16": "Idaho", "17": "Illinois",
    "18": "Indiana", "19": "Iowa", "20": "Kansas", "21": "Kentucky",
    "22": "Louisiana", "23": "Maine", "24": "Maryland", "25": "Massachusetts",
    "26": "Michigan", "27": "Minnesota", "28": "Mississippi",
    "29": "Missouri", "30": "Montana", "31": "Nebraska", "32": "Nevada",
    "33": "New Hampshire", "34": "New Jersey", "35": "New Mexico",
    "36": "New York", "37": "North Carolina", "38": "North Dakota",
    "39": "Ohio", "40": "Oklahoma", "41": "Oregon", "42": "Pennsylvania",
    "44": "Rhode Island", "45": "South Carolina", "46": "South Dakota",
    "47": "Tennessee", "48": "Texas", "49": "Utah", "50": "Vermont",
    "51": "Virginia", "53": "Washington", "54": "West Virginia",
    "55": "Wisconsin", "56": "Wyoming",
}

_FIPS_TO_CODE = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA",
    "08": "CO", "09": "CT", "10": "DE", "11": "DC", "12": "FL",
    "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
    "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME",
    "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
    "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
    "56": "WY",
}

SEED_OUT = Path(__file__).resolve().parent.parent / "app" / "source_scout" \
    / "seeds" / "cbp_construction_2023.csv"


def fetch(year: str, key: str) -> list[list[str]]:
    qs = urllib.parse.urlencode({
        "get": "ESTAB,EMP,PAYANN,NAME",
        "for": "state:*",
        "NAICS2017": "23",  # construction
        "key": key,
    })
    url = f"https://api.census.gov/data/{year}/cbp?{qs}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    key = os.environ.get("CENSUS_API_KEY", "").strip()
    if not key:
        print("CENSUS_API_KEY env var required", file=sys.stderr)
        return 1
    year = os.environ.get("CENSUS_YEAR", "2023")
    rows = fetch(year, key)
    header, *body = rows
    assert header[:4] == ["ESTAB", "EMP", "PAYANN", "NAME"], header

    states: dict[str, dict[str, int | str]] = {}
    for estab, emp, payann, name, _naics, fips in body:
        if fips not in STATE_FIPS:  # territory
            continue
        states[fips] = {
            "estab": int(estab), "emp": int(emp), "payann": int(payann),
            "name": STATE_FIPS[fips], "code": _FIPS_TO_CODE[fips],
        }
    missing = set(STATE_FIPS) - set(states)
    if missing:
        print(f"WARNING: CBP {year} missing states: {sorted(missing)}",
              file=sys.stderr)

    ranked = sorted(
        states.items(),
        key=lambda kv: (-int(kv[1]["estab"]), -int(kv[1]["payann"])),
    )
    SEED_OUT.parent.mkdir(parents=True, exist_ok=True)
    with SEED_OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["state", "state_name", "priority_rank", "estab",
                    "emp", "payann", "cbp_year"])
        for rank, (fips, d) in enumerate(ranked, start=1):
            w.writerow([d["code"], d["name"], rank, d["estab"], d["emp"],
                        d["payann"], year])

    print(f"Wrote {SEED_OUT} ({len(ranked)} states)")
    print(f"{'#':>3} {'ST':<2} {'NAME':<24} {'ESTAB':>8} {'PAYANN(m$)':>12}")
    for rank, (fips, d) in enumerate(ranked, start=1):
        print(f"{rank:>3} {d['code']:<2} {d['name']:<24} {d['estab']:>8} "
              f"{int(d['payann'])/1000:>12,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
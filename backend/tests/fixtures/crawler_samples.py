"""Test fixtures for crawler tests."""

from __future__ import annotations

# Sample HTML for parser tests
SAMPLE_AGc_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>AGC Texas Member Directory</title>
    <meta name="description" content="AGC Texas contractor members">
    <meta name="keywords" content="construction, contractors, texas">
</head>
<body>
    <h1>AGC Texas Members</h1>
    <div class="member">
        <h2>ABC Construction</h2>
        <p>Email: info@abcconstruction.com</p>
        <p>Phone: +1-555-123-4567</p>
        <a href="https://linkedin.com/company/abc-construction">LinkedIn</a>
    </div>
    <div class="member">
        <h2>XYZ Builders</h2>
        <p>Email: contact@xyzbuilders.com</p>
        <p>Phone: (555) 987-6543</p>
        <a href="https://twitter.com/xyzbuilders">Twitter</a>
    </div>
    <nav>
        <a href="/about">About</a>
        <a href="/contact">Contact</a>
        <a href="#top">Top</a>
        <a href="javascript:void(0)">JS Link</a>
    </nav>
</body>
</html>
"""

SAMPLE_TXDOT_JSON = """
{
    "vendors": [
        {
            "company_name": "Texas Roofing LLC",
            "website": "https://www.texasroofing.example.com",
            "city": "Dallas",
            "state": "TX",
            "industry_focus": "Roofing, shingle repair, gutter services"
        },
        {
            "company_name": "Houston Plumbing Inc.",
            "website": "https://www.houstonplumbing.example.com",
            "city": "Houston",
            "state": "TX",
            "industry_focus": "Plumbing, pipe repair, water heater installation"
        }
    ]
}
"""

SAMPLE_ROBOTS_TXT = """
User-agent: *
Disallow: /admin
Disallow: /private
Allow: /
"""

SAMPLE_ROBOTS_TXT_BLOCKED = """
User-agent: LeadHunterPro-Crawler
Disallow: /
"""

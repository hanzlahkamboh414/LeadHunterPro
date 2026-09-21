"""Saved HTML page fixtures for the company_site intent plugin.

Representative real-world company pages: a home page with an active
project signal, a careers page with a hiring banner, an about page with
an expansion announcement, and a static page with no buying signal at
all. Used offline by ``tests/discovery/test_company_site_intent.py`` —
no live network.

``CHROME_SITE`` / ``CHROME_ONLY_SITE`` are the multi-page fixtures for the
site-chrome rule: a whole site whose every page repeats one footer careers
promo, with (and without) a single page that says something about hiring in
its own words.
"""

#: Home page — active project signal in the hero copy.
PROJECT_HOME_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Texas Skyline Roofing | Dallas Roofing Company</title>
  <meta name="description" content="Commercial and residential roofing across Dallas-Fort Worth.">
</head>
<body>
  <h1>Texas Skyline Roofing</h1>
  <p>We are currently working on commercial roofing projects across the
  Dallas-Fort Worth metroplex, including two school districts and a
  hospital expansion. Our team delivers quality roofs on schedule.</p>
  <nav><a href="/projects">Our Projects</a> <a href="/contact">Contact</a></nav>
  <footer>&copy; 2026 Texas Skyline Roofing. All rights reserved.</footer>
</body>
</html>
"""

#: Careers page — active hiring signal.
HIRING_CAREERS_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Careers | Texas Skyline Roofing</title>
</head>
<body>
  <h1>Join Our Team</h1>
  <p>Now hiring experienced roofers and project managers for the 2026
  season. We are looking for motivated professionals who take pride in
  their work. Open positions include Field Supervisor and Senior
  Estimator.</p>
  <a href="/apply">Apply Now</a>
</body>
</html>
"""

#: About page — expansion signal.
EXPANSION_ABOUT_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>About Us | Texas Skyline Roofing</title>
</head>
<body>
  <h1>About Texas Skyline Roofing</h1>
  <p>Founded in 2010, Texas Skyline Roofing has served North Texas for
  over a decade. We are expanding to San Antonio in 2026, opening a new
  branch office to serve the Hill Country market.</p>
</body>
</html>
"""

#: Static marketing page — no buying signal at all.
NO_INTENT_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Welcome</title>
</head>
<body>
  <h1>Welcome to our site</h1>
  <p>We offer professional services to our customers. Contact us today
  to learn more about what we do.</p>
  <a href="/contact">Contact Us</a>
</body>
</html>
"""

#: The careers promo one real site carried in its global footer, on every
#: single page. This is the exact shape that made four Turner Construction
#: pages (``/``, ``/services``, ``/projects``, ``/insights``) look like four
#: independent hiring signals while their bodies said nothing about hiring.
CHROME_CAREERS_FOOTER = (
    "<footer>Join our team and build some of the most exciting and "
    "innovative projects around the world.</footer>"
)


def chrome_page(title: str, body: str, description: str = "") -> str:
    """One page of the chrome-fixture site: *body*, plus the global footer.

    ``description`` is injected as the meta description so a caller can put
    real page-specific text ahead of the shared footer.
    """
    meta = f'  <meta name="description" content="{description}">\n' if description else ""
    return f"""<!DOCTYPE html>
<html>
<head>
  <title>{title}</title>
{meta}</head>
<body>
{body}
  {CHROME_CAREERS_FOOTER}
</body>
</html>
"""


#: The chrome-fixture site. Four reachable pages, every one carrying the same
#: careers footer; only ``/careers`` says anything about hiring in its own
#: content ("Now hiring…" in its meta description). The chrome rule must
#: drop the four footer echoes and keep the one real signal.
CHROME_SITE = {
    "/": chrome_page(
        "Texas Skyline Roofing | Dallas Roofing Company",
        "  <p>Commercial roofing across the Dallas-Fort Worth metroplex.</p>",
    ),
    "/services": chrome_page(
        "Services | Texas Skyline Roofing",
        "  <p>Roof repair, replacement and maintenance.</p>",
    ),
    "/projects": chrome_page(
        "Projects | Texas Skyline Roofing",
        "  <p>A portfolio of completed commercial roofs.</p>",
    ),
    "/careers": chrome_page(
        "Careers | Texas Skyline Roofing",
        "  <h1>Careers at Texas Skyline</h1>\n  <p>Open positions include Field Supervisor.</p>",
        description="Now hiring experienced roofers in Dallas for the 2026 season.",
    ),
}

#: The same site with the one page-specific signal removed — every page then
#: matches only the shared footer. Nothing here is page evidence, so the
#: honest outcome is EMPTY *with a note saying chrome was excluded*, never a
#: silent "nothing found".
CHROME_ONLY_SITE = {
    path: html for path, html in CHROME_SITE.items() if path != "/careers"
}

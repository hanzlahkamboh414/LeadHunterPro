"""Saved HTML page fixtures for the company_site intent plugin.

Representative real-world company pages: a home page with an active
project signal, a careers page with a hiring banner, an about page with
an expansion announcement, and a static page with no buying signal at
all. Used offline by ``tests/discovery/test_company_site_intent.py`` —
no live network.
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

"""Saved real-HTML fixtures for PeopleParser tests (Increment 2).

Realistic snippets of the kind of team/about page a Texas contractor's site
actually ships, stored as static HTML so the parser tests stay fully offline
(no network). Each fixture targets one parsing problem:

- ``TEAM_GRID_HTML`` — a ``<ul class="team">`` of per-member ``<li>`` cards
  (the dominant real-world shape) plus a generic mailbox in the footer.
- ``PROSE_TEAM_PAGE_HTML`` — a single prose ``<div>`` where the COMPANY NAME
  appears before the person. The old parser grabbed "Texas Skyline Roofing";
  the rewritten one must anchor to the role and return "Maria Gomez".
- ``COMPANY_BEFORE_ROLE_HTML`` — adversarial prose: the company name is the
  LAST capitalized phrase before the role, so role-anchoring alone is not
  enough — only the site-name guard rejects it.
- ``NO_PEOPLE_PAGE_HTML`` — a contact block with only a generic mailbox and
  no person; must produce zero records.
- ``TEAM_PAGE_WITH_DUPLICATE_HTML`` — the same person/role in two cards;
  must be emitted exactly once.
"""

from __future__ import annotations

TEAM_GRID_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta property="og:site_name" content="Texas Skyline Roofing" />
  <title>Texas Skyline Roofing - Our Team</title>
</head>
<body>
  <header>
    <h1>Texas Skyline Roofing</h1>
  </header>
  <main>
    <h2>Meet Our Team</h2>
    <ul class="team">
      <li class="team-member">
        <h3>Maria Gomez</h3>
        <p>Owner &amp; President</p>
        <a href="mailto:m.gomez@texasskylineco.com">m.gomez@texasskylineco.com</a>
        <p>General inquiries: <a href="mailto:info@texasskylineco.com">info@texasskylineco.com</a></p>
      </li>
      <li class="team-member">
        <h3>Jake Reed</h3>
        <p>Project Manager</p>
        <a href="mailto:j.reed@texasskylineco.com">j.reed@texasskylineco.com</a>
      </li>
      <li class="team-member">
        <h3>Sue Lee</h3>
        <p>Marketing Director</p>
        <a href="mailto:sue@texasskylineco.com">sue@texasskylineco.com</a>
      </li>
    </ul>
  </main>
  <footer>
    <div class="contact-block">
      <p>Call us or email <a href="mailto:office@texasskylineco.com">office@texasskylineco.com</a></p>
    </div>
  </footer>
</body>
</html>
"""

PROSE_TEAM_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta property="og:site_name" content="Texas Skyline Roofing" />
  <title>Texas Skyline Roofing - About Us</title>
</head>
<body>
  <main>
    <div class="about">
      <h2>Texas Skyline Roofing Company</h2>
      <p>Texas Skyline Roofing has served Dallas since 2004. The company was
      founded by Maria Gomez, who today serves as Owner and President,
      overseeing all estimating and project bidding. Reach Maria at
      <a href="mailto:m.gomez@texasskylineco.com">m.gomez@texasskylineco.com</a>.</p>
    </div>
  </main>
</body>
</html>
"""

COMPANY_BEFORE_ROLE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta property="og:site_name" content="Texas Skyline Roofing" />
  <title>Texas Skyline Roofing - Leadership</title>
</head>
<body>
  <div class="bio">
    <h2>Texas Skyline Roofing LLC</h2>
    <p>Texas Skyline Roofing is led by Owner and President Maria Gomez, who
    handles all estimating and project bidding. Contact
    <a href="mailto:m.gomez@texasskylineco.com">m.gomez@texasskylineco.com</a>.</p>
  </div>
</body>
</html>
"""

NO_PEOPLE_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Acme Roofing Tx - Contact</title>
</head>
<body>
  <main>
    <div class="contact-block">
      <p>Email us at <a href="mailto:info@acmeroofingtx.com">info@acmeroofingtx.com</a>
      or call (555) 010-1234.</p>
    </div>
  </main>
</body>
</html>
"""

TEAM_PAGE_WITH_DUPLICATE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Acme Builders - Team</title>
</head>
<body>
  <ul class="team">
    <li class="team-member"><h3>Tom Alvarez</h3><p>Estimator</p></li>
    <li class="team-member"><h3>Tom Alvarez</h3><p>Estimator</p></li>
  </ul>
</body>
</html>
"""

# --- Inc11 Step B regression fixtures (from the live-run evidence) ---------
#
# These are the actual shapes the Inc11 Step A diagnostic caught on real
# Texas roofing sites: marketing copy captured as decision-makers, and a
# mailto link whose anchor text hides the address. Each must now produce the
# RIGHT person (or none at all), never a fabricated one.

MARKETING_PROSE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Bert Roofing - About</title>
</head>
<body>
  <main>
    <div class="sales-blurb">
      <h2>Schedule No Obligation Inspection</h2>
      <p>Owned Dallas Since Honest 2004. You Back Same Day.</p>
      <p>First Name Last Name President</p>
    </div>
  </main>
</body>
</html>
"""

SENTENCE_BOUNDARY_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Arrington Roofing - Leadership</title>
</head>
<body>
  <main>
    <div class="bio">
      <p>Schedule No Obligation Inspection for homeowners in Dallas.</p>
      <p>Arrington Roofing is led by Chris Arrington, President, who runs
      all estimating and project bidding.</p>
    </div>
  </main>
</body>
</html>
"""

MAILTO_ANCHOR_TEXT_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Bert Roofing - About</title>
</head>
<body>
  <main>
    <div class="bio">
      <h2>You Back Same Day Free Estimates</h2>
      <p>John Smith, Owner and President</p>
      <a href="mailto:john.smith@bertroofing.com">Email John</a>
    </div>
  </main>
</body>
</html>
"""

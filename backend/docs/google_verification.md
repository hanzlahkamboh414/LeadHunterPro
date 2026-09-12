# Google OAuth Verification — Phase E6 (public launch)

This is the operator's checklist for taking the Gmail OAuth app from
**Testing** mode (what runs today) to a **verified production** app.
The code side of E6 is already done: public Privacy Policy
(`https://leadhuntarpro.online/privacy`) and Terms of Service
(`https://leadhuntarpro.online/terms`), reachable without logging in.

## Where we stand

| Item | Status |
|------|--------|
| OAuth client (gmail.send, gmail.readonly, openid/email/profile) | working |
| App mode | **Testing** — max 100 test users, refresh tokens expire **every 7 days** |
| Public privacy policy | done — `/privacy` |
| Public terms of service | done — `/terms` |
| HTTPS + public domain | done — leadhuntarpro.online |
| Google verification submission | **operator action (below)** |
| CASA Tier 2 assessment | **operator action — paid, see options** |

The 7-day token expiry is why accounts occasionally show "reconnect" in
Testing mode. That symptom disappears once the app is verified and in
production.

## Options (pick one)

| Option | Cost | What you get |
|--------|------|--------------|
| **A. Stay in Testing mode** | free | ≤100 users you add by email as testers; tokens die every 7 days (users must reconnect weekly). Fine for a private team. |
| **B. Verification + CASA Tier 2 self-scan** | ~US$500/year (Google-authorized lab, e.g. TAC Security's self-scan portal) | Verified production app, unlimited users, no token expiry. The self-scan is a guided automated assessment of the app's security posture — the cheap legitimate route for restricted Gmail scopes. |
| **C. Verification + full lab-led CASA Tier 2** | US$10k+ | Same result as B; only worth it if a customer/enterprise demands a lab-led report. |

Gmail scopes are **restricted** — there is no free path to an unlimited
public production app. Option B is the standard indie/SaaS route.

## Operator checklist (Google Cloud Console)

Do these in order; each is a console action, not code.

1. **Domain ownership** — Search Console (search.google.com/search-console),
   add `leadhuntarpro.online`, verify via the DNS TXT record (the DNS is
   wherever the domain is hosted). This unlocks the same verification in
   Cloud Console.
2. **OAuth consent screen** (APIs & Services → OAuth consent screen):
   - User type: External
   - App name: `LeadHunter Pro`
   - Support email + developer email: an inbox you actually monitor
     (e.g. `support@leadhuntarpro.online` — the same address as on
     `/privacy`; set this inbox up first if it doesn't exist yet)
   - App homepage: `https://leadhuntarpro.online`
   - Privacy policy: `https://leadhuntarpro.online/privacy`
   - Terms of service: `https://leadhuntarpro.online/terms`
   - Authorized domains: `leadhuntarpro.online`
3. **Scopes** — add exactly these with justifications (copy-paste):

   | Scope | Justification text (paste as-is) |
   |-------|----------------------------------|
   | `https://www.googleapis.com/auth/gmail.send` | LeadHunter Pro sends B2B outreach email campaigns on the user's behalf, composed and scheduled by the user in the app. The app sends these emails from the user's own connected Gmail account at a user-configured pace. |
   | `https://www.googleapis.com/auth/gmail.readonly` | To stop the user's follow-up email sequence when a lead replies, the app reads the From and Subject headers of the user's inbox and matches them against addresses the app itself emailed. Email bodies are never read and nothing is modified or deleted. |
   | `openid`, `email`, `profile` | Identify which Gmail account the user connected, so the app can label sending accounts correctly. |

4. **Demo video** — Google requires a short (1–2 min) YouTube video
   (unlisted is fine) showing the OAuth flow and each scope in use.
   Storyboard:
   1. Open `https://leadhuntarpro.online` → Settings → Email accounts →
      click **Connect Gmail**
   2. Google consent screen — zoom on the requested scopes
   3. Account appears as connected; click **Send test** → email arrives
      (this demonstrates `gmail.send`)
   4. Campaigns screen — create a campaign with a follow-up; show the
      "reply stops the ladder" note (this is what `gmail.readonly` is for)
   5. Settings → **Disconnect** the account — tokens deleted, access gone
   Upload, set unlisted, paste the URL in the verification form.
5. **Submit for verification** — OAuth consent screen → "Submit for
   verification". Google emails follow-ups to the support address over
   days-to-weeks; answer promptly.
6. **CASA Tier 2** — Google's email will direct you to choose an
   authorized lab. Pick the **self-scan** tier (~$500). The scan walks
   through the app's security controls; most answers for this stack:
   - Tokens encrypted at rest (Fernet) — yes
   - HTTPS everywhere — yes
   - Public privacy policy — yes (`/privacy`)
   - Data deletion on request — yes (`/privacy` §5)

## After verification

Flip **Publishing status → In production** in the consent screen. The
100-user cap and the 7-day refresh-token expiry both disappear. No code
change is needed — the backend already treats all of this as config.

## Keeping it honest

- The privacy policy's contact address must be a real, monitored inbox —
  Google sends verification mail there and users may too. If
  `support@leadhuntarpro.online` is not set up, change `CONTACT_EMAIL` in
  `frontend/src/screens/Legal.tsx` and redeploy.
- Any change to what the platform collects or reads must update `/privacy`
  in the same change — Google re-checks on re-verification (annual).

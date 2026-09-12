# Gmail OAuth Setup (Phase E2 — Connect Gmail)

LeadHunter Pro sends email through the user's own Gmail via Google's official
OAuth flow. **No Gmail password is ever seen or stored** — only an OAuth token
with the `gmail.send` permission, encrypted at rest (Fernet).

The operator (you) must create a Google Cloud project once, with your own
Google account. Total time: ~10 minutes, free.

## 1. Create the project

1. Go to <https://console.cloud.google.com/> → **Create project** (e.g. "LeadHunter Pro").
2. Billing is NOT required for OAuth or the Gmail API.

## 2. Enable the Gmail API

1. In the project, open **APIs & Services → Library**.
2. Search **Gmail API** → **Enable**.

## 3. Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. User type: **External** → Create.
3. App name: `LeadHunter Pro`; support email: yours.
4. **Scopes** → add: `.../auth/gmail.send` (and the openid/email/profile that
   come with it). Leave the rest default.
5. **Test users** → **+ Add users"** → add every Gmail that will connect
   (up to 100 in Testing mode).

> **Testing mode is fine for private use.** Google's restricted-scope
> verification (CASA Tier 2 assessment, paid) is only needed before
> onboarding the general public. In Testing mode refresh tokens expire after
> 7 days — a connected Gmail just needs a one-click reconnect weekly.
> (Phase E6 covers going to production.)

## 4. Create the OAuth client

1. **APIs & Services → Credentials → + Create credentials → OAuth client ID**.
2. Application type: **Web application**.
3. **Authorized redirect URIs** — add EXACTLY:
   ```
   https://leadhuntarpro.online/api/v1/email-accounts/google/callback
   ```
4. Create → copy the **Client ID** and **Client secret**.

## 5. Put the credentials on the server

In `/opt/leadhunter/backend/.env` on the EC2 instance:

```
GOOGLE_CLIENT_ID=<paste client id>.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=<paste client secret>
```

Optional but recommended — decouples token encryption from the JWT secret (by
default the Fernet key is derived from `AUTH_SECRET_KEY`; rotating that secret
would make stored tokens unreadable):

```
EMAIL_TOKEN_KEY=<any random url-safe base64 string, keep it stable>
```

Generate one with:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Then restart the service:

```
sudo systemctl restart leadhunter
```

## 6. Connect in the app

Settings → **Email accounts** → **+ Connect Gmail** → Google's consent page →
approve → back in LeadHunter Pro → **Send test** to prove the link end-to-end.

## Sending limits (why the scheduler stays conservative)

| Account type       | Gmail limit |
|--------------------|-------------|
| Free @gmail.com    | 500/day     |
| Google Workspace   | 2,000/day   |

These are Google's hard limits — new accounts that jump to volume land in
spam or get suspended. Phase E3's scheduler caps well below them
(~30–50/day/account) with randomized delays.

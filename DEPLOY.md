# LeadHunter Pro — Deploy

Live deployment guide for the Hostinger VPS upload is in:

- **`deploy/README.md`** — the runbook (architecture, commands, security, rollback)
- **`deploy/FILE_MANIFEST.md`** — the exact upload list + what to NEVER upload
- **`deploy/verification-checklist.md`** — post-deploy proof each layer works
- **`deploy/`** also contains `systemd/`, `nginx/`, and the `.env.production` template

> Honest prerequisite: this backend is Python FastAPI + worker threads + SQLite.
> Hostinger **shared** hosting cannot run it — a Hostinger **VPS** (≥2GB RAM,
> Ubuntu) is required.
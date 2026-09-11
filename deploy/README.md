# LeadHunter Pro — Hostinger VPS Deployment Guide

Live deployment runbook for **this project, today**. Read `FILE_MANIFEST.md`
first — it has the exact upload list plus the honest hard fact that shared
hosting can't run this backend (you need a VPS).

## What this folder contains

| File | Purpose |
|---|---|
| `FILE_MANIFEST.md` | Exact upload list + what to NEVER upload |
| `.env.production` | Production env **template** (fill real values on the VPS) |
| `systemd/leadhunter.service` | Runs the FastAPI backend as a service |
| `nginx/leadhunter.conf` | nginx: serves the built SPA + proxies `/api` → uvicorn |
| `verification-checklist.md` | Post-deploy proof each layer works |

## The architecture being deployed

```
Browser
  │  GET /                     (served from /var/www/leadhunter = frontend/dist)
  │  GET /api/v1/*  POST /api/v1/jobs
  ▼
nginx (leadhunter.conf)
  │  /api/ → proxy_pass http://127.0.0.1:8000
  ▼
uvicorn  app.main:app   (FastAPI, 1 worker — the pipeline is thread-based)
  ├── jobs table  → SQLite  backend/output/lead_research.db
  ├── search cache → SQLite backend/output/search_cache.db
  ├── runtime keys → backend/output/runtime_keys.json  (admin-set API keys)
  └── logs → journald + backend/output/backend.log  (jam-proof QueueHandler)
```

- **Why one uvicorn worker:** a Research job runs on a background *thread*
  inside the app process (`JobManager`), and research itself is a 3-thread pool
  (`LEADS_CONCURRENCY`). Raising `--workers` starts N processes all reading
  the same SQLite file — unnecessary and a lock hazard. Keep `--workers` unset.
- **Why nginx buffers off for `/api/`:** the job-progress endpoint streams
  events; buffering would stall the frontend's live History view.
- **HTTPS**: nginx config is port 80 for a fast first deploy; add
  `certbot --nginx` (in FILE_MANIFEST step 6) when the domain is ready.

## Deploy in five commands (after the upload in FILE_MANIFEST)

```bash
cd /opt/leadhunter/backend
python3 -m venv /opt/leadhunter/venv
/opt/leadhunter/venv/bin/pip install -r requirements.txt
sudo systemctl enable --now leadhunter && sudo nginx -t && sudo systemctl reload nginx
curl -s http://127.0.0.1:8000/   # expect: {"message":"LeadHunter Pro API is running",...}
```

## Security notes (read these — §1 / §12 rules)

1. **Real keys stay off any public upload.** The template `.env.production`
   contains placeholders only. The live Tavily key lives in
   `backend/output/runtime_keys.json`; move it to the VPS by SCP, never via
   File Manager (which could expose it). Same for `.env`.
2. **Do not copy your local `backend/output/*.db` to the VPS.** Those are your
   live research rows + cache. A fresh server should start an empty `output/`
   (the app creates its SQLite files on first boot). Surfaces: real search
   history, dossier data, your key file. Nothing about the project requires a
   DB transplant.
3. **GitHub-private-hosting note:** nothing with a real key may be pushed.
   `deploy/.env.production` is a template by design; `backend/output/` and
   `.env` are already git-excluded. Re-verify with `git status` before any
   push: if `backend/output/runtime_keys.json` or `.env` show up, stop.

## Known operational honesty (what to expect on the VPS)

- SQLite (not Postgres) is the app's database — fine for single-process on a
  VPS; it is cheap, file-based, and every path derives from `__file__` so the
  instance is cwd-independent.
- The AI router + search providers make LIVE network calls at run time — a
  VPS on a real public IP behaves identically to localhost. Expect the same
  per-lead latency as local.
- Frontend build: `npm run build` was run locally (this repo, `frontend/dist/`
  up to date: index.html + assets/index-*.js css). Rebuilding on the VPS is
  optional (node+npm needed); uploading the built `dist/` is enough.

## Rollback

```bash
# Stop the new backend, restart the old one (keep a copy of the prior code):
sudo systemctl stop leadhunter
# swap current backend code for the prior copy, then:
sudo systemctl start leadhunter
# nginx: unlink the vhost to take the frontend down:
sudo rm /etc/nginx/sites-enabled/leadhunter && sudo systemctl reload nginx
```
No destructive git operations are used anywhere in this runbook (CLAUDE.md §13).
# LeadHunter Pro — File Manifest for Hostinger VPS Upload

This is the exact list of what to upload to the VPS, and what to EXCLUDE.

## ⚠️ FIRST — the honest deployment fact

**Hostinger SHARED hosting cannot run this project.** The backend is a
Python FastAPI service (uvicorn + worker threads + SQLite) that listens on a
socket and must be started as a long-running process. Shared-hosting plans
(File Manager + cPanel) only serve static files and run PHP — they have no way
to keep a Python server alive. **You need a Hostinger VPS (any KVM plan, ≥2GB
RAM) with Ubuntu 22.04/24.04.** Everything below assumes that VPS.

## Upload via SCP / WinSCP (NOT the File Manager)

- **Code + config:** WinSCP to `root@VPS_IP` → `/opt/leadhunter/
- **The real `.env`:** same way (private — it has keys).
- **`output/runtime_keys.json`** (your live Tavily/router keys): SCP it to
  `/opt/leadhunter/backend/output/runtime_keys.json`. Never via File Manager.

## ✔ Upload list

| Path on your machine | → Path on VPS | Why |
|---|---|---|
| `backend/` (whole folder, code only) | `/opt/leadhunter/backend/` | FastAPI app + pipeline |
| `backend/requirements/requirements.txt` | `/opt/leadhunter/backend/requirements.txt` | Python deps |
| `frontend/dist/` (the BUILD — not `src/`) | `/var/www/leadhunter/` | SPA served by nginx |
| `deploy/.env.production` → rename to `.env`, fill real values | `/opt/leadhunter/.env` | Runtime settings |
| `deploy/systemd/leadhunter.service` | `/etc/systemd/system/` | Runs the backend |
| `deploy/nginx/leadhunter.conf` | `/etc/nginx/sites-available/leadhunter` | Front + API proxy |
| **(private)** `backend/output/runtime_keys.json` | `/opt/leadhunter/backend/output/runtime_keys.json` | Tavily key the app uses |

> **Auth (new 2026-09-10):** nothing extra to upload. The login system creates
> `backend/output/users.db` on first boot and seeds `admin4269` / `223344`.
> Set a strong `AUTH_SECRET_KEY` in the `.env` (see `env.production`) BEFORE
> the first `systemctl start` — JWTs are signed with it.

## ✖ NEVER upload these (secrets/local data)

| Path | Why excluded |
|---|---|
| `backend/.env` (local) | Real DB paths / any local keys — recreate as `.env` on the VPS from the template |
| `backend/output/*.db`, `*.db-wal`, `*.db-shm` | **Local research data.** Start the VPS with a CLEAN output/ (SQLite + cache it just for you). Uploading your local DB would leak your leads/serch history to the server and can include stale absolute local paths. |
| `backend/output/runtime_keys.json` **if** it went through any public route | It holds your Tavily key — SCP it privately, never File Manager. |
| `frontend/node_modules/`, `frontend/src/`, `frontend/dist` if rebuilt on VPS | Not needed at runtime; the built `dist/` is enough. |
| `.git/`, `.claude/`, `bugs/`, `queries/`, `Assets/`, `.pytest_tmp/` | Dev/tooling only; never ship. |
| `backend/tests/`, `backend/scripts/` (optional) | Not needed at runtime (keep them if you want to run the suite on the VPS). |

## After upload — on the VPS

```bash
# 1. System packages + Python
sudo apt update && sudo apt install -y python3-venv python3-pip nginx

# 2. Backend venv + deps
cd /opt/leadhunter/backend
python3 -m venv /opt/leadhunter/venv
/opt/leadhunter/venv/bin/pip install -r requirements.txt

# 3. Ensure output/ exists and is writable by the service
mkdir -p /opt/leadhunter/backend/output /var/www/leadhunter

# 4. Runtime keys (private upload done above) — sanity check they read:
test -f /opt/leadhunter/backend/output/runtime_keys.json && echo "keys present"

# 5. systemd + nginx
# (The deploy/* files you uploaded were staged under /opt/leadhunter/deploy/)
sudo cp /opt/leadhunter/deploy/systemd/leadhunter.service /etc/systemd/system/leadhunter.service
sudo systemctl daemon-reload && sudo systemctl enable --now leadhunter
sudo cp /opt/leadhunter/deploy/nginx/leadhunter.conf /etc/nginx/sites-available/leadhunter
sudo ln -sf /etc/nginx/sites-available/leadhunter /etc/nginx/sites-enabled/leadhunter
sudo sed -i 's/server_name example.com;/server_name YOUR_DOMAIN_OR_IP;/' /etc/nginx/sites-available/leadhunter
sudo nginx -t && sudo systemctl reload nginx

# 6. Verify
sudo systemctl status leadhunter --no-pager          # active (running)
curl -s http://127.0.0.1:8000/                       # {"message":"LeadHunter Pro API is running",...}
curl -s http://YOUR_IP/ | head                       # SPA HTML
# (Optional) HTTPS: sudo apt install -y certbot python3-certbot-nginx && sudo certbot --nginx -d YOUR_DOMAIN
```

## Test after deploy

1. `curl -s http://127.0.0.1:8000/api/v1/jobs | head` → `[]` or your jobs (API up).
2. Open `http://YOUR_IP/` → frontend loads, no console 404s on `/api/*`.
3. Start one small search (target 5) on the Research screen → History shows live progress events; results appear.
4. `sudo journalctl -u leadhunter -f` → backend logs stream (jam-proof queue → file sink also at `backend/output/backend.log`).
# LeadHunter Pro — Post-Deploy Verification Checklist

Run these on the VPS in order after `deploy/FILE_MANIFEST.md` finishes. One
step at a time; only fix what proves broken (§CLAUDE.md Manual Verification).

## Step 1 — Backend process is alive

```bash
sudo systemctl status leadhunter --no-pager
```
**PASS:** `active (running)`, no restart-loop in the last 30s.

## Step 2 — API answers

```bash
curl -s http://127.0.0.1:8000/
```
**PASS:** JSON with `"message": "LeadHunter Pro API is running"`.

## Step 3 — Job endpoints work end-to-end

```bash
curl -s http://127.0.0.1:8000/api/v1/jobs | head -c 200
curl -s -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{"trade":"general contractor","location":"Houston TX","target_emails":5}'
```
**PASS:** first returns a JSON array (possibly `[]`); second returns a `job_id`
with `state: "queued"` or `"running"`.

## Step 4 — Logs stream (jam-proof path)

```bash
sudo journalctl -u leadhunter -n 20 --no-pager
```
**PASS:** `setup_logging` boot lines + research/discovery INFO lines. Also confirm
`tail -5 /opt/leadhunter/backend/output/backend.log` grows (the rotating file
sink that never depends on a console).

## Step 5 — Frontend over nginx

```bash
curl -s http://YOUR_IP/ | head -20     # → <!doctype html> … index.html
curl -s -o /dev/null -w "%{http_code}" http://YOUR_IP/api/v1/leads/jobs   # → 401 (auth wall = healthy)
```
**PASS:** SPA HTML served; the proxied API answers 401 WITHOUT a token (the
auth wall working) — not 404/502 (proxy broken).

## Step 6 — Real research smoke (honest data, small target)

1. Open `http://YOUR_IP/` → you land on **Login**. Log in as `admin4269` /
   `223344`, then change the password (Login → Forgot password).
2. Research screen: run `general contractor` · `Houston TX` · target **5**.
3. Watch History: `queued → running`, progress events stream, then `completed`
   (or an honest shortfall with a reason — never a fake full-delivery).
4. Open a result's detail: dossier has company + person + recommendation.

**PASS:** a real completed run with ≥1 working lead (or an honest
shortfall_reason — both are truthful).

## Optional — HTTPS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d YOUR_DOMAIN
```
**PASS:** `https://YOUR_DOMAIN` loads with a valid cert; mixed-content errors
are gone in the browser console.

---

If any step FAILs: read `sudo journalctl -u leadhunter -n 50` / backend.log,
fix the specific proven cause, re-run that ONE step. Do not skip ahead.
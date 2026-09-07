# LeadHunter Pro — Frontend

React + TypeScript + Vite + Tailwind CSS SPA for the LeadHunter Pro Leads API
(Stage 3), styled to the founder's dashboard theme (`#0B0E14` + indigo, lucide
icons).

## Screens

- **Dashboard** (`/`) — live stats computed from the API (total companies,
  bound contacts, contact-now count, average score — no fake numbers), recent
  runs, and the top buying-window leads.
- **Research / Execute** (`/research`) — submit a discovery + AI-research query
  and watch the job run live (TanStack Query polling of `GET /leads/jobs/{id}`),
  with a cancel button and a running list of qualified leads as they land.
- **Companies / Contacts** (`/leads`, `/contacts`) — table of researched leads
  with recommendation / bound / min-score filters, a live topbar search
  (`/leads?q=…`), and CSV export.
- **Lead Detail** (`/leads/:email`) — full dossier with the evidence trail:
  company, decision-maker, intent, timing, and sources audited. Every claim
  links to its `source_url`.
- **Run History** (`/history`) — past jobs; expand one to see its results and
  progress log.
- **Settings** (`/settings`) — optional API key.

## Run

```bash
npm install
npm run dev        # http://localhost:5173
```

The Vite dev server proxies `/api` → `http://127.0.0.1:8000` (the FastAPI
backend). Override with `VITE_API_TARGET=http://host:port npm run dev`.

If the backend sets `LEADS_API_KEY`, enter that key in Settings / on the
Research screen — it is sent as the `X-API-Key` header (persisted to
localStorage).

## Build

```bash
npm run build      # tsc type-check + vite production build -> dist/
```

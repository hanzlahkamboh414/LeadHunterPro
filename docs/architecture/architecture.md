# LeadHunter Pro — System Architecture

## Overview

LeadHunter Pro is an AI-powered construction lead intelligence platform. The system discovers construction companies from public sources, enriches them with AI-generated insights, scores their fit, and produces outreach-ready reports.

The backend is a **Python FastAPI** service that follows Clean Architecture and SOLID principles. The frontend (planned) will be a React + Electron desktop application.

---

## Technology Stack

| Layer          | Technology                        |
|----------------|-----------------------------------|
| API Framework  | FastAPI                           |
| ORM            | SQLAlchemy 2.x                    |
| Validation     | Pydantic v2                       |
| Database       | PostgreSQL                        |
| AI Providers   | OpenAI, Anthropic, Gemini, Ollama (local) |
| Embeddings     | FAISS                             |
| Formatting     | Black + Ruff                      |
| Logging        | Python `logging` (structured)     |

---

## Repository Layout

```
backend/
├── app/
│   ├── api/v1/          # HTTP routes and request handling
│   ├── core/            # Settings, constants, logging, exceptions
│   ├── database/        # SQLAlchemy engine, session factory
│   ├── models/          # SQLAlchemy ORM models
│   ├── schemas/         # Pydantic request/response schemas
│   ├── repositories/    # Data access layer
│   ├── services/        # Business logic orchestration
│   ├── engines/         # Specialised business engines
│   │   ├── discovery/company/     # Public-source company discovery
│   │   ├── source_intelligence/   # Source-based intelligence planning
│   │   └── source_connectors/     # SDK + individual source connectors
│   ├── research/website/    # Website crawling & parsing
│   ├── ai/                  # AI provider gateway and tools
│   ├── scoring/             # Company scoring heuristics + AI
│   ├── email/               # Email discovery & validation
│   └── reports/             # Report generation
├── migrations/              # Alembic migration scripts
└── tests/                   # Unit and integration tests
```

---

## Layer Dependencies

```
api/v1  →  services  →  engines / repositories  →  database
                         ↓
                       ai/           (one-way: engines → AI, never reverse)
```

- **API layer** handles routing, input validation, and response formatting only.
- **Services** contain business logic and orchestrate engines/repositories.
- **Engines** are domain-specific (discovery, scoring, research). They never touch the database directly.
- **Repositories** encapsulate all database access.
- **AI module** is invoked *by* engines/services; it never calls back into those layers.

---

## Core Modules

### Configuration (`core/config.py`)

Uses Pydantic `BaseSettings` loaded from `.env`. Key settings:

- `DATABASE_URL` — PostgreSQL connection string (required)
- `AI_PROVIDER` — `"openai"`, `"anthropic"`, `"gemini"`, or `"local"` (defaults to local via Ollama)
- `OPENAI_API_KEY`, `OLLAMA_URL` — provider credentials/endpoints
- `LOG_LEVEL` — logging verbosity

### Database (`database/`)

- `base.py` — SQLAlchemy `DeclarativeBase` subclass. All models inherit from it.
- `session.py` — Creates the engine and a `SessionLocal` factory; provides the `get_db()` FastAPI dependency.

### ORM Models (`models/`)

| Model        | Table        | Purpose                                |
|-------------|-------------|----------------------------------------|
| `Company`   | `companies` | Discovered/found companies             |
| `Contact`   | `contacts`  | People associated with a company       |
| `Research`  | `research`  | Captured website data + AI summaries   |
| `Task`      | `tasks`     | Asynchronous job tracking (future)     |
| `Campaign`  | `campaigns` | Outreach campaign container (future)   |

Relationships:
- `Company` ↔ `Contact` (one-to-many, cascade delete)
- `Company` ↔ `Research` (one-to-many, cascade delete)

### API Routes (`api/v1/`)

All routes are registered through `router.py` under the `/api/v1` prefix. See [architecture.md](architecture.md) for the full endpoint inventory.

---

## Enterprise Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        FastAPI App                          │
│  ┌──────────┐  ┌───────────┐  ┌────────────┐  ┌─────────┐ │
│  │ Companies│  │ Discovery │  │ Research   │  │ Connect │ │
│  │ Endpoints│  │ Endpoints │  │ Endpoints  │  │ Endpoints││
│  └────┬─────┘  └─────┬─────┘  └─────┬─────┘  └────┬────┘ │
│       │              │               │              │       │
│  ┌────▼──────────────▼───────────────▼──────────────▼────┐ │
│  │                 Services Layer                          │ │
│  │  company_service · email_service · crawler_service     │ │
│  │  leadership_service · research_service                 │ │
│  └──────────────────────┬─────────────────────────────────┘ │
│                         │                                   │
│  ┌──────────────────────▼─────────────────────────────────┐ │
│  │                 Engines Layer                            │ │
│  │  company_engine · ai_engine · phone_engine             │ │
│  │  social_engine · website_engine                        │ │
│  │  discovery/company/  source_intelligence/               │ │
│  │  source_connectors/                                    │ │
│  └──────────────────────┬─────────────────────────────────┘ │
│                         │                                   │
│  ┌──────────────────────▼─────────────────────────────────┐ │
│  │              Data Access Layer                           │ │
│  │  repositories/  ←→  database/session.py                │ │
│  └──────────────────────┬─────────────────────────────────┘ │
│                         │                                   │
│                  ┌──────▼──────┐                            │
│                  │ PostgreSQL  │                            │
│                  └─────────────┘                            │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              AI Module (one-way out)                  │   │
│  │  ai/gateway.py → ai/manager.py → ai/providers/       │   │
│  │  ai/scorer.py · ai/summarizer.py                     │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## Design Principles

1. **One responsibility per module** — no folder or class handles more than its defined domain.
2. **No circular imports** — enforced during architecture freeze (Sprint 1.2).
3. **Dependency inversion** — API and services depend on abstractions; repositories implement them.
4. **AI isolation** — the AI module is called *by* engines; engines never call AI back.
5. **No placeholder implementations** — every produced file is production-ready.

---

## Current Sprint Status

| Sprint | Title                              | Status   |
|--------|------------------------------------|----------|
| 1.1    | Foundation Infrastructure          | ✅ Done  |
| 1.2    | Architecture Freeze                | ✅ Done  |
| 2.1    | Company Discovery Engine           | ✅ Done  |
| 2.2    | Connector SDK                      | 🔄 In Prog |

See individual sprint files in [`docs/sprints/`](../sprints/) for details.

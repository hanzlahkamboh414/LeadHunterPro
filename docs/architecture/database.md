# Database Schema

## Overview

LeadHunter Pro uses **PostgreSQL** as its relational database, managed through **SQLAlchemy 2.x** with **Alembic** for migrations.

Connection is configured via the `DATABASE_URL` environment variable (see [architecture/architecture.md](architecture.md)).

---

## ER Diagram

```
┌──────────────┐       ┌──────────────┐       ┌──────────────┐
│   companies  │       │   contacts   │       │   research   │
├──────────────┤       ├──────────────┤       ├──────────────┤
│ id (PK)      │───┐   │ id (PK)      │   ┌───│ id (PK)      │
│ company_name │   └──►│ company_id(FK)│   │   │ company_id(FK)│
│ website (UNQ)│       │ full_name    │   │   │ raw_data     │
│ industry     │       │ job_title    │   │   │ ai_summary   │
│ headquarters │       │ email (UNQ)  │   │   │ created_at   │
│ employee_count│      │ linkedin_url │   │   └──────────────┘
│ ai_score     │       │ phone        │
│ completed    │       │ verified     │
│ created_at   │       └──────────────┘
└──────────────┘

(future)
┌──────────────┐       ┌──────────────┐
│   campaigns  │       │    tasks     │
├──────────────┤       ├──────────────┤
│ id (PK)      │       │ id (PK)      │
│ name         │       │ type         │
│ company_id(FK)│      │ payload (JSON)│
│ status       │       │ status       │
│ created_at   │       │ result       │
└──────────────┘       └──────────────┘
```

---

## Tables

### `companies`

Stores discovered or manually added companies.

| Column             | Type         | Constraints            | Notes                         |
|--------------------|--------------|------------------------|-------------------------------|
| `id`               | Integer      | PK, auto-increment     |                               |
| `company_name`     | String(255)  | NOT NULL               |                               |
| `website`          | String(255)  | UNIQUE, NOT NULL       | Canonical URL for dedup       |
| `industry`         | String(100)  | NULLABLE               | e.g. "Construction Estimating"|
| `headquarters`     | String(255)  | NULLABLE               | City/state/country            |
| `employee_count`   | Integer      | NULLABLE               |                               |
| `ai_score`         | Integer      | NOT NULL, default 0    | 0–100 lead score              |
| `research_completed`| Boolean     | NOT NULL, default False| Set True when research finishes|
| `created_at`       | DateTime     | NOT NULL, default now  | UTC timestamp                 |

**Relationships:**
- One-to-many → `contacts` (cascade delete)
- One-to-many → `research` (cascade delete)

---

### `contacts`

Individual people found at a company.

| Column         | Type         | Constraints                        | Notes                  |
|----------------|--------------|------------------------------------|------------------------|
| `id`           | Integer      | PK                                 |                        |
| `company_id`   | Integer      | FK → companies.id, CASCADE DELETE  |                        |
| `full_name`    | String(255)  | NOT NULL                           |                        |
| `job_title`    | String(255)  | NULLABLE                           |                        |
| `email`        | String(255)  | UNIQUE                             | Verified flag separate |
| `linkedin_url` | String(500)  | NULLABLE                           |                        |
| `phone`        | String(100)  | NULLABLE                           |                        |
| `verified`     | Boolean      | DEFAULT False                      | Manual or automated    |

---

### `research`

Captures crawled website content and AI-generated summaries.

| Column         | Type    | Constraints             | Notes                              |
|----------------|---------|-------------------------|------------------------------------|
| `id`           | Integer | PK                      |                                    |
| `company_id`   | Integer | FK → companies.id, CASCADE DELETE |                            |
| `raw_data`     | JSON    | NOT NULL                | All scraped fields as a dict       |
| `ai_summary`   | JSON    | NULLABLE                | AI-generated summary dict          |
| `created_at`   | DateTime| NOT NULL, default now   | UTC timestamp                      |

---

### Planned Tables (not yet implemented)

| Table       | Purpose                              |
|-------------|--------------------------------------|
| `campaigns` | Group companies for an outreach push |
| `tasks`     | Async job queue entries              |

---

## Migration Strategy

Migrations are managed by Alembic under `backend/migrations/`. Each sprint that changes the schema should produce a new migration file.

Key models and their tables were approved during **Sprint 1.2 – Architecture Freeze**:

> Approved: Company, Leadership (→ contacts), Research, Evidence (→ research), LeadIntelligence (→ ai_score on companies)

Any model outside this list requires explicit review before merging.

---

## Connection Pattern

```python
from app.database.session import get_db, SessionLocal

# In FastAPI routes (dependency injection)
def my_endpoint(db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.website == url).first()
    ...
```

Always use the injected `db` session; never create your own connection in service code.

# Sprint 1.1 -- Foundation Infrastructure

## Objective

Build the production-grade foundation for **LeadHunter Pro**.

------------------------------------------------------------------------

## General Rules

-   Python 3.12+
-   FastAPI
-   SQLAlchemy 2.x
-   Pydantic v2
-   PostgreSQL
-   Clean Architecture
-   SOLID Principles
-   Type hints everywhere
-   Google-style docstrings
-   Black formatting
-   Ruff compatible
-   No duplicate code
-   No placeholder implementations
-   No `print()` statements
-   Use structured logging
-   Every module must be production-ready

------------------------------------------------------------------------

## Project Structure

``` text
backend/
    app/
        core/
            config.py
            settings.py
            constants.py
            logging.py

        database/
            base.py
            session.py

        models/
            __init__.py
```

------------------------------------------------------------------------

## Tasks

1.  Build a professional configuration system.
2.  Implement environment variable support using Pydantic Settings.
3.  Configure centralized logging.
4.  Create SQLAlchemy Base.
5.  Create SQLAlchemy Session.
6.  Configure PostgreSQL connection.
7.  Build the production-ready folder structure.
8.  Add proper docstrings to every file.
9.  Use dependency injection where appropriate.
10. Do **not** implement business logic yet.

------------------------------------------------------------------------

## Acceptance Criteria

The sprint is complete only if:

-   Project starts successfully.
-   FastAPI imports without errors.
-   Database connection is configured.
-   Logging is working.
-   `.env` configuration is supported.
-   Foundation is ready for future AI modules.

------------------------------------------------------------------------

## Instructions for Claude Code

Implement only the foundation described above.

Do **not** create business logic, AI modules, crawlers, parsers, or
repositories in this sprint.

Generate production-quality code file by file.

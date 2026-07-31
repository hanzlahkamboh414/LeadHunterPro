# LeadHunter Pro - CLAUDE.md
Version: 1.0
Status: LOCKED
Owner: Hanzlah (Product Owner)
Architect: ChatGPT (CTO)

---

# PROJECT MISSION

LeadHunter Pro is NOT a CRM.

LeadHunter Pro is NOT a web scraper.

LeadHunter Pro is NOT an email finder.

LeadHunter Pro is an AI-powered Lead Intelligence Platform built specifically for Construction Estimating companies.

Mission:

Find 20–25 high-quality opportunities that have the highest probability of needing construction estimating services.

Success Metric:

20 Leads
↓

1–2 Paying Clients

Everything inside this project must contribute toward this mission.

If a feature does not improve lead quality,
DO NOT BUILD IT.

---

# CORE PHILOSOPHY

Simple beats Complex.

Reliable beats Clever.

Evidence beats Assumption.

Quality beats Quantity.

Architecture beats Speed.

Never sacrifice architecture for short-term progress.

---

# DEVELOPMENT RULES

Claude NEVER decides architecture.

Claude NEVER creates new folders.

Claude NEVER creates extra modules.

Claude NEVER changes folder structure.

Claude ONLY implements approved sprint specifications.

If something is missing,
STOP
and ask.

Never invent.

---

# ARCHITECTURE

Layers

API

↓

Services

↓

Repositories

↓

Database

Independent Engines

↓

AI

Dependencies always point downward.

Never create circular imports.

Never bypass repositories.

Never access database directly from API.

---

# FOLDER RESPONSIBILITIES

## api/

HTTP endpoints only.

No business logic.

No database logic.

No AI.

---

## services/

Business logic only.

Coordinates repositories and engines.

---

## repositories/

Database access only.

No business logic.

No AI.

---

## database/

Database session.

Migrations.

Base.

Connection.

Nothing else.

---

## models/

ORM models only.

---

## schemas/

Pydantic schemas only.

---

## crawler/

Website crawling.

HTML download.

No parsing.

No AI.

---

## email/

Email discovery.

Email validation.

Email cleaning.

---

## ai/

AI Providers.

Prompt templates.

Gateway.

Manager.

Never crawl.

Never scrape.

Never access database.

---

## engines/

The intelligence layer.

Contains:

Discovery

Research

Compiler

Intelligence

Ranking

Each engine has ONE responsibility.

---

# DISCOVERY ENGINE

Purpose

Find companies.

Allowed

Google Search

Bing

Directories

Government websites

Bid portals

News

Hiring

Forbidden

Lead Scoring

AI Reasoning

Database writes

---

# RESEARCH ENGINE

Purpose

Research one company deeply.

Collect

Website

Leadership

Emails

Phones

Projects

News

Services

Technology

Hiring

Output

Structured Evidence

---

# EVIDENCE COMPILER

Python only.

NO AI.

Responsibilities

Remove duplicates

Normalize data

Remove useless information

Build structured JSON

Prepare AI input

---

# AI INTELLIGENCE ENGINE

Input

Structured Evidence

Output

Need Probability

Confidence

Reasoning

Outreach Angle

Summary

AI NEVER performs crawling.

AI NEVER performs scraping.

AI NEVER invents data.

---

# RANKING ENGINE

Ranks leads.

Chooses Top Leads.

No crawling.

No AI prompting.

---

# DATABASE RULES

Current Core Tables

Company

Leadership

Research

Evidence

LeadIntelligence

No additional tables without approval.

---

# CODING STANDARDS

Python 3.12+

FastAPI

SQLAlchemy 2.x

Pydantic v2

PostgreSQL

Type hints everywhere

Google Docstrings

Black formatting

Ruff compatible

Structured logging

No print()

No TODO

No placeholder code

Production quality only.

---

# IMPORT RULES

Never use wildcard imports.

Avoid circular imports.

Prefer dependency injection.

Keep modules loosely coupled.

---

# ERROR HANDLING

Raise meaningful exceptions.

Never silently ignore errors.

Always log unexpected failures.

---

# TESTING RULES

Every sprint must pass

Syntax

Imports

API

Database

Basic integration

before moving forward.

No broken code allowed.

---

# GIT RULES

One Sprint

↓

One Commit

Commit Message Example

Sprint 2.1 Company Discovery Completed

Never mix multiple features into one commit.

---

# SPRINT RULES

Claude implements ONLY the current sprint.

Never implement future sprint files.

Never create speculative code.

---

# PERFORMANCE RULES

Optimize after correctness.

Never prematurely optimize.

Readable code is preferred.

---

# SECURITY RULES

Never hardcode secrets.

Always use environment variables.

Validate every input.

Never trust external data.

---

# FINAL RULE

If architecture and implementation conflict,

Architecture ALWAYS wins.

If sprint instructions conflict with CLAUDE.md,

STOP and ask for clarification.

Never guess.

---

END OF DOCUMENT

This document is the Constitution of LeadHunter Pro.

Every future sprint must follow it.
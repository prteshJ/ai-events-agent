# AI Events Agent

A tiny service that:
1) reads your **Gmail UNREAD** emails,  
2) turns them into simple **events**,  
3) stores them in **Neon Postgres**,  
4) lets you **list / search / view** the events via a REST API.

> Built to be minimal: FastAPI + SQLAlchemy + Postgres.  
> No LLM required. You can add one later if you want.

---

## How it works (at a glance)

- **Inbox** → `inbox.py`  
  Reads **UNREAD** Gmail messages (read-only) using OAuth **refresh token**.

- **Parser** → `parser.py`  
  Rule-based extraction (e.g., “Daily Standup”, “Client Kickoff”) to a normalized event.

- **Storage** → `storage.py`  
  Saves events in Neon (Postgres) via SQLAlchemy ORM.

- **API** → `app.py`  
  Endpoints to import emails (admin), list, search, and fetch events.

---

## What you need

- A **Neon** Postgres database (connection string)
- A **Railway** app to run the service
- A **Google Cloud** project with **Gmail API** enabled

---

## Environment variables (Railway → Variables)

Required:

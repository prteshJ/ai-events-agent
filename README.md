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

---

## 🗺️ Roadmap

These are small, safe improvements planned for upcoming versions:

| Priority | Feature | Why it matters |
|-----------|----------|----------------|
| ✅ short term | **ICS attachment parsing** | Many event invites arrive as `.ics` files — parsing them ensures perfect time and location data. |
| ✅ short term | **Idempotency (dedup)** | Prevents duplicate events when re-importing the same Gmail messages. |
| ⏳ medium term | **Time zone normalization** | Standardize all event times to UTC in the database for consistent querying. |
| ⏳ medium term | **Web UI for review** | A lightweight admin page to manually confirm uncertain events. |
| 🧠 long term | **LLM fallback (OpenAI/Gemini)** | Use an AI model only for hard-to-parse or ambiguous emails, returning clean JSON. |
| 🧩 long term | **Multi-source support** | Import events not only from Gmail but also from ICS links, Slack, or calendar APIs. |

> The goal: keep the system simple, predictable, and cheap —  
> add "smarts" only when they clearly save time or errors.


# AI Events Agent

Simple service that:
1) reads emails (mock for now),
2) turns them into events,
3) stores them in Neon Postgres,
4) lets you search and view the events.

## How it works (v1)
- **Inbox**: `inbox.py` returns 3 mock emails (no Gmail needed yet).
- **Parser**: `parser.py` turns each email into an event (standup, kickoff, notes).
- **Storage**: `storage.py` saves events to Neon using SQLAlchemy.
- **API**: `app.py` exposes read/search endpoints and an admin import route.

## Run (Railway)
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT --proxy-headers --lifespan on`
- Health: `/health`
- Env vars:
  - `DATABASE_URL` — Neon connection string
  - `ADMIN_BEARER` (or `BEARER_TOKEN`) — token for admin route

## Endpoints

**Health**
- `GET /` – service check
- `GET /health` – liveness
- `GET /health/db` – DB health and latency

**Events (Read)**
- `GET /events` – list events (sorted by start)
- `GET /events/{id}` – get one event by id
- `GET /events/search` – search events  
  Query params:
  - `q` (optional) – text search in title/description/location
  - `start_from`, `start_to` (optional) – ISO datetime or `YYYY-MM-DD`
  - `exclude_recurring` (default: `true`)
  - `limit`, `offset`

**Admin (Write)**
- `POST /events/import` – import emails → parse → save events  
  Requires header: `Authorization: Bearer <ADMIN_BEARER>`

  ## Gmail (Unread Inbox) — Setup & Env

**Goal:** Let the app read *your* UNREAD Gmail messages safely (read-only).

### What you need (once)
- Google Cloud project with **Gmail API** enabled
- OAuth **Web application** client (name anything)
- Add redirect URI: `https://developers.google.com/oauthplayground`
- Add yourself under **Audience → Test users** (new UI)
- Use **OAuth 2.0 Playground** to get a **Refresh token** with scope:


## Quick test

```bash
# 1) Import mock emails (creates events in Neon)
curl -X POST "https://<your-app>/events/import" \
  -H "Authorization: Bearer <ADMIN_BEARER>"

# 2) List events
curl "https://<your-app>/events?limit=10"

# 3) Search
curl "https://<your-app>/events/search?q=standup"

# 4) Get by id (use an id from the list)
curl "https://<your-app>/events/<id>"



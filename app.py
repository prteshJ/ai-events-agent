"""
app.py — AI Events Agent (FastAPI + Railway + Neon)
===================================================

What this service does (plain English)
--------------------------------------
1) Starts a tiny FastAPI server.
2) Ensures Postgres tables exist at startup (via SQLAlchemy).
3) Provides health checks so your platform (Railway) knows it's alive.
4) Reads "emails" from a mock inbox, parses them into events, and stores them.
5) Exposes simple endpoints to list, search, and fetch events.

Why this file exists / what's special
------------------------------------
- Some event IDs can contain '#'. Browsers treat '#' as a URL fragment and
  *don't send it to the server* in the path. To keep the database untouched:
  - We keep /events/{id} (works if client URL-encodes '#' as '%23').
  - We add /events/by-id?id=... which is **URL-safe** in browsers (query
    parameters are auto-encoded by the browser).

Optional "browser import"
-------------------------
- POST /events/import uses a Bearer token (best practice).
- For demos, you can enable GET /events/import-web?token=... by setting:
    ENABLE_IMPORT_WEB=true
  This lets non-technical folks trigger an import from a browser.
  Turn it off after demos (set to false or remove).

Environment variables you should set
------------------------------------
- ADMIN_BEARER (or BEARER_TOKEN): the admin token for imports
- DATABASE_URL: your Neon/Postgres connection string
- ENABLE_IMPORT_WEB: "true" or "false" (default false)

Endpoints overview
------------------
- GET  /health                  -> quick liveness check
- GET  /health/db               -> DB connectivity + event count
- GET  /events                  -> list events (paginated)
- GET  /events/{id}             -> fetch one event by its ID (URL-encode '#'!)
- GET  /events/by-id?id=...     -> fetch one event by query param (browser-safe)
- GET  /events/search           -> simple text/time search
- POST /events/import           -> mock inbox -> parse -> upsert (admin only)
- GET  /events/import-web       -> same import via ?token= (only if enabled)

Implementation notes
--------------------
- Storage is in `storage.py` (SQLAlchemy model + session helpers).
- Parsing is in `parser.py` (email -> normalized event dict(s)).
- Inbox is in `inbox.py` (mocked async fetch).
"""

import os
import re
import time
import traceback
from contextlib import suppress
from datetime import datetime
from typing import Optional, List

# Optional .env support for local dev; harmless if missing in prod
with suppress(Exception):
    from dotenv import load_dotenv
    load_dotenv()

from fastapi import FastAPI, Depends, HTTPException, Request, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, text

# Local modules (provided in your repo)
from storage import init_db, get_db, Event as EventModel
from parser import parse_email_to_events
from inbox import get_inbox

# ------------------------------------------------------------------------------
# Configuration (from environment)
# ------------------------------------------------------------------------------
# Admin token for protected routes (POST /events/import). Prefer setting ADMIN_BEARER.
ADMIN_BEARER = os.getenv("BEARER_TOKEN") or os.getenv("ADMIN_BEARER") or "change-me"

# Optional: enable a browser-friendly import URL for demos (GET /events/import-web?token=...)
ENABLE_IMPORT_WEB = os.getenv("ENABLE_IMPORT_WEB", "false").lower() in ("1", "true", "yes")

app = FastAPI(
    title="AI Events Agent",
    version="1.0.2",
    description="Reads emails → extracts event info → stores in Postgres → simple API.",
)

# ------------------------------------------------------------------------------
# Middleware: tiny request logger (handy in Railway logs)
# ------------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    ms = round((time.time() - start) * 1000)
    print(f"{request.method} {request.url.path} → {response.status_code} [{ms}ms]")
    # Useful to see which git SHA is running in logs/responses
    response.headers["X-Rev"] = os.getenv("RAILWAY_GIT_COMMIT_SHA", "dev")
    return response

# ------------------------------------------------------------------------------
# Startup: ensure tables exist (non-fatal if it fails; you'll see logs)
# ------------------------------------------------------------------------------
@app.on_event("startup")
def startup():
    try:
        print("[startup] init_db()")
        init_db()
        print("[startup] done")
    except Exception:
        print("[startup] init_db failed (non-fatal):")
        traceback.print_exc()

# ------------------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------------------
def require_bearer(request: Request):
    """Require 'Authorization: Bearer <token>' and validate against ADMIN_BEARER."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    token = auth.removeprefix("Bearer ").strip()
    if token != ADMIN_BEARER:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Bearer token")

def require_token_param(token: Optional[str]):
    """For GET /events/import-web (browser demo). Only allowed if ENABLE_IMPORT_WEB=true."""
    if not ENABLE_IMPORT_WEB:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="import-web is disabled")
    if not token or token != ADMIN_BEARER:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

# ------------------------------------------------------------------------------
# Small utility: accept either ISO datetime or YYYY-MM-DD and normalize to ISO
# ------------------------------------------------------------------------------
def _iso_or_date(dt: Optional[str]) -> Optional[str]:
    if not dt:
        return None
    with suppress(Exception):
        return datetime.fromisoformat(dt).isoformat()
    with suppress(Exception):
        return datetime.strptime(dt, "%Y-%m-%d").date().isoformat()
    raise HTTPException(status_code=400, detail=f"Invalid datetime/date: {dt}")

# ------------------------------------------------------------------------------
# Health endpoints (useful for platform checks and quick verification)
# ------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return {"msg": "ai-events-agent is running", "docs": "/docs", "health": "/health"}

@app.get("/health", include_in_schema=False)
async def health():
    return {"ok": True}

@app.get("/health/db", include_in_schema=False)
async def health_db():
    """Try a quick DB query; return current event count and latency."""
    start = time.time()
    count: Optional[int] = None
    session: Optional[Session] = None
    try:
        session = next(get_db())
        session.execute(text("SELECT 1"))  # connectivity check
        count = session.query(EventModel).count()
    except Exception as e:
        print(f"[health/db] probe failed: {e}")
    finally:
        with suppress(Exception):
            if session is not None:
                session.close()
    ms = round((time.time() - start) * 1000)
    return {"ok": count is not None, "events": count, "duration_ms": ms}

# ------------------------------------------------------------------------------
# Pydantic response model (maps ORM instance → JSON)
# ------------------------------------------------------------------------------
class EventOut(BaseModel):
    id: str
    title: str
    start: Optional[str] = None
    end: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    recurring: bool
    recurrence_rule: Optional[str] = None
    source_type: str
    source_message_id: str
    source_snippet: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True  # allow Pydantic to read from SQLAlchemy model attributes

# ------------------------------------------------------------------------------
# Events: read/list/search
# ------------------------------------------------------------------------------
@app.get("/events", response_model=List[EventOut], summary="List events")
def list_events(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List events (sorted by start; nulls last)."""
    return (
        db.query(EventModel)
        .order_by(EventModel.start.asc().nulls_last(), EventModel.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

@app.get("/events/{event_id}", response_model=EventOut, summary="Get event by ID")
def get_event(event_id: str, db: Session = Depends(get_db)):
    """
    Fetch a single event by its primary key (path parameter).
    NOTE: If your ID contains '#', clients MUST URL-encode it as '%23', or use /events/by-id.
    """
    # Try primary-key lookup first (fast path)
    obj = None
    with suppress(Exception):
        obj = db.get(EventModel, event_id)
    # Fallback to explicit filter (covers some SQLAlchemy versions/engines)
    if obj is None:
        obj = db.query(EventModel).filter(EventModel.id == event_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Event not found")
    return obj

@app.get("/events/by-id", response_model=EventOut, summary="Get event by ID (URL-safe)")
def get_event_by_query(id: str = Query(..., description="Exact event id"), db: Session = Depends(get_db)):
    """
    Browser-safe way to fetch by ID (query params are auto-encoded by browsers).
    Use this if IDs can contain characters like '#'.
    """
    obj = db.get(EventModel, id)
    if not obj:
        obj = db.query(EventModel).filter(EventModel.id == id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Event not found")
    return obj

@app.get("/events/search", response_model=List[EventOut], summary="Search events (non-recurring by default)")
def search_events(
    q: Optional[str] = Query(None, description="Search in title/description/location"),
    start_from: Optional[str] = Query(None, description="ISO datetime or YYYY-MM-DD"),
    start_to: Optional[str] = Query(None, description="ISO datetime or YYYY-MM-DD"),
    exclude_recurring: bool = Query(True, description="Exclude recurring events"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Simple text + time range search. Times are compared as ISO strings (UTC)."""
    sf = _iso_or_date(start_from)
    st = _iso_or_date(start_to)

    query = db.query(EventModel)
    if exclude_recurring:
        query = query.filter(or_(EventModel.recurring == False, EventModel.recurring.is_(None)))  # noqa: E712
    if sf:
        query = query.filter(EventModel.start >= sf)
    if st:
        query = query.filter(EventModel.start <= st)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                EventModel.title.ilike(like),
                EventModel.description.ilike(like),
                EventModel.location.ilike(like),
            )
        )

    return (
        query.order_by(EventModel.start.asc().nulls_last(), EventModel.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

# ------------------------------------------------------------------------------
# Events: admin import (mock inbox → parser → DB)
# ------------------------------------------------------------------------------
@app.post("/events/import", summary="Import emails → parse → save events (admin)")
async def import_emails(request: Request, db: Session = Depends(get_db)):
    """
    Admin-only (requires Bearer token).
    1) Fetch emails from mock inbox (async).
    2) Parse emails into normalized events.
    3) Upsert into Postgres by primary key (id).
    """
    require_bearer(request)
    return await _do_import(db)

@app.get("/events/import-web", include_in_schema=False)
async def import_emails_web(token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """
    Optional (disabled by default). Enable by setting ENABLE_IMPORT_WEB=true.
    Lets you trigger an import from a browser using:
      GET /events/import-web?token=<ADMIN_BEARER>
    Use for demos; turn off afterwards.
    """
    require_token_param(token)
    return await _do_import(db)

# Shared import implementation (used by both routes)
async def _do_import(db: Session):
    # 1) Read "emails" from the mock inbox
    try:
        emails = await get_inbox()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to read inbox: {e}")

    if not emails:
        print("[import] inbox returned 0 messages")

    # 2) Parse each email into 0..N events; 3) Upsert each event
    written = 0
    for mail in emails:
        try:
            events = parse_email_to_events(mail) or []
            for e in events:
                evt = EventModel(
                    id=e["_id"],  # IMPORTANT: we DO NOT modify the ID (Neon stays untouched)
                    title=e["title"] or "Untitled",
                    start=e.get("start"),   # stored as ISO string (UTC)
                    end=e.get("end"),
                    location=e.get("location"),
                    description=e.get("description"),
                    recurring=bool(e.get("recurring")),
                    recurrence_rule=e.get("recurrence_rule"),
                    source_type=e.get("source_type") or "mock",
                    source_message_id=e.get("source_message_id") or "unknown",
                    source_snippet=e.get("source_snippet"),
                )
                db.merge(evt)   # merge = upsert by primary key
                written += 1
        except Exception as e:
            # Keep importing even if one email fails; log and continue
            print(f"[import] parse/save error for message {mail.get('id')}: {e}")

    db.commit()
    return {"ok": True, "emails": len(emails), "events_written": written}

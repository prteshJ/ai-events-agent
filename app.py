"""
app.py — AI Events Agent (FastAPI + Railway + Neon)

What this service does (in plain english)
-----------------------------------------
1) Starts a small FastAPI server.
2) Creates the database tables if they don't exist.
3) Has health endpoints so Railway knows the app is alive.
4) Exposes event endpoints:
   - GET  /events           → list events
   - GET  /events/{id}      → get a single event
   - GET  /events/search    → search events (non-recurring by default)
   - POST /events/import    → read mock inbox → parse → save events (admin only)

How “AI agent” works in v1
--------------------------
- We simulate email reading using a mock inbox (inbox.py).
- We parse those emails into events (parser.py).
- We store events in Neon Postgres (storage.py).
- Later, you can swap the mock inbox for real Gmail without changing endpoints.

Auth for admin route
--------------------
- Set ADMIN_BEARER (or BEARER_TOKEN) in Railway.
- Call POST /events/import with header:  Authorization: Bearer <token>
"""

import os
import re
import time
import hashlib
import traceback
import asyncio
from contextlib import suppress
from datetime import datetime
from typing import Optional, List

# .env support (safe if missing)
with suppress(Exception):
    from dotenv import load_dotenv
    load_dotenv()

from fastapi import FastAPI, Depends, HTTPException, Request, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, text

# our modules
from storage import init_db, get_db, Event as EventModel
from parser import parse_email_to_events
from inbox import get_inbox

# config
ADMIN_BEARER = os.getenv("BEARER_TOKEN") or os.getenv("ADMIN_BEARER") or "change-me"

app = FastAPI(
    title="AI Events Agent",
    version="1.0.0",
    description="Reads emails → extracts event info → stores in Postgres → simple API.",
)

# --------------------------------------------------
# Logging middleware (helps in Railway logs)
# --------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration_ms = round((time.time() - start_time) * 1000)
    print(f"{request.method} {request.url.path} → {response.status_code} [{duration_ms}ms]")
    response.headers["X-Rev"] = os.getenv("RAILWAY_GIT_COMMIT_SHA", "dev")
    return response

# --------------------------------------------------
# Startup: create tables if needed
# --------------------------------------------------
@app.on_event("startup")
def startup():
    try:
        print("[startup] init_db()")
        init_db()
        print("[startup] done")
    except Exception:
        print("[startup] init_db failed (non-fatal):")
        traceback.print_exc()

# --------------------------------------------------
# Auth helper for admin routes
# --------------------------------------------------
def require_bearer(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    token = auth.removeprefix("Bearer ").strip()
    if token != ADMIN_BEARER:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Bearer token")

# --------------------------------------------------
# Small utils (kept for future)
# --------------------------------------------------
_slugify_re = re.compile(r"[^a-z0-9]+")
def slugify(s: str) -> str:
    s = (s or "").lower()
    s = _slugify_re.sub("-", s).strip("-")
    return s or "untitled"

def make_event_id(source_type: str, source_message_id: str, title: str, index: int | None = None) -> str:
    base = f"{source_type or 'mock'}::{source_message_id or 'unknown'}::{title or 'untitled'}"
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    suffix = f"-{index}" if index is not None else ""
    return f"{(source_type or 'mock')}-{slugify(source_message_id)}-{slugify(title)}-{h}{suffix}"

def _probe_db_count() -> Optional[int]:
    session: Optional[Session] = None
    try:
        session = next(get_db())
        session.execute(text("SELECT 1"))
        return session.query(EventModel).count()
    except Exception as e:
        print(f"[health/db] probe failed: {e}")
        return None
    finally:
        with suppress(Exception):
            if session is not None:
                session.close()

def _iso_or_date(dt: Optional[str]) -> Optional[str]:
    """Accept ISO datetime or YYYY-MM-DD. Return normalized ISO string."""
    if not dt:
        return None
    with suppress(Exception):
        return datetime.fromisoformat(dt).isoformat()
    with suppress(Exception):
        return datetime.strptime(dt, "%Y-%m-%d").date().isoformat()
    raise HTTPException(status_code=400, detail=f"Invalid datetime/date: {dt}")

# --------------------------------------------------
# Health endpoints
# --------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return {"msg": "ai-events-agent is running", "docs": "/docs", "health": "/health"}

@app.get("/health", include_in_schema=False)
async def health():
    return {"ok": True}

@app.get("/health/db", include_in_schema=False)
async def health_db():
    start = time.time()
    count = _probe_db_count()
    duration = round((time.time() - start) * 1000)
    return {"ok": count is not None, "events": count, "duration_ms": duration}

# --------------------------------------------------
# Response model (maps ORM → JSON)
# --------------------------------------------------
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
        from_attributes = True

# --------------------------------------------------
# Events: read endpoints
# --------------------------------------------------
@app.get("/events", response_model=List[EventOut], summary="List events")
def list_events(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List events found so far (sorted by start)."""
    return (
        db.query(EventModel)
        .order_by(EventModel.start.asc().nulls_last(), EventModel.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

@app.get("/events/{event_id}", response_model=EventOut, summary="Get event by ID")
def get_event(event_id: str, db: Session = Depends(get_db)):
    """Return one event by its id."""
    obj = None
    with suppress(Exception):
        obj = db.get(EventModel, event_id)
    if obj is None:
        obj = db.query(EventModel).filter(EventModel.id == event_id).first()
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
    """Search events with simple filters. Uses ISO strings for time comparisons."""
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

# --------------------------------------------------
# Events: admin import (mock inbox → parser → db)
# --------------------------------------------------
@app.post("/events/import", summary="Import emails → parse → save events (admin)")
def import_emails(request: Request, db: Session = Depends(get_db)):
    """
    Admin-only:
    - Fetch emails (mock inbox for now).
    - Parse emails into events.
    - Upsert (merge) into DB by primary key (id).
    """
    require_bearer(request)

    # get_inbox() is async; run it fully
    emails = asyncio.get_event_loop().run_until_complete(get_inbox())

    for mail in emails:
        events = parse_email_to_events(mail) or []
        for e in events:
            evt = EventModel(
                id=e["_id"],  # parser uses _id → we store as id
                title=e["title"] or "Untitled",
                start=e.get("start"),  # stored as ISO string (v1)
                end=e.get("end"),
                location=e.get("location"),
                description=e.get("description"),
                recurring=bool(e.get("recurring")),
                recurrence_rule=e.get("recurrence_rule"),
                source_type=e.get("source_type") or "mock",
                source_message_id=e.get("source_message_id") or "unknown",
                source_snippet=e.get("source_snippet"),
            )
            db.merge(evt)  # merge = upsert by primary key
    db.commit()
    return {"ok": True}

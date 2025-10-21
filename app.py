Here’s the final `app.py` — same behavior as your working setup, plus the **smallest Swagger enhancement** (an Authorize button for the admin POST). Everything else stays as-is.

```python
"""
AI Events Agent — FastAPI + Railway + Neon
==========================================

Purpose
-------
- Reads "emails" (mock inbox) → parses into structured events → saves in Neon (Postgres)
- Exposes a simple REST API to list, search, and fetch events
- Includes health checks and an optional browser import for demos

Environment Variables
---------------------
- ADMIN_BEARER (or BEARER_TOKEN): admin token for imports
- DATABASE_URL: Neon connection string
- ENABLE_IMPORT_WEB: "true" to enable GET /events/import-web (browser demo)

Endpoints
---------
Health:
  GET  /health
  GET  /health/db
  GET  /robots.txt
  GET  /favicon.ico

Events:
  GET  /events
  GET  /events/id/{event_id}     ← unambiguous, no shadowing risk
  GET  /events/by-id?id=...      ← browser-safe; accepts URL-encoded IDs
  GET  /events/search

Admin:
  POST /events/import             ← protected via Swagger "Authorize" (Bearer)
  GET  /events/import-web?token=...   (if ENABLE_IMPORT_WEB=true)

Swagger Enhancement
-------------------
- Adds a simple HTTP Bearer security scheme so Swagger shows the "Authorize" button.
- Only /events/import uses this scheme (everything else unchanged).
"""

import os
import time
import traceback
from contextlib import suppress
from datetime import datetime
from typing import Optional, List

# Optional .env support for local dev
with suppress(Exception):
    from dotenv import load_dotenv
    load_dotenv()

from fastapi import FastAPI, Depends, HTTPException, Request, Query, Security
from fastapi.responses import PlainTextResponse, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, text

# Local modules
from storage import init_db, get_db, Event as EventModel
from parser import parse_email_to_events
from inbox import get_inbox

# ------------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------------
ADMIN_BEARER = os.getenv("BEARER_TOKEN") or os.getenv("ADMIN_BEARER") or "change-me"
ENABLE_IMPORT_WEB = os.getenv("ENABLE_IMPORT_WEB", "false").lower() in ("1", "true", "yes")

app = FastAPI(
    title="AI Events Agent",
    version="1.0.6",
    description="Reads emails → extracts event info → stores in Postgres → simple API.",
)

# ------------------------------------------------------------------------------
# Middleware: simple request logger (shows up in Railway logs)
# ------------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    ms = round((time.time() - start) * 1000)
    print(f"{request.method} {request.url.path} → {response.status_code} [{ms}ms]")
    response.headers["X-Rev"] = os.getenv("RAILWAY_GIT_COMMIT_SHA", "dev")
    return response

# ------------------------------------------------------------------------------
# Startup: ensure DB tables exist
# ------------------------------------------------------------------------------
@app.on_event("startup")
def startup():
    try:
        print("[startup] init_db()")
        init_db()
        print("[startup] done")
    except Exception:
        # Keep going so /health still responds; surface the issue in logs
        print("[startup] init_db failed:")
        traceback.print_exc()

# ------------------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------------------
# Browser demo token check (unchanged)
def require_token_param(token: Optional[str]):
    """For GET /events/import-web (browser demo). Only allowed if ENABLE_IMPORT_WEB=true."""
    if not ENABLE_IMPORT_WEB:
        raise HTTPException(status_code=403, detail="import-web is disabled")
    if not token or token != ADMIN_BEARER:
        raise HTTPException(status_code=401, detail="Invalid token")

# Swagger Bearer auth (adds the Authorize button, used only on POST /events/import)
bearer_scheme = HTTPBearer(auto_error=True)

def verify_admin(credentials: HTTPAuthorizationCredentials = Security(bearer_scheme)):
    token = (credentials.credentials or "").strip()
    if token != ADMIN_BEARER:
        raise HTTPException(status_code=401, detail="Invalid Bearer token")

# ------------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------------
def _iso_or_date(dt: Optional[str]) -> Optional[str]:
    """Accept ISO datetime or YYYY-MM-DD, normalize to ISO string."""
    if not dt:
        return None
    with suppress(Exception):
        return datetime.fromisoformat(dt).isoformat()
    with suppress(Exception):
        return datetime.strptime(dt, "%Y-%m-%d").date().isoformat()
    raise HTTPException(status_code=400, detail=f"Invalid datetime/date: {dt}")

# ------------------------------------------------------------------------------
# Health + misc
# ------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return {"msg": "ai-events-agent is running", "docs": "/docs", "health": "/health"}

@app.get("/health", include_in_schema=False)
async def health():
    return {"ok": True}

@app.get("/health/db", include_in_schema=False)
async def health_db():
    """Connectivity check + event count + latency."""
    start = time.time()
    count: Optional[int] = None
    session: Optional[Session] = None
    try:
        session = next(get_db())
        session.execute(text("SELECT 1"))
        count = session.query(EventModel).count()
    except Exception as e:
        print(f"[health/db] probe failed: {e}")
    finally:
        with suppress(Exception):
            if session is not None:
                session.close()
    ms = round((time.time() - start) * 1000)
    return {"ok": count is not None, "events": count, "duration_ms": ms}

@app.get("/robots.txt", include_in_schema=False, response_class=PlainTextResponse)
async def robots_txt():
    # Reduce log noise from bots and uptime monitors
    return "User-agent: *\nDisallow:\n"

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # 204 = No Content; prevents 404 spam in logs
    return Response(status_code=204)

# ------------------------------------------------------------------------------
# Schemas
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
        from_attributes = True

# ------------------------------------------------------------------------------
# Events — define specific routes FIRST, then the unambiguous ID route LAST
# ------------------------------------------------------------------------------

@app.get("/events", response_model=List[EventOut], summary="List events")
def list_events(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return (
        db.query(EventModel)
        .order_by(EventModel.start.asc().nulls_last(), EventModel.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

@app.get("/events/by-id", response_model=EventOut, summary="Get event by ID (URL-safe)")
def get_event_by_query(
    id: str = Query(..., description="Exact event id"),
    db: Session = Depends(get_db),
):
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
# Admin (write)
# ------------------------------------------------------------------------------
@app.post("/events/import", summary="Import emails → parse → save events (admin)")
async def import_emails(
    db: Session = Depends(get_db),
    _: None = Depends(verify_admin),  # ← Swagger "Authorize" enables Bearer here
):
    return await _do_import(db)

@app.get("/events/import-web", summary="Browser import demo (token required)")
async def import_emails_web(token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    require_token_param(token)
    return await _do_import(db)

async def _do_import(db: Session):
    """Shared import implementation."""
    try:
        emails = await get_inbox()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to read inbox: {e}")

    written = 0
    for mail in emails:
        try:
            events = parse_email_to_events(mail) or []
            for e in events:
                evt = EventModel(
                    id=e["_id"],                 # keep IDs exactly as produced by parser
                    title=e["title"] or "Untitled",
                    start=e.get("start"),
                    end=e.get("end"),
                    location=e.get("location"),
                    description=e.get("description"),
                    recurring=bool(e.get("recurring")),
                    recurrence_rule=e.get("recurrence_rule"),
                    source_type=e.get("source_type") or "mock",
                    source_message_id=e.get("source_message_id") or "unknown",
                    source_snippet=e.get("source_snippet"),
                )
                db.merge(evt)                   # upsert by primary key
                written += 1
        except Exception as e:
            # Keep importing others; log the failure
            print(f"[import] parse/save error for message {mail.get('id')}: {e}")

    db.commit()
    return {"ok": True, "emails": len(emails), "events_written": written}

# ------------------------------------------------------------------------------
# Get-by-ID (unambiguous path) — defined LAST to avoid any accidental shadowing
# ------------------------------------------------------------------------------
@app.get("/events/id/{event_id}", response_model=EventOut, summary="Get event by ID")
def get_event(event_id: str, db: Session = Depends(get_db)):
    """
    Fetch a single event by its primary key (path parameter).
    Using /events/id/{event_id} avoids shadowing /events/<fixed-routes>.
    """
    obj = db.get(EventModel, event_id)
    if not obj:
        obj = db.query(EventModel).filter(EventModel.id == event_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Event not found")
    return obj
```

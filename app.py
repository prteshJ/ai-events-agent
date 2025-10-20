# app.py — Main FastAPI Application (resilient startup + safe health checks)
#
# Endpoints:
#   GET  /            → friendly message
#   GET  /health      → ALWAYS fast, no DB
#   GET  /health/db   → optional DB probe (non-fatal)
#   GET  /events
#   GET  /events/recurring
#   GET  /events/nonrecurring
#   GET  /events/search
#   POST /scan        → Bearer protected
#
# Env (Railway):
#   DATABASE_URL  = <Neon URL> (postgresql://…)
#   BEARER_TOKEN  or ADMIN_BEARER

import os
import re
import hashlib
from contextlib import suppress
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, text

from storage import init_db, get_db, Event as EventModel
from parser import parse_email_to_events
from inbox import get_inbox

# -----------------------------------------------------------------------------
# config
# -----------------------------------------------------------------------------
ADMIN_BEARER = os.getenv("BEARER_TOKEN") or os.getenv("ADMIN_BEARER") or "change-me"

# -----------------------------------------------------------------------------
# app
# -----------------------------------------------------------------------------
app = FastAPI(
    title="AI Events Agent",
    version="0.1.0",
    description="Reads emails → extracts event info → stores in Postgres → simple API.",
)

# -----------------------------------------------------------------------------
# schema (response model)
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# startup (best-effort: don't block the server if DB is unreachable)
# -----------------------------------------------------------------------------
@app.on_event("startup")
def startup():
    try:
        init_db()  # should create engine/tables if needed
        print("[startup] init_db completed")
    except Exception as e:
        # log but DO NOT crash the app — /health stays responsive
        print(f"[startup] init_db failed (non-fatal): {e}")

# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------
def require_bearer(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    token = auth.removeprefix("Bearer ").strip()
    if token != ADMIN_BEARER:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Bearer token")

_slugify_re = re.compile(r"[^a-z0-9]+")
def slugify(s: str) -> str:
    s = (s or "").lower()
    s = _slugify_re.sub("-", s).strip("-")
    return s or "untitled"

def make_event_id(source_type: str, source_message_id: str, title: str, index: int | None = None) -> str:
    """
    Stable, short id that matches storage.Event primary key (str).
    Combines source_type, message id, and title; appends index for multi-parsed events.
    """
    base = f"{source_type or 'mock'}::{source_message_id or 'unknown'}::{title or 'untitled'}"
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    suffix = f"-{index}" if index is not None else ""
    return f"{(source_type or 'mock')}-{slugify(source_message_id)}-{slugify(title)}-{h}{suffix}"

# small helper to test DB without blocking the app if it fails
def _probe_db_count() -> Optional[int]:
    session: Optional[Session] = None
    try:
        # get_db() is a generator dependency; pull one session manually
        session = next(get_db())
        # lightweight probe (avoids ORM layer work)
        session.execute(text("SELECT 1"))
        # optional: count for visibility (can remove if you want it ultra-fast)
        count = session.query(EventModel).count()
        return count
    except Exception as e:
        print(f"[health/db] probe failed: {e}")
        return None
    finally:
        with suppress(Exception):
            if session is not None:
                session.close()

# -----------------------------------------------------------------------------
# friendly root
# -----------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return {"msg": "ai-events-agent is running", "docs": "/docs", "health": "/health"}

# -----------------------------------------------------------------------------
# health (ALWAYS fast, NO DB)
# -----------------------------------------------------------------------------
@app.get("/health", include_in_schema=False)
def health():
    # purely app-level health so Railway edge doesn't time out
    return {"ok": True}

# Optional DB health (non-fatal, for your own checks)
@app.get("/health/db", include_in_schema=False)
def health_db():
    count = _probe_db_count()
    return {"ok": count is not None, "events": count}

# -----------------------------------------------------------------------------
# events — list
# -----------------------------------------------------------------------------
@app.get("/events", response_model=List[EventOut])
def list_events(db: Session = Depends(get_db)):
    return db.query(EventModel).order_by(EventModel.start.asc().nulls_last()).all()

@app.get("/events/recurring", response_model=List[EventOut])
def list_recurring(db: Session = Depends(get_db)):
    return (
        db.query(EventModel)
        .filter(EventModel.recurring.is_(True))
        .order_by(EventModel.start.asc().nulls_last())
        .all()
    )

@app.get("/events/nonrecurring", response_model=List[EventOut])
def list_nonrecurring(db: Session = Depends(get_db)):
    return (
        db.query(EventModel)
        .filter(EventModel.recurring.is_(False))
        .order_by(EventModel.start.asc().nulls_last())
        .all()
    )

# search
@app.get("/events/search", response_model=List[EventOut])
def search_events(
    q: Optional[str] = Query(default=None),
    recurring: Optional[bool] = Query(default=None),
    db: Session = Depends(get_db),
):
    query = db.query(EventModel)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                EventModel.title.ilike(like),
                EventModel.description.ilike(like),
                EventModel.location.ilike(like),
                EventModel.source_snippet.ilike(like),
            )
        )
    if recurring is True:
        query = query.filter(EventModel.recurring.is_(True))
    elif recurring is False:
        query = query.filter(EventModel.recurring.is_(False))
    return query.order_by(EventModel.start.asc().nulls_last()).all()

# -----------------------------------------------------------------------------
# scan — bearer protected
# -----------------------------------------------------------------------------
@app.post("/scan")
def scan(request: Request, db: Session = Depends(get_db)):
    require_bearer(request)

    messages = get_inbox()  # list of mock email dicts/objects
    parsed_events_total = 0
    created = updated = skipped = 0

    for msg in messages:
        try:
            results = parse_email_to_events(msg) or []
        except Exception as e:
            print(f"[scan] parse failed for message {getattr(msg, 'id', None)}: {e}")
            continue

        for idx, ev in enumerate(results):
            parsed_events_total += 1
            data = dict(ev)
            title = data.get("title") or "Untitled"
            source_type = data.get("source_type") or "mock"
            source_message_id = data.get("source_message_id") or getattr(msg, "id", "unknown")
            recurring = bool(data.get("recurring", False))

            eid = data.get("id") or make_event_id(
                source_type, source_message_id, title, idx if len(results) > 1 else None
            )

            existing = (
                db.query(EventModel)
                .filter(
                    EventModel.source_message_id == source_message_id,
                    EventModel.title == title,
                )
                .first()
            )

            if existing:
                for k in [
                    "start", "end", "location", "description",
                    "recurring", "recurrence_rule", "source_type", "source_snippet",
                ]:
                    if k in data and data[k] is not None:
                        setattr(existing, k, data[k])
                existing.updated_at = datetime.utcnow()
                updated += 1
            else:
                model = EventModel(
                    id=eid,
                    title=title,
                    start=data.get("start"),
                    end=data.get("end"),
                    location=data.get("location"),
                    description=data.get("description"),
                    recurring=recurring,
                    recurrence_rule=data.get("recurrence_rule"),
                    source_type=source_type,
                    source_message_id=source_message_id,
                    source_snippet=data.get("source_snippet"),
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                db.add(model)
                created += 1

    db.commit()
    total = db.query(EventModel).count()
    return {
        "ok": True,
        "parsed": parsed_events_total,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total_events": total,
    }

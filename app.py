# app.py — Main FastAPI Application (super simple & well documented)
#
# Endpoints:
#   GET  /health
#   GET  /events
#   GET  /events/recurring
#   GET  /events/nonrecurring
#   GET  /events/search
#   POST /scan    (Bearer protected)
#
# Env (Railway):
#   DATABASE_URL  = <Neon URL>  (postgresql://… OR postgresql+psycopg://…)
#   BEARER_TOKEN  = alpha-12345 (or use ADMIN_BEARER; both supported)

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
from sqlalchemy import or_

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
# startup
# -----------------------------------------------------------------------------
@app.on_event("startup")
def startup():
    init_db()

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
    # short hash to keep id length safe
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    suffix = f"-{index}" if index is not None else ""
    return f"{(source_type or 'mock')}-{slugify(source_message_id)}-{slugify(title)}-{h}{suffix}"

# -----------------------------------------------------------------------------
# friendly root
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    return {"msg": "ai-events-agent is running. See /health and /docs"}

# -----------------------------------------------------------------------------
# health
# -----------------------------------------------------------------------------
@app.get("/health")
def health(db: Session = Depends(get_db)):
    with suppress(Exception):
        count = db.query(EventModel).count()
        return {"ok": True, "events": count}
    return JSONResponse(status_code=500, content={"ok": False, "events": 0})

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
            # normalize
            data = dict(ev)
            title = data.get("title") or "Untitled"
            source_type = data.get("source_type") or "mock"
            source_message_id = data.get("source_message_id") or getattr(msg, "id", "unknown")
            recurring = bool(data.get("recurring", False))

            # generate REQUIRED primary key id to match storage.Event
            eid = data.get("id") or make_event_id(source_type, source_message_id, title, idx if len(results) > 1 else None)

            # look for an existing row (upsert key)
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
                    id=eid,  # <-- required
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

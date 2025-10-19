# app.py — Main FastAPI Application (super simple & well documented)
#
# What this file does
# -------------------
# ✅ Provides clean API endpoints:
#     GET  /health                 → check DB connection + event count
#     GET  /events                 → list all events
#     GET  /events/recurring       → recurring only
#     GET  /events/nonrecurring    → one-time only
#     GET  /events/search          → search with optional recurrence filter
#     POST /scan                   → protected endpoint that scans emails (mock for now)
#
# ✅ Connects to:
#     - inbox.py    → mock email reader
#     - parser.py   → turns emails into event info
#     - storage.py  → stores events in Postgres (Neon)
#
# Environment variables (set in Railway)
# --------------------------------------
# DATABASE_URL  = your Neon connection string (postgresql+psycopg://…)
# BEARER_TOKEN  = alpha-12345                (our docs/snippets use this)
# ADMIN_BEARER  = (alternative name supported for convenience)
#
# Start command on Railway:
# uvicorn app:app --host 0.0.0.0 --port $PORT

import os
from contextlib import suppress
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_

# local modules
from storage import init_db, get_db, Event as EventModel
from parser import parse_email_to_events
from inbox import get_inbox

# ---------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------
# Support BOTH names so your Railway var can be either one
ADMIN_BEARER = os.getenv("BEARER_TOKEN") or os.getenv("ADMIN_BEARER") or "change-me"

# ---------------------------------------------------------------------
# FastAPI app setup
# ---------------------------------------------------------------------
app = FastAPI(
    title="AI Events Agent",
    version="0.1.0",
    description="Reads emails → extracts event info → stores in Postgres → simple API.",
)

# ---------------------------------------------------------------------
# Pydantic response model (what your API returns)
# ---------------------------------------------------------------------
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
        from_attributes = True  # tell FastAPI how to read SQLAlchemy objects


# ---------------------------------------------------------------------
# startup hook — create tables
# ---------------------------------------------------------------------
@app.on_event("startup")
def startup():
    init_db()


# ---------------------------------------------------------------------
# tiny helper: bearer auth gate for /scan
# ---------------------------------------------------------------------
def require_bearer(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    token = auth.removeprefix("Bearer ").strip()
    if token != ADMIN_BEARER:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Bearer token")


# ---------------------------------------------------------------------
# friendly root (stops 404s on "/")
# ---------------------------------------------------------------------
@app.get("/")
def root():
    return {"msg": "ai-events-agent is running. See /health and /docs"}


# ---------------------------------------------------------------------
# /health — shows DB connectivity + event count
# ---------------------------------------------------------------------
@app.get("/health")
def health(db: Session = Depends(get_db)):
    with suppress(Exception):
        count = db.query(EventModel).count()
        return {"ok": True, "events": count}
    # if querying fails, report degraded status
    return JSONResponse(status_code=500, content={"ok": False, "events": 0})


# ---------------------------------------------------------------------
# /events — list all
# ---------------------------------------------------------------------
@app.get("/events", response_model=List[EventOut])
def list_events(db: Session = Depends(get_db)):
    return db.query(EventModel).order_by(EventModel.start.asc().nulls_last()).all()


# ---------------------------------------------------------------------
# /events/recurring — list recurring only
# ---------------------------------------------------------------------
@app.get("/events/recurring", response_model=List[EventOut])
def list_recurring(db: Session = Depends(get_db)):
    return (
        db.query(EventModel)
        .filter(EventModel.recurring.is_(True))
        .order_by(EventModel.start.asc().nulls_last())
        .all()
    )


# ---------------------------------------------------------------------
# /events/nonrecurring — list one-time only
# ---------------------------------------------------------------------
@app.get("/events/nonrecurring", response_model=List[EventOut])
def list_nonrecurring(db: Session = Depends(get_db)):
    return (
        db.query(EventModel)
        .filter(EventModel.recurring.is_(False))
        .order_by(EventModel.start.asc().nulls_last())
        .all()
    )


# ---------------------------------------------------------------------
# /events/search — free text + optional recurring filter
# ---------------------------------------------------------------------
@app.get("/events/search", response_model=List[EventOut])
def search_events(
    q: Optional[str] = Query(default=None, description="Search text in title/description/location"),
    recurring: Optional[bool] = Query(default=None, description="True for recurring, False for one-time"),
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


# ---------------------------------------------------------------------
# /scan — protected; reads mock inbox → parse → store
# ---------------------------------------------------------------------
@app.post("/scan")
def scan(request: Request, db: Session = Depends(get_db)):
    # Auth
    require_bearer(request)

    # Read inbox (mock)
    messages = get_inbox()  # expect a list of email-like objects/dicts

    # Parse emails to events (list of dicts)
    parsed_events = []
    for msg in messages:
        try:
            results = parse_email_to_events(msg) or []
            parsed_events.extend(results)
        except Exception as e:
            # skip problematic emails but continue the scan
            print(f"[scan] parse failed for message {getattr(msg, 'id', None)}: {e}")

    # Store events; upsert by (source_message_id + title) to avoid duplicates
    created, updated, skipped = 0, 0, 0
    for ev in parsed_events:
        # normalize dict shape
        data = dict(ev)

        # required minimal fields with safe defaults
        title = data.get("title") or "Untitled"
        source_message_id = data.get("source_message_id") or "unknown"
        recurring = bool(data.get("recurring", False))

        # find existing
        existing = (
            db.query(EventModel)
            .filter(
                EventModel.source_message_id == source_message_id,
                EventModel.title == title,
            )
            .first()
        )

        if existing:
            # update a few fields
            for k in [
                "start",
                "end",
                "location",
                "description",
                "recurring",
                "recurrence_rule",
                "source_type",
                "source_snippet",
            ]:
                if k in data and data[k] is not None:
                    setattr(existing, k, data[k])
            updated += 1
        else:
            # construct new model
            model = EventModel(
                title=title,
                start=data.get("start"),
                end=data.get("end"),
                location=data.get("location"),
                description=data.get("description"),
                recurring=recurring,
                recurrence_rule=data.get("recurrence_rule"),
                source_type=data.get("source_type") or "mock",
                source_message_id=source_message_id,
                source_snippet=data.get("source_snippet"),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(model)
            created += 1

    db.commit()

    # final count for convenience
    total = db.query(EventModel).count()
    return {"ok": True, "created": created, "updated": updated, "skipped": skipped, "total_events": total}

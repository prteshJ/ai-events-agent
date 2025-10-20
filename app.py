# app.py — Main FastAPI Application for AI Events Agent

import os
import re
import time
import hashlib
import traceback
from contextlib import suppress
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, text
from dotenv import load_dotenv

# Load environment variables for local dev
load_dotenv()

from storage import init_db, get_db, Event as EventModel
from parser import parse_email_to_events
from inbox import get_inbox

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
ADMIN_BEARER = os.getenv("BEARER_TOKEN") or os.getenv("ADMIN_BEARER") or "change-me"
PORT = int(os.getenv("PORT", "8080"))  # Railway assigns $PORT dynamically

# --------------------------------------------------------------------------
# FastAPI App
# --------------------------------------------------------------------------
app = FastAPI(
    title="AI Events Agent",
    version="0.1.0",
    description="Reads emails → extracts event info → stores in Neon Postgres → exposes API.",
)

# --------------------------------------------------------------------------
# Middleware (log requests)
# --------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = round((time.time() - start_time) * 1000)
    print(f"{request.method} {request.url.path} → {response.status_code} [{duration}ms]")
    return response

# --------------------------------------------------------------------------
# Response Schema
# --------------------------------------------------------------------------
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

# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    try:
        print("[startup] calling init_db()")
        init_db()
        print("[startup] init_db completed successfully")
    except Exception:
        print("[startup] init_db failed (non-fatal):")
        traceback.print_exc()

# --------------------------------------------------------------------------
# Auth helper
# --------------------------------------------------------------------------
def require_bearer(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    token = auth.removeprefix("Bearer ").strip()
    if token != ADMIN_BEARER:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Bearer token")

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
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
        count = session.query(EventModel).count()
        return count
    except Exception as e:
        print(f"[health/db] probe failed: {e}")
        return None
    finally:
        with suppress(Exception):
            if session is not None:
                session.close()

# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return {
        "msg": "✅ AI Events Agent is running successfully!",
        "docs": "/docs",
        "health": "/health",
    }

@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

@app.get("/health/db", include_in_schema=False)
def health_db():
    start = time.time()
    count = _probe_db_count()
    duration = round((time.time() - start) * 1000)
    return {"ok": count is not None, "events": count, "duration_ms": duration}

@app.get("/events", response_model=List[EventOut])
def list_events(db: Session = Depends(get_db)):
    return db.query(EventModel).order_by(EventModel.start.asc().nulls_last()).all()

@app.get("/events/recurring", response_model=List[EventOut])
def list_recurring(db: Session = Depends(get_db)):
    return db.query(EventModel).filter(EventModel.recurring.is_(True)).order_by(EventModel.start.asc().nulls_last()).all()

@app.get("/events/nonrecurring", response_model=List[EventOut])
def list_nonrecurring(db: Session = Depends(get_db)):
    return db.query(EventModel).filter(EventModel.recurring.is_(False)).order_by(EventModel.start.asc().nulls_last()).all()

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

@app.post("/scan")
def scan(request: Request, db: Session = Depends(get_db)):
    require_bearer(request)
    messages = get_inbox()
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
            eid = data.get("id") or make_event_id(source_type, source_message_id, title, idx if len(results) > 1 else None)

            existing = db.query(EventModel).filter(
                EventModel.source_message_id == source_message_id,
                EventModel.title == title,
            ).first()

            if existing:
                for k in ["start", "end", "location", "description", "recurring", "recurrence_rule", "source_type", "source_snippet"]:
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
    return {"parsed": parsed_events_total, "created": created, "updated": updated, "skipped": skipped}

# --------------------------------------------------------------------------
# Local dev entrypoint (used when running locally)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)

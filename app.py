"""
app.py — AI Events Agent (FastAPI + Railway + Neon)

Purpose
-------
Main FastAPI service that:
1. Initializes the database on startup.
2. Provides basic health endpoints for Railway.
3. (Later) Reads inbox → parses emails → stores events.

Design
------
- Framework: FastAPI
- Database: PostgreSQL (via SQLAlchemy)
- Deployment: Railway + Neon
- Entry point: uvicorn app:app --host 0.0.0.0 --port $PORT

Notes
-----
- Uses python-dotenv for local .env loading, but doesn’t crash if missing.
- Keeps health endpoints extremely lightweight for 200 OK liveness checks.
- DB init failures are logged but non-fatal to prevent 502s on boot.
"""

import os
import re
import time
import hashlib
import traceback
from contextlib import suppress
from datetime import datetime
from typing import List, Optional

# --------------------------------------------------
# Load .env safely (non-fatal if python-dotenv missing)
# --------------------------------------------------
with suppress(Exception):
    from dotenv import load_dotenv
    load_dotenv()

# --------------------------------------------------
# Core framework imports
# --------------------------------------------------
from fastapi import FastAPI, Depends, HTTPException, Request, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, text

# --------------------------------------------------
# Internal modules
# --------------------------------------------------
from storage import init_db, get_db, Event as EventModel
from parser import parse_email_to_events
from inbox import get_inbox

# --------------------------------------------------
# Configuration
# --------------------------------------------------
ADMIN_BEARER = os.getenv("BEARER_TOKEN") or os.getenv("ADMIN_BEARER") or "change-me"

# --------------------------------------------------
# Application instance
# --------------------------------------------------
app = FastAPI(
    title="AI Events Agent",
    version="0.1.0",
    description="Reads emails → extracts event info → stores in Postgres → simple API.",
)

# --------------------------------------------------
# Middleware for logging & tracing
# --------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    Logs each HTTP request method, path, response code, and duration.
    Adds an X-Rev header containing commit SHA for traceability.
    """
    start_time = time.time()
    response = await call_next(request)
    duration = round((time.time() - start_time) * 1000)
    print(f"{request.method} {request.url.path} → {response.status_code} [{duration}ms]")
    response.headers["X-Rev"] = os.getenv("RAILWAY_GIT_COMMIT_SHA", "dev")
    return response


# ==================================================
# MODELS & HELPERS
# ==================================================
class EventOut(BaseModel):
    """Response model for events (used later for API expansion)."""
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


@app.on_event("startup")
def startup():
    """
    Initialize DB schema on startup.
    If Neon is unreachable, logs stack trace but continues boot.
    """
    try:
        print("[startup] calling init_db()")
        init_db()
        print("[startup] init_db completed successfully")
    except Exception:
        print("[startup] init_db failed (non-fatal):")
        traceback.print_exc()


def require_bearer(request: Request):
    """
    Enforces Bearer token for protected routes.
    Raises 401 if missing or invalid.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    token = auth.removeprefix("Bearer ").strip()
    if token != ADMIN_BEARER:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Bearer token")


# Simple utilities for slug and ID generation
_slugify_re = re.compile(r"[^a-z0-9]+")


def slugify(s: str) -> str:
    """Convert arbitrary string to lowercase slug."""
    s = (s or "").lower()
    s = _slugify_re.sub("-", s).strip("-")
    return s or "untitled"


def make_event_id(source_type: str, source_message_id: str, title: str, index: int | None = None) -> str:
    """Generate stable event ID using SHA1 fingerprint."""
    base = f"{source_type or 'mock'}::{source_message_id or 'unknown'}::{title or 'untitled'}"
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    suffix = f"-{index}" if index is not None else ""
    return f"{(source_type or 'mock')}-{slugify(source_message_id)}-{slugify(title)}-{h}{suffix}"


def _probe_db_count() -> Optional[int]:
    """Return count of events for health diagnostics."""
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


# ==================================================
# ROUTES
# ==================================================
@app.get("/", include_in_schema=False)
async def root():
    """
    Lightweight root endpoint for Railway routing checks.
    Does not touch the DB.
    """
    print("[root] request received")
    return {"msg": "ai-events-agent is running", "docs": "/docs", "health": "/health"}


@app.get("/health", include_in_schema=False)
async def health():
    """Simple liveness endpoint."""
    return {"ok": True}


@app.get("/health/db", include_in_schema=False)
async def health_db():
    """DB health endpoint with query latency measurement."""
    start = time.time()
    count = _probe_db_count()
    duration = round((time.time() - start) * 1000)
    return {"ok": count is not None, "events": count, "duration_ms": duration}


# ==================================================
# ENTRY POINT
# ==================================================
if __name__ == "__main__":
    """
    Local or manual launch entry point.
    Railway start command overrides this automatically in deploy.
    """
    import uvicorn

    port = int(os.getenv("PORT", 8080))
    print(f"[main] Starting Uvicorn on port {port}")
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")

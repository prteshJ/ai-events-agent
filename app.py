# app.py — Main FastAPI Application (Railway-ready)
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

load_dotenv()

from storage import init_db, get_db, Event as EventModel
from parser import parse_email_to_events
from inbox import get_inbox

ADMIN_BEARER = os.getenv("BEARER_TOKEN") or os.getenv("ADMIN_BEARER") or "change-me"

app = FastAPI(
    title="AI Events Agent",
    version="0.1.0",
    description="Reads emails → extracts event info → stores in Postgres → simple API.",
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = round((time.time() - start_time) * 1000)
    print(f"{request.method} {request.url.path} → {response.status_code} [{duration}ms]")
    return response

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

@app.on_event("startup")
def startup():
    try:
        print("[startup] calling init_db()")
        init_db()
        print("[startup] init_db completed successfully")
    except Exception:
        print("[startup] init_db failed (non-fatal):")
        traceback.print_exc()

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

@app.get("/", include_in_schema=False)
def root():
    print("[root] request received")  # Debug
    return {"msg": "ai-events-agent is running", "docs": "/docs", "health": "/health"}

@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

# ... rest of your existing routes here ...

# --------------------------------------------------
# Entry point for Railway
# --------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))  # Use Railway PORT or default 8080
    print(f"[main] Starting Uvicorn on port {port}")
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")

"""
app.py — Main FastAPI Application (super simple & well documented)

What this file does
-------------------
✅ Provides clean API endpoints:
    GET  /health                 → check DB connection
    GET  /events                 → list all events
    GET  /events/recurring       → recurring only
    GET  /events/nonrecurring    → one-time only
    GET  /events/search          → search with optional recurrence filter
    POST /scan                   → protected endpoint that scans emails (mock for now)

✅ Connects to:
    - inbox.py    → mock email reader (you’ll add later)
    - parser.py   → turns emails into event info
    - storage.py  → stores events in Postgres (Neon)

Environment variables (to set later in Railway/Render)
------------------------------------------------------
DATABASE_URL  = your Neon connection string
ADMIN_BEARER  = any random secret string (like a password)
"""

import os
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_

# these modules will exist later when you add more files
from storage import init_db, get_db, Event as EventModel
from parser import parse_email_to_events
from inbox import get_inbox


# ---------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------
ADMIN_BEARER = os.getenv("ADMIN_BEARER", "change-me")  # replace later in Railway/Render

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
        from_attributes = True  # tells FastAPI how to read SQLAlchemy objects


# ---------------------------------------------------------------------
# startup hook — create tables
# ---------------------------------------------------------------------
@app.on_event("startup")
def startup():
    init_db()


# ------------------------

"""
app.py
------
FastAPI service with:
- GET  /health          -> quick readiness check
- POST /import_emails   -> secured by ADMIN_BEARER, runs the full pipeline

Pipeline:
Gmail (unread by query) → Gemini (JSON extraction) → Pydantic validation → Postgres upsert

Environment:
- ADMIN_BEARER   (required, e.g., 'alpha-12345')
- All other env vars used by inbox/parser/storage modules.

Run locally (example):
  uvicorn app:app --host 0.0.0.0 --port 8000

Call import (example):
  curl -X POST https://<RAILWAY_URL>/import_emails \
       -H "Authorization: Bearer alpha-12345"
"""

from __future__ import annotations

import os
from fastapi import FastAPI, Header, HTTPException

from inbox import get_unread_emails
from parser import extract_event
from storage import ExtractedEvent, save_event

app = FastAPI(title="AI Events Agent", version="1.0.0")


def _require_admin(auth_header: str | None):
    """Simple bearer check against ADMIN_BEARER env var."""
    token = os.getenv("ADMIN_BEARER")
    if not token:
        raise HTTPException(status_code=500, detail="ADMIN_BEARER not configured")
    if not auth_header or auth_header.strip() != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/import_emails")
def import_emails(authorization: str | None = Header(default=None)):
    """
    Pull unread emails (by query), parse each via Gemini,
    and upsert events into Postgres using Gmail ID as primary key.
    """
    _require_admin(authorization)

    emails = get_unread_emails()
    processed = 0
    saved = 0

    for em in emails:
        processed += 1
        event_dict = extract_event(em["text"])
        if not event_dict:
            continue
        try:
            ev = ExtractedEvent(**event_dict)
            save_event(em["id"], ev)
            saved += 1
        except Exception as e:
            # Do not fail the whole batch on one bad row
            print("❌ save_event error:", repr(e))

    return {"processed": processed, "saved": saved}

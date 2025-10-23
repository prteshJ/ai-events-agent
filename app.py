"""
app.py
------
AI Events Agent - FastAPI service (final, production-friendly)

Routes
------
GET  /                 -> Friendly welcome with quick links (so base URL is not 404)
GET  /health           -> Readiness check. Returns {"status": "ok"}.
POST /import_emails    -> PRODUCTION entrypoint (Authorization: Bearer <ADMIN_BEARER>)
GET  /run?token=...    -> TEMP browser trigger for quick QA (uses same ADMIN_BEARER value)

Pipeline
--------
Gmail (unread by query) -> Extract text -> Gemini (structured JSON) -> Pydantic -> Postgres UPSERT

Environment (Railway Variables)
-------------------------------
ADMIN_BEARER             e.g., alpha-12345
DATABASE_URL             Neon connection string
GEMINI_API_KEY           Google AI Studio key (no leading '=' or quotes)
GEMINI_MODEL             e.g., gemini-2.5-flash  (no 'models/' prefix)
GMAIL_CLIENT_ID
GMAIL_CLIENT_SECRET
GMAIL_REFRESH_TOKEN
GMAIL_QUERY              optional (default: in:inbox is:unread newer_than:14d)
GMAIL_MAX_RESULTS        optional (default: 50)

Notes
-----
- storage.py quotes "start"/"end" (reserved identifiers) and satisfies NOT NULLs (created_at/updated_at/etc).
- parser.py sanitizes model/key and normalizes ISO datetime; prints rich HTTP errors if Gemini responds with 4xx/5xx.
- We add light retry/backoff around LLM calls to survive transient 503/timeouts.

Remove /run when you’re done QA, or keep ADMIN_BEARER unguessable.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Any, Dict

from fastapi import FastAPI, Header, HTTPException

from inbox import get_unread_emails
from parser import extract_event
from storage import ExtractedEvent, save_event

app = FastAPI(title="AI Events Agent", version="1.0.0")


# ---------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------
def _require_admin(authorization: str | None):
    """
    Checks Authorization header: 'Bearer <ADMIN_BEARER>'.
    401 -> client error (bad/missing token)
    500 -> server misconfig (ADMIN_BEARER not set)
    """
    bearer = os.getenv("ADMIN_BEARER")
    if not bearer:
        raise HTTPException(status_code=500, detail="ADMIN_BEARER not configured")
    if not authorization or authorization.strip() != f"Bearer {bearer}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------
# Tiny retry/backoff for Gemini/network hiccups
# ---------------------------------------------------------------------
def _with_retry(fn: Callable[[], Any], tries: int = 3, base_delay: float = 0.8):
    """
    Retry a no-arg function a few times with exponential backoff.
    Returns the first truthy result, or None if all attempts fail.
    """
    for attempt in range(1, tries + 1):
        try:
            out = fn()
            if out:
                return out
        except Exception as e:
            print("⚠️ transient error, will retry:", repr(e))
        if attempt < tries:
            time.sleep(base_delay * (2 ** (attempt - 1)))
    return None


# ---------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------
def _run_pipeline() -> Dict[str, int]:
    """
    Pull unread emails, extract events via Gemini, upsert to DB.
    Returns counters so operators/CTO can see exactly what happened.
    """
    emails = get_unread_emails()
    stats = {"processed": 0, "empty_text": 0, "parsed": 0, "failed": 0, "saved": 0}

    for em in emails:
        stats["processed"] += 1
        text = (em.get("text") or "").strip()

        if not text:
            stats["empty_text"] += 1
            continue

        # Retry Gemini a few times (handles occasional 503/timeout)
        event_dict = _with_retry(lambda: extract_event(text), tries=3, base_delay=0.8)
        if not event_dict:
            stats["failed"] += 1
            continue

        stats["parsed"] += 1
        try:
            ev = ExtractedEvent(**event_dict)
            # Gmail message id is both the primary id and source_message_id in storage.py
            save_event(em["id"], ev)
            stats["saved"] += 1
        except Exception as e:
            print("❌ save_event error:", repr(e))

    return stats


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.get("/")
def root():
    """
    Friendly root so the base URL is not 404.
    Shows quick links you can click in a browser.
    """
    return {
        "service": "ai-events-agent",
        "ok": True,
        "try": {
            "health": "/health",
            "run_browser_QA": "/run?token=<ADMIN_BEARER>",
            "docs_swagger": "/docs"
        },
        "note": "Use POST /import_emails with Authorization: Bearer <ADMIN_BEARER> in production."
    }


@app.get("/health")
def health():
    """Readiness check for Railway/monitors."""
    return {"status": "ok"}


@app.post("/import_emails")
def import_emails(authorization: str | None = Header(default=None)):
    """
    PRODUCTION entrypoint for scheduled/secure runs.
    Requires header: Authorization: Bearer <ADMIN_BEARER>
    """
    _require_admin(authorization)
    return _run_pipeline()


# TEMP browser-friendly trigger (remove after QA if desired)
@app.get("/run")
def run(token: str):
    """
    Quick manual trigger from a browser:
      https://<app-url>/run?token=<ADMIN_BEARER>
    Uses the same pipeline as /import_emails.
    """
    if token != os.getenv("ADMIN_BEARER"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return _run_pipeline()

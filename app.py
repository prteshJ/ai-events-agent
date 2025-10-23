"""
app.py
------
FastAPI service for the AI Events Agent.

Endpoints
- GET  /health
    Quick readiness check. Returns {"status":"ok"}.

- POST /import_emails   (production path)
    Secured by ADMIN_BEARER header: Authorization: Bearer <ADMIN_BEARER>
    Runs full pipeline: Gmail (unread by query) → Gemini (JSON extraction)
    → Pydantic validation → Postgres upsert.
    Returns per-run diagnostics: processed/empty_text/parsed/failed/saved.

- GET  /run?token=<ADMIN_BEARER>   (temporary, browser-friendly)
    Same as /import_emails but callable directly from the browser for quick QA.
    Remove this route after verification or keep it behind a random ADMIN_BEARER.

Environment variables (set in Railway)
- ADMIN_BEARER           e.g., alpha-12345
- DATABASE_URL           Neon connection string
- GEMINI_API_KEY         Google AI Studio API key (no leading '=')
- GEMINI_MODEL           e.g., gemini-2.5-flash  (no 'models/' prefix)
- GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
- GMAIL_QUERY            (optional; default: in:inbox is:unread newer_than:14d)
- GMAIL_MAX_RESULTS      (optional; default: 50)

Run locally (example):
    uvicorn app:app --host 0.0.0.0 --port 8000

Security note:
- /run is meant for quick testing only. It checks the same ADMIN_BEARER value but via query param.
  Delete it once your pipeline is verified, or keep ADMIN_BEARER unguessable.
"""

from __future__ import annotations

import os
from fastapi import FastAPI, Header, HTTPException

from inbox import get_unread_emails
from parser import extract_event
from storage import ExtractedEvent, save_event

app = FastAPI(title="AI Events Agent", version="1.0.0")


# ---------- Auth Helpers ----------

def _require_admin(authorization: str | None):
    """
    Checks Authorization header: 'Bearer <ADMIN_BEARER>'.
    Raises 401 if not provided or mismatched.
    """
    bearer = os.getenv("ADMIN_BEARER")
    if not bearer:
        # Misconfiguration should be a 500 to signal operator action.
        raise HTTPException(status_code=500, detail="ADMIN_BEARER not configured")
    if not authorization or authorization.strip() != f"Bearer {bearer}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------- Core Pipeline ----------

def _run_pipeline() -> dict:
    """
    Pipeline:
      Gmail → extract text → Gemini → Pydantic → Postgres.
    Returns diagnostic counters so you can see exactly what happened.
    """
    emails = get_unread_emails()
    stats = {"processed": 0, "empty_text": 0, "parsed": 0, "failed": 0, "saved": 0}

    for em in emails:
        stats["processed"] += 1
        text = (em.get("text") or "").strip()
        if not text:
            stats["empty_text"] += 1
            continue

        event_dict = extract_event(text)
        if not event_dict:
            stats["failed"] += 1
            continue

        stats["parsed"] += 1
        try:
            ev = ExtractedEvent(**event_dict)
            save_event(em["id"], ev)
            stats["saved"] += 1
        except Exception as e:
            # Do not fail the whole batch on one row; log and continue.
            print("❌ save_event error:", repr(e))
            # we count it as parsed but not saved

    return stats


# ---------- Routes ----------

@app.get("/health")
def health():
    """Basic readiness probe for Railway/uptime monitors."""
    return {"status": "ok"}


@app.post("/import_emails")
def import_emails(authorization: str | None = Header(default=None)):
    """
    Production entrypoint. Requires:
        Authorization: Bearer <ADMIN_BEARER>
    """
    _require_admin(authorization)
    return _run_pipeline()


# --- Temporary browser-friendly trigger for quick manual testing ---
@app.get("/run")
def run(token: str):
    """
    Temporary GET trigger for quick testing from a browser.
    Call: https://<your-app>/run?token=<ADMIN_BEARER>
    """
    if token != os.getenv("ADMIN_BEARER"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return _run_pipeline()

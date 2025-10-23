"""
app.py
------
AI Events Agent - FastAPI service (swagger auth + quota-aware)

Routes
------
GET  /                 Friendly root (links to /health, /docs, /run)
GET  /health           Readiness check
POST /import_emails    PRODUCTION entry (Authorization: Bearer <ADMIN_BEARER>)
GET  /run?token=...    TEMP browser trigger (same admin secret)

Free-tier safety
---------------
- Caps LLM calls per run via LLM_MAX_PER_RUN (default: 8) to stay below Gemini free-tier bursts (~10)
- Backoff + stop early if quota is hit; counters returned

Env (Railway)
-------------
ADMIN_BEARER, DATABASE_URL
GEMINI_API_KEY, GEMINI_MODEL
GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
GMAIL_QUERY (opt, default: in:inbox is:unread newer_than:14d)
GMAIL_MAX_RESULTS (opt, default: 50)
LLM_MAX_PER_RUN (opt, default: 8)

Notes
-----
- storage.py satisfies NOT NULLs and quotes "start"/"end"
- parser.py will raise RetryableError with optional wait_seconds on 429/503
"""

from __future__ import annotations

import os
import time
from typing import Callable, Any, Dict

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.security.api_key import APIKeyHeader

from inbox import get_unread_emails
from parser import extract_event, RetryableError
from storage import ExtractedEvent, save_event

app = FastAPI(title="AI Events Agent", version="1.1.0")

# -------- Swagger auth (adds the lock button in /docs) --------
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)

def _require_admin(authorization: str | None):
    bearer = os.getenv("ADMIN_BEARER")
    if not bearer:
        raise HTTPException(status_code=500, detail="ADMIN_BEARER not configured")
    if not authorization or authorization.strip() != f"Bearer {bearer}":
        raise HTTPException(status_code=401, detail="Unauthorized")

def auth_dep(authorization: str | None = Depends(api_key_header)):
    _require_admin(authorization)

# ----------------- Retry helper for parser --------------------
def _with_retry(fn: Callable[[], Any], tries: int = 3, base_delay: float = 0.8):
    """
    Retry a no-arg function a few times with exponential backoff.
    If a RetryableError carries wait_seconds, honor that.
    """
    for attempt in range(1, tries + 1):
        try:
            out = fn()
            if out:
                return out
        except RetryableError as e:
            delay = e.wait_seconds or (base_delay * (2 ** (attempt - 1)))
            print(f"↻ retryable error; sleeping {delay:.1f}s …")
            time.sleep(delay)
        except Exception as e:

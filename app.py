# app.py
"""
AI Events Agent — FastAPI service
=================================

Plain-English summary
---------------------
When you call POST /run (with a shared token), the service will:
  1) Read recent emails from Gmail that match GMAIL_QUERY.
  2) Ask an AI model (Gemini) to pull out structured "event" details.
  3) Save those events into a Neon Postgres table (ai_events).

Health probe:
  GET /health  -> {"status": "ok"}  (Railway uses this to check the app is alive)

Security:
  A simple shared secret is required to call /run.
  Provide it as ?token=... or header X-Run-Token: ...

Configuration:
  All secrets and switches are passed by environment variables (see bottom).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

# ---------- DB (Neon Postgres) ----------
# psycopg 3 is fast and simple.
import psycopg
from psycopg.rows import dict_row

# ---------- Gmail (Google API) ----------
# These imports are optional; we guard them so the app never fails to boot if
# you temporarily remove Gmail variables or packages.
try:
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
except Exception:  # libs not installed — keep booting and log later
    build = None
    Credentials = None

# ---------- Gemini (LLM) ----------
try:
    import google.generativeai as genai
except Exception:
    genai = None


# ---------------------------------------------------------
# App & Logging
# ---------------------------------------------------------
app = FastAPI(title="AI Events Agent", version="1.1.0")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("ai-events-agent")


# ---------------------------------------------------------
# Environment (kept 1:1 with your Railway setup)
# ---------------------------------------------------------
# Auth for /run
RUN_TOKEN = os.getenv("ADMIN_BEARER", "alpha-12345")

# Gmail
GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN")
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox is:unread newer_than:7d")
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", "50"))
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
LLM_MAX_PER_RUN = int(os.getenv("LLM_MAX_PER_RUN", "3"))  # safety cap

# Neon
DATABASE_URL = os.getenv("DATABASE_URL")  # e.g., postgresql://...sslmode=require

# Feature flags (optional)
ENABLE_IMPORT_WEB = os.getenv("ENABLE_IMPORT_WEB", "true").lower() == "true"

# Keep a single process-wide connection to reduce cold-start overhead
_db_conn: Optional[psycopg.Connection] = None


# ---------------------------------------------------------
# Pydantic Models (clear types for API response)
# ---------------------------------------------------------
class EventOut(BaseModel):
    subject: str
    event_date: Optional[str] = None
    event_time: Optional[str] = None
    location: Optional[str] = None
    organizer: Optional[str] = None
    notes: Optional[str] = None
    source_gmail_id: Optional[str] = None


class RunResponse(BaseModel):
    total_emails: int
    parsed_events: int
    inserted_rows: int
    skipped_reason: Optional[str] = None
    details: List[EventOut] = []


# ---------------------------------------------------------
# DB Helpers
# ---------------------------------------------------------
def get_db() -> Optional[psycopg.Connection]:
    """
    Return a Neon connection. Boot never fails if DATABASE_URL is missing:
    callers just skip DB writes.
    """
    global _db_conn
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — DB writes will be skipped.")
        return None

    if _db_conn is None or _db_conn.closed:
        _db_conn = psycopg.connect(DATABASE_URL, autocommit=True)
        log.info("Connected to Neon (Postgres).")
        _ensure_events_table(_db_conn)
    return _db_conn


def _ensure_events_table(conn: psycopg.Connection) -> None:
    """
    Create a simple, analytics-friendly table if it doesn't exist.
    """
    sql = """
    CREATE TABLE IF NOT EXISTS ai_events (
        id BIGSERIAL PRIMARY KEY,
        subject TEXT NOT NULL,
        event_date TEXT NULL,
        event_time TEXT NULL,
        location TEXT NULL,
        organizer TEXT NULL,
        notes TEXT NULL,
        source_gmail_id TEXT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """
    with conn.cursor() as cur:
        cur.execute(sql)
    log.info("Ensured table ai_events exists.")


def insert_events(conn: psycopg.Connection, rows: List[EventOut]) -> int:
    if not rows:
        return 0
    sql = """
    INSERT INTO ai_events
      (subject, event_date, event_time, location, organizer, notes, source_gmail_id)
    VALUES
      (%(subject)s, %(event_date)s, %(event_time)s, %(location)s, %(organizer)s, %(notes)s, %(source_gmail_id)s);
    """
    with conn.cursor() as cur:
        cur.executemany(sql, [r.dict() for r in rows])
    return len(rows)


# ---------------------------------------------------------
# Gmail Helpers
# ---------------------------------------------------------
def build_gmail_service() -> Optional[Any]:
    """
    Build the Gmail API client if credentials and packages are present.
    Otherwise return None (we'll skip gracefully).
    """
    if build is None or Credentials is None:
        log.warning("Google API libraries not installed; skipping Gmail.")
        return None

    if not (GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN):
        log.warning("Gmail credentials not fully set; skipping Gmail.")
        return None

    creds = Credentials(
        token=None,  # access token will be lazily refreshed using refresh_token
        refresh_token=GMAIL_REFRESH_TOKEN,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return service
    except Exception as e:
        log.error(f"Failed to build Gmail service: {e}")
        return None


def fetch_gmail_messages(service: Any, q: str, limit: int) -> List[Tuple[str, str, str]]:
    """
    Return up to `limit` emails as (message_id, subject, snippet).
    """
    if service is None:
        return []

    try:
        users = service.users()
        resp = users.messages().list(
            userId="me",
            q=q,
            maxResults=min(limit, 100)  # Gmail max per page
        ).execute()

        ids = [m["id"] for m in resp.get("messages", [])]
        out: List[Tuple[str, str, str]] = []

        for mid in ids:
            m = users.messages().get(
                userId="me",
                id=mid,
                format="metadata",
                metadataHeaders=["Subject"],
            ).execute()
            headers = m.get("payload", {}).get("headers", [])
            subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "(no subject)")
            snippet = m.get("snippet", "")
            out.append((mid, subject, snippet))

        return out
    except Exception as e:
        log.error(f"Gmail fetch failed: {e}")
        return []


# ---------------------------------------------------------
# Gemini Helpers
# ---------------------------------------------------------
def init_gemini():
    """
    Initialize Gemini client if available. Otherwise return None.
    """
    if genai is None:
        log.warning("Gemini library not installed; skipping AI parsing.")
        return None
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not set; skipping AI parsing.")
        return None
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        return genai.GenerativeModel(GEMINI_MODEL)
    except Exception as e:
        log.error(f"Gemini init failed: {e}")
        return None


def parse_event_with_gemini(model, subject: str, snippet: str) -> EventOut:
    """
    Ask Gemini to extract a small JSON with event fields.
    Retries with backoff for transient errors (e.g., 429/503).
    Falls back to minimal data if not available.
    """
    base = EventOut(subject=subject, notes=None)

    if model is None:
        return base

    prompt = f"""
You are a structured information extractor.
From the email below, extract a JSON object with keys:
  subject, event_date (YYYY-MM-DD or null), event_time (HH:MM or text or null),
  location, organizer, notes.
Keep it short.

Subject: {subject}
Snippet: {snippet}
"""

    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            resp = model.generate_content(prompt)
            text = (resp.text or "").strip()

            # If model returns JSON, parse; else place raw text into notes
            try:
                data = json.loads(text)
                return EventOut(
                    subject=data.get("subject") or subject,
                    event_date=data.get("event_date"),
                    event_time=data.get("event_time"),
                    location=data.get("location"),
                    organizer=data.get("organizer"),
                    notes=data.get("notes"),
                )
            except Exception:
                return EventOut(subject=subject, notes=(text or snippet[:500]))
        except Exception as e:
            wait = 1.5 * attempt
            log.warning(f"Gemini call failed (attempt {attempt}/{max_attempts}): {e}; retrying in {wait:.1f}s")
            time.sleep(wait)

    return base


# ---------------------------------------------------------
# Security dependency for /run
# ---------------------------------------------------------
def require_token(req: Request) -> None:
    token = req.query_params.get("token") or req.headers.get("X-Run-Token")
    if token != RUN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# ---------------------------------------------------------
# Routes
# ---------------------------------------------------------
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/run", response_model=RunResponse)
def run(_: None = Depends(require_token)) -> RunResponse:
    """
    Orchestration:
      Gmail → Gemini → Neon
    """
    # 1) Gmail
    gmail = build_gmail_service()
    fetch_limit = min(GMAIL_MAX_RESULTS, LLM_MAX_PER_RUN)
    messages = fetch_gmail_messages(gmail, GMAIL_QUERY, fetch_limit)
    log.info(f"Fetched {len(messages)} emails (query='{GMAIL_QUERY}', limit={fetch_limit}).")

    # 2) Gemini
    model = init_gemini()
    parsed: List[EventOut] = []
    for mid, subject, snippet in messages:
        evt = parse_event_with_gemini(model, subject, snippet)
        evt.source_gmail_id = mid
        parsed.append(evt)

    # 3) Neon
    inserted = 0
    skipped_reason = None
    conn = get_db()
    if conn is None:
        skipped_reason = "DB not configured"
    else:
        try:
            inserted = insert_events(conn, parsed)
        except Exception as e:
            log.error(f"DB insert failed: {e}")
            skipped_reason = f"DB insert failed: {e}"

    return RunResponse(
        total_emails=len(messages),
        parsed_events=len(parsed),
        inserted_rows=inserted,
        skipped_reason=skipped_reason,
        details=parsed,
    )


# ---------------------------------------------------------
# Startup hook (light warmups)
# ---------------------------------------------------------
@app.on_event("startup")
def on_startup() -> None:
    try:
        if DATABASE_URL:
            get_db()  # ensure table + warm connection
        log.info("Startup complete. /health is ready.")
        log.info(f"Gemini: {'ENABLED' if GEMINI_API_KEY else 'DISABLED'}; "
                 f"Gmail: {'ENABLED' if (GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN) else 'DISABLED'}")
    except Exception as e:
        # IMPORTANT: never leave an empty except — always indent the handler body.
        log.error(f"Startup failed gracefully (service continues with /health): {e}")

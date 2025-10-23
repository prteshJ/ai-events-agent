"""
AI Events Agent — Production-Ready FastAPI App
==============================================

✔ Pulls unread Gmail messages (using Google API)
✔ Optionally parses events with Gemini (if quota available)
✔ Writes structured events into Neon PostgreSQL
✔ Exposes /health and /run endpoints
✔ Lazy-loads psycopg so Railway boots cleanly even if DB isn’t ready
"""

import os
import json
import time
import logging
from typing import List, Optional, Dict, Any, Tuple

from fastapi import FastAPI, Depends, HTTPException, Request
from pydantic import BaseModel

# ----------------- Logging -----------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("ai-events-agent")

# ----------------- Imports -----------------
# Lazy import psycopg to avoid startup crash if DB driver missing
try:
    import psycopg  # provided by psycopg-binary
except Exception as e:
    psycopg = None
    log.warning("psycopg import deferred/unavailable: %s", e)

# Gmail API libs (optional)
try:
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
except Exception:
    build = None
    Credentials = None

# Gemini AI libs (optional)
try:
    import google.generativeai as genai
except Exception:
    genai = None

# ----------------- FastAPI -----------------
app = FastAPI(title="AI Events Agent", version="2.0.0")

# ----------------- Environment Vars -----------------
RUN_TOKEN = os.getenv("ADMIN_BEARER", "alpha-12345")
DATABASE_URL = os.getenv("DATABASE_URL")
GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN")
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox is:unread newer_than:7d")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ----------------- Models -----------------
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

# ----------------- DB Helpers -----------------
def get_db() -> Optional["psycopg.Connection"]:
    """
    Open Neon DB connection lazily.
    Prevents startup crashes if libpq not loaded yet.
    """
    if psycopg is None:
        log.error("psycopg not available; DB ops disabled.")
        return None
    if not DATABASE_URL:
        log.warning("DATABASE_URL missing; skipping DB connection.")
        return None
    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True)
        log.info("Connected to Neon Postgres.")
        with conn.cursor() as cur:
            cur.execute("""
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
            """)
        return conn
    except Exception as e:
        log.error(f"DB connection failed: {e}")
        return None

def insert_events(conn, rows: List[EventOut]) -> int:
    if not rows:
        return 0
    sql = """
    INSERT INTO ai_events (subject, event_date, event_time, location, organizer, notes, source_gmail_id)
    VALUES (%(subject)s, %(event_date)s, %(event_time)s, %(location)s, %(organizer)s, %(notes)s, %(source_gmail_id)s);
    """
    with conn.cursor() as cur:
        cur.executemany(sql, [r.dict() for r in rows])
    return len(rows)

# ----------------- Gmail Helpers -----------------
def build_gmail_service():
    if not (GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN):
        log.warning("Gmail credentials not configured.")
        return None
    if build is None or Credentials is None:
        log.warning("Gmail client libraries missing.")
        return None
    creds = Credentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

def fetch_gmail_messages(service, q: str, limit: int = 10) -> List[Tuple[str, str, str]]:
    if service is None:
        return []
    try:
        users = service.users()
        resp = users.messages().list(userId="me", q=q, maxResults=limit).execute()
        ids = [m["id"] for m in resp.get("messages", [])]
        results = []
        for mid in ids:
            m = users.messages().get(userId="me", id=mid, format="metadata", metadataHeaders=["Subject"]).execute()
            headers = m.get("payload", {}).get("headers", [])
            subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "(no subject)")
            snippet = m.get("snippet", "")
            results.append((mid, subject, snippet))
        return results
    except Exception as e:
        log.error(f"Gmail fetch failed: {e}")
        return []

# ----------------- Gemini Helpers -----------------
def init_gemini():
    if genai is None or not GEMINI_API_KEY:
        log.info("Gemini disabled or quota exhausted; running Gmail→Neon only.")
        return None
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        return genai.GenerativeModel(GEMINI_MODEL)
    except Exception as e:
        log.error(f"Gemini init failed: {e}")
        return None

def parse_event_with_gemini(model, subject: str, snippet: str) -> EventOut:
    if model is None:
        # Gemini bypass: directly push Gmail data into Neon
        return EventOut(
            subject=subject,
            event_date=None,
            event_time=None,
            location="Gmail Inbox",
            organizer="Gmail Fetch",
            notes=snippet[:500],
        )
    prompt = f"""
Extract JSON with keys:
subject, event_date (YYYY-MM-DD or null), event_time, location, organizer, notes.
Subject: {subject}
Snippet: {snippet}
"""
    try:
        resp = model.generate_content(prompt)
        text = (resp.text or "").strip()
        data = json.loads(text)
        return EventOut(
            subject=data.get("subject") or subject,
            event_date=data.get("event_date"),
            event_time=data.get("event_time"),
            location=data.get("location"),
            organizer=data.get("organizer"),
            notes=data.get("notes"),
        )
    except Exception as e:
        log.warning(f"Gemini parse failed: {e}")
        return EventOut(subject=subject, notes=snippet[:500])

# ----------------- Security -----------------
def require_token(req: Request):
    token = req.query_params.get("token") or req.headers.get("X-Run-Token")
    if token != RUN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")

# ----------------- Routes -----------------
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}

@app.post("/run", response_model=RunResponse)
def run(_: None = Depends(require_token)):
    gmail_service = build_gmail_service()
    messages = fetch_gmail_messages(gmail_service, GMAIL_QUERY, 5)
    log.info(f"Fetched {len(messages)} Gmail messages.")
    gemini_model = init_gemini()
    parsed = []
    for mid, subject, snippet in messages:
        evt = parse_event_with_gemini(gemini_model, subject, snippet)
        evt.source_gmail_id = mid
        parsed.append(evt)
    conn = get_db()
    inserted, skipped = 0, None
    if conn:
        try:
            inserted = insert_events(conn, parsed)
        except Exception as e:
            skipped = str(e)
            log.error(f"DB insert failed: {e}")
    return RunResponse(
        total_emails=len(messages),
        parsed_events=len(parsed),
        inserted_rows=inserted,
        skipped_reason=skipped,
        details=parsed,
    )

@app.on_event("startup")
def startup_event():
    log.info("🚀 Starting AI Events Agent ...")
    if psycopg is None:
        log.warning("psycopg not loaded — check psycopg-binary install.")
    if DATABASE_URL:
        get_db()
    log.info("✅ App ready — /health endpoint available.")

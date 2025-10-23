"""
AI Events Agent — FastAPI (Neon table mapping only)
---------------------------------------------------

• Reads unread Gmail (Google API)
• (Optional) Parses with Gemini; if not available, passes through subject/snippet
• Writes to Neon: public.events (title, description, source_message_id, source_snippet)
• Endpoints:
    - GET  /health
    - POST /run?token=...
"""

import os
import json
import logging
from typing import List, Optional, Tuple, Dict

from fastapi import FastAPI, Depends, HTTPException, Request
from pydantic import BaseModel, Field

# ----------------- Logging -----------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("ai-events-agent")

# ----------------- Optional imports (tolerant) -----------------
# DB driver (use psycopg-binary in requirements)
try:
    import psycopg  # type: ignore
except Exception as e:
    psycopg = None
    log.warning("psycopg import deferred/unavailable: %s", e)

# Gmail API libs (optional)
try:
    from googleapiclient.discovery import build  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
except Exception:
    build = None
    Credentials = None

# Gemini AI libs (optional)
try:
    import google.generativeai as genai  # type: ignore
except Exception:
    genai = None

# ----------------- FastAPI -----------------
app = FastAPI(title="AI Events Agent", version="2.1.1")

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
    notes: Optional[str] = None
    source_gmail_id: Optional[str] = None
    source_snippet: Optional[str] = None  # raw Gmail snippet

class RunResponse(BaseModel):
    total_emails: int
    parsed_events: int
    inserted_rows: int
    skipped_reason: Optional[str] = None
    details: List[EventOut] = Field(default_factory=list)

# ----------------- DB Helpers -----------------
def get_db() -> Optional["psycopg.Connection"]:
    """
    Open Neon DB connection lazily with autocommit.
    No DDL executed here to avoid tampering with live schema.
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
        return conn
    except Exception as e:
        log.error("DB connection failed: %s", e)
        return None

def insert_into_public_events(conn, rows: List[EventOut]) -> int:
    """
    Insert rows into existing Neon table: public.events
      - title             ← subject
      - description       ← notes
      - source_message_id ← source_gmail_id
      - source_snippet    ← source_snippet
    Dedupes by source_message_id in code (no schema changes).
    """
    if not rows or conn is None:
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            if not r.source_gmail_id:
                continue
            # dedupe by source_message_id
            cur.execute(
                "SELECT 1 FROM public.events WHERE source_message_id = %s LIMIT 1;",
                (r.source_gmail_id,)
            )
            if cur.fetchone():
                continue

            cur.execute(
                """
                INSERT INTO public.events (title, description, source_message_id, source_snippet)
                VALUES (%s, %s, %s, %s);
                """,
                (
                    r.subject,
                    (r.notes or "")[:4000],
                    r.source_gmail_id,
                    (r.source_snippet or "")[:4000],
                ),
            )
            inserted += 1
    return inserted

# ----------------- Gmail Helpers -----------------
def build_gmail_service():
    if not (GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN):
        log.warning("Gmail credentials not configured; skipping fetch.")
        return None
    if build is None or Credentials is None:
        log.warning("Gmail client libraries missing; skipping fetch.")
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
    """
    Returns list of tuples: (message_id, subject, snippet)
    """
    if service is None:
        return []
    try:
        users = service.users()
        resp = users.messages().list(userId="me", q=q, maxResults=limit).execute()
        ids = [m["id"] for m in resp.get("messages", [])]
        results: List[Tuple[str, str, str]] = []
        for mid in ids:
            m = users.messages().get(
                userId="me",
                id=mid,
                format="metadata",
                metadataHeaders=["Subject"]
            ).execute()
            headers = m.get("payload", {}).get("headers", [])
            subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "(no subject)")
            snippet = m.get("snippet", "")
            results.append((mid, subject, snippet))
        return results
    except Exception as e:
        log.error("Gmail fetch failed: %s", e)
        return []

# ----------------- Gemini Helpers -----------------
def init_gemini():
    if genai is None or not GEMINI_API_KEY:
        log.info("Gemini disabled or no quota; pass-through mode.")
        return None
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        return genai.GenerativeModel(GEMINI_MODEL)
    except Exception as e:
        log.error("Gemini init failed: %s", e)
        return None

def parse_event(model, subject: str, snippet: str) -> EventOut:
    """
    If Gemini available, ask it to produce a compact structured event.
    Otherwise, pass-through subject + snippet.
    """
    if model is None:
        return EventOut(
            subject=subject,
            notes=snippet,
            source_snippet=snippet,
        )
    prompt = f"""
Return ONLY strict JSON:
{{
  "subject": string,
  "notes": string
}}
Subject: {subject}
Snippet: {snippet}
"""
    try:
        resp = model.generate_content(prompt)
        text = (resp.text or "").strip()
        data = json.loads(text)
        return EventOut(
            subject=data.get("subject") or subject,
            notes=data.get("notes") or snippet,
            source_snippet=snippet,
        )
    except Exception as e:
        log.warning("Gemini parse failed; falling back: %s", e)
        return EventOut(subject=subject, notes=snippet, source_snippet=snippet)

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
    messages = fetch_gmail_messages(gmail_service, GMAIL_QUERY, limit=10)
    log.info("Fetched %d Gmail messages.", len(messages))

    gemini_model = init_gemini()
    parsed: List[EventOut] = []
    for mid, subject, snippet in messages:
        evt = parse_event(gemini_model, subject, snippet)
        evt.source_gmail_id = mid
        parsed.append(evt)

    inserted, skipped = 0, None
    conn = get_db()
    if conn:
        try:
            inserted = insert_into_public_events(conn, parsed)
        except Exception as e:
            skipped = str(e)
            log.error("DB insert failed: %s", e)

    return RunResponse(
        total_emails=len(messages),
        parsed_events=len(parsed),
        inserted_rows=inserted,
        skipped_reason=skipped,
        details=parsed,
    )

@app.on_event("startup")
def on_startup():
    log.info("🚀 Starting AI Events Agent")
    if psycopg is None:
        log.warning("psycopg not loaded — ensure psycopg-binary installed.")
    if DATABASE_URL and psycopg is not None:
        try:
            conn = get_db()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                log.info("✅ DB ping ok.")
        except Exception as e:
            log.warning("DB ping failed (app still starts): %s", e)
    log.info("✅ App ready — /health live.")

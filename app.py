"""
AI Events Agent — FastAPI
---------------------------------
Reads UNREAD Gmail messages, optionally summarizes with Gemini,
and stores results into Neon `public.events`.

Design goals:
- Keep it simple and stable (no breaking changes).
- Pretty Swagger with "Authorize" button (API key header: X-Run-Token).
- Correctness first:
  * Idempotent DB inserts via ON CONFLICT(source_message_id) DO NOTHING.
  * DB connection closed per request to avoid leaks on serverless hosts.
  * Safe, optional Gemini parsing with JSON fence handling + fallback.
  * Startup sanity: create unique index IF NOT EXISTS (safe/idempotent).

Endpoints
- GET  /health
- POST /run  (requires token via Swagger Authorize or ?token=)

Environment
- ADMIN_BEARER            (token for /run; default: "alpha-12345")
- DATABASE_URL            (Neon URL; recommend adding ?sslmode=require)
- GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
- GMAIL_QUERY             (default: "in:inbox is:unread newer_than:7d")
- GMAIL_MAX_RESULTS       (default: 10)
- GEMINI_API_KEY          (optional; if absent, pass-through mode)
- GEMINI_MODEL            (default: "gemini-2.5-flash")
- LOG_LEVEL               (default: "INFO")
"""

from __future__ import annotations

import os
import re
import json
import logging
from typing import List, Optional, Tuple, Dict

from fastapi import FastAPI, Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

# ----------------- Logging -----------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("ai-events-agent")

# ----------------- Optional imports (tolerant) -----------------
# DB driver (use psycopg-binary in requirements)
try:
    import psycopg  # type: ignore
except Exception as e:
    psycopg = None
    log.warning("psycopg import unavailable: %s", e)

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
app = FastAPI(
    title="AI Events Agent",
    version="2.3.0",
    # keeps the token remembered across refreshes — great for demos
    swagger_ui_parameters={"persistAuthorization": True},
)

# Swagger “Authorize” button config — API key via header X-Run-Token
api_key_header = APIKeyHeader(
    name="X-Run-Token",
    description="Enter your ADMIN_BEARER token to authorize /run",
    auto_error=False,
)

# ----------------- Environment Vars -----------------
RUN_TOKEN = os.getenv("ADMIN_BEARER", "alpha-12345")
DATABASE_URL = os.getenv("DATABASE_URL")  # e.g., postgres://.../db?sslmode=require

GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN")
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox is:unread newer_than:7d")
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", "10"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ----------------- Models -----------------
class EventOut(BaseModel):
    """Represents one parsed/collected event-like record derived from a Gmail message."""
    subject: str
    notes: Optional[str] = None
    source_gmail_id: Optional[str] = None
    source_snippet: Optional[str] = None  # raw Gmail snippet


class RunResponse(BaseModel):
    """Response shape for /run to make Swagger output predictable and demo-friendly."""
    total_emails: int
    parsed_events: int
    inserted_rows: int
    skipped_reason: Optional[str] = None
    details: List[EventOut] = Field(default_factory=list)

# ----------------- Security -----------------
def require_token(x_token: str = Security(api_key_header), req: Request = None):
    """
    Accept token from Swagger's Authorize popup (X-Run-Token) OR fallback to ?token=.
    Keeps current behavior but makes Swagger nicer for demos.
    """
    token = x_token or (req.query_params.get("token") if req else None)
    if token != RUN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")

# ----------------- DB Helpers -----------------
def get_db() -> Optional["psycopg.Connection"]:
    """
    Open Neon DB connection lazily with autocommit.
    Closed after each request to prevent connection leaks on serverless hosts.
    """
    if psycopg is None:
        log.error("psycopg not available; DB ops disabled.")
        return None
    if not DATABASE_URL:
        log.warning("DATABASE_URL missing; skipping DB connection.")
        return None
    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True)
        return conn
    except Exception as e:
        log.error("DB connection failed: %s", e)
        return None


def ensure_indexes(conn: "psycopg.Connection") -> None:
    """
    Create a unique index for idempotent imports.
    Safe to run on every startup (IF NOT EXISTS).
    """
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_events_source_message_id
            ON public.events (source_message_id);
            """
        )


def insert_into_public_events(conn: "psycopg.Connection", rows: List[EventOut]) -> int:
    """
    Insert rows into existing Neon table: public.events

      - title             ← subject
      - description       ← notes
      - source_message_id ← source_gmail_id
      - source_snippet    ← source_snippet

    Idempotency:
      ON CONFLICT(source_message_id) DO NOTHING
    """
    if not rows or conn is None:
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            if not r.source_gmail_id:
                continue
            cur.execute(
                """
                INSERT INTO public.events (title, description, source_message_id, source_snippet)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (source_message_id) DO NOTHING;
                """,
                (
                    r.subject,
                    (r.notes or "")[:4000],
                    r.source_gmail_id,
                    (r.source_snippet or "")[:4000],
                ),
            )
            # rowcount == 1 only when actually inserted (not on conflict)
            if cur.rowcount == 1:
                inserted += 1
    return inserted

# ----------------- Gmail Helpers -----------------
def build_gmail_service():
    """
    Builds a Gmail API service using a refresh token.
    Returns None if creds/libs are not configured, so the app can degrade gracefully.
    """
    if not (GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN):
        log.info("Gmail credentials not configured; skipping fetch.")
        return None
    if build is None or Credentials is None:
        log.info("Gmail client libraries missing; skipping fetch.")
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
    Fetch UNREAD messages matching query.
    Returns list of tuples: (message_id, subject, snippet)

    Intentionally simple because your current setup works well.
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
                metadataHeaders=["Subject"],
            ).execute()
            headers = m.get("payload", {}).get("headers", [])
            subject = next(
                (h["value"] for h in headers if h.get("name", "").lower() == "subject"),
                "(no subject)",
            )
            snippet = m.get("snippet", "")
            results.append((mid, subject, snippet))
        return results
    except Exception as e:
        log.error("Gmail fetch failed: %s", e)
        return []

# ----------------- Gemini Helpers -----------------
# Extract fenced ```json { ... } ``` blocks if present
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

def init_gemini():
    """Initialize Gemini client if configured; otherwise return None (pass-through)."""
    if genai is None or not GEMINI_API_KEY:
        log.info("Gemini disabled or not configured. Using pass-through.")
        return None
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        return genai.GenerativeModel(
            GEMINI_MODEL,
            generation_config={"response_mime_type": "application/json"},
        )
    except Exception as e:
        log.error("Gemini init failed: %s", e)
        return None


def _coerce_json(text: str) -> Dict:
    """
    Coerce model output to JSON, handling common code-fence cases.
    Returns {} on failure.
    """
    text = (text or "").strip()
    if not text:
        return {}
    m = _JSON_BLOCK_RE.search(text)
    if m:
        text = m.group(1).strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        return json.loads(text)
    except Exception:
        # last-resort: try to slice first {...} block
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(text[a : b + 1])
            except Exception:
                pass
    return {}


def parse_event(model, subject: str, snippet: str) -> EventOut:
    """
    If Gemini available, request compact JSON {subject, notes}; otherwise pass-through.
    Always returns a valid EventOut (no exceptions propagate).
    """
    if model is None:
        return EventOut(subject=subject, notes=snippet, source_snippet=snippet)

    prompt = {
        "instruction": "Return ONLY strict JSON with keys 'subject' and 'notes'. No extra text.",
        "subject": subject,
        "snippet": snippet,
    }
    try:
        resp = model.generate_content(json.dumps(prompt))
        data = _coerce_json(getattr(resp, "text", "") or "")
        subj = (data.get("subject") or subject) if isinstance(data, dict) else subject
        notes = (data.get("notes") or snippet) if isinstance(data, dict) else snippet
        return EventOut(subject=subj, notes=notes, source_snippet=snippet)
    except Exception as e:
        log.warning("Gemini parse failed; fallback to pass-through: %s", e)
        return EventOut(subject=subject, notes=snippet, source_snippet=snippet)

# ----------------- Routes -----------------
@app.get("/health", tags=["System"])
def health() -> Dict[str, str]:
    """Simple readiness check for load balancers and smoke tests."""
    return {"status": "ok"}


@app.post("/run", response_model=RunResponse, tags=["Importer"])
def run(_: None = Depends(require_token)):
    """
    Pipeline:
      1) Fetch unread Gmail matching `GMAIL_QUERY`
      2) Optionally parse with Gemini (if configured)
      3) Insert into Neon `public.events` (idempotent by source_message_id)
    """
    # 1) Gmail
    gmail_service = build_gmail_service()
    messages = fetch_gmail_messages(gmail_service, GMAIL_QUERY, limit=GMAIL_MAX_RESULTS)
    log.info("Fetched %d Gmail messages.", len(messages))

    # 2) Gemini (optional)
    gemini_model = init_gemini()
    parsed: List[EventOut] = []
    for mid, subject, snippet in messages:
        evt = parse_event(gemini_model, subject, snippet)
        evt.source_gmail_id = mid
        parsed.append(evt)

    # 3) DB insert (idempotent)
    inserted, skipped = 0, None
    conn = get_db()
    try:
        if conn:
            inserted = insert_into_public_events(conn, parsed)
    except Exception as e:
        skipped = str(e)
        log.error("DB insert failed: %s", e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return RunResponse(
        total_emails=len(messages),
        parsed_events=len(parsed),
        inserted_rows=inserted,
        skipped_reason=skipped,
        details=parsed,
    )

# ----------------- Startup -----------------
@app.on_event("startup")
def on_startup():
    """
    Light startup checks:
    - Ping DB (if configured) and ensure unique index exists.
    - Do not fail startup on DB errors (service remains usable for /health).
    """
    log.info("🚀 Starting AI Events Agent")
    if psycopg is None:
        log.warning("psycopg not loaded — ensure psycopg-binary installed.")
    if DATABASE_URL and psycopg is not None:
        conn = get_db()
        try:
            if conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                ensure_indexes(conn)  # safe: IF NOT EXISTS
                log.info("✅ DB ping + indexes ok.")
        except Exception as e:
            log.warning("DB ping failed (app still starts): %s", e)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    log.info("✅ App ready — Swagger /docs live (Authorize persists).")

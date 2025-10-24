"""
storage.py (v2)
---------------
Postgres persistence for events (aligned with the final schema).

New schema:

  CREATE TABLE public.events (
    id BIGSERIAL PRIMARY KEY,
    source_message_id TEXT NOT NULL,
    subject TEXT,
    sender TEXT,
    event_datetime TIMESTAMPTZ,
    location TEXT,
    raw_payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
  );

  CREATE UNIQUE INDEX IF NOT EXISTS ux_events_source_message_id
    ON public.events (source_message_id);

Idempotency is enforced by `ON CONFLICT (source_message_id) DO NOTHING`.
Uses psycopg v3 (install with: psycopg[binary]).
"""

from __future__ import annotations

import os
from typing import Optional, Dict, Any

import psycopg  # psycopg v3
from pydantic import BaseModel, Field


class ExtractedEvent(BaseModel):
    title: str = Field(..., description="Main subject/title")
    date_time: str = Field(..., description="ISO-8601 datetime (YYYY-MM-DDTHH:MM:SS[Z])")
    location: str | None = Field(None, description="Physical/virtual location")
    summary: str | None = Field(None, description="One-sentence purpose")


def _conn() -> psycopg.Connection:
    """
    Open a psycopg v3 connection. Neon URL should include ?sslmode=require.
    Autocommit True so each execute commits immediately.
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(url, autocommit=True)  # type: ignore[arg-type]


def ensure_unique_index(conn: psycopg.Connection) -> None:
    """Ensure idempotency index exists (safe to call anytime)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_events_source_message_id
              ON public.events (source_message_id);
            """
        )


def save_event(gmail_id: str, ev: ExtractedEvent) -> Optional[int]:
    """
    Insert 1 event into public.events using the v2 schema.

    Mapping from legacy fields:
      - subject           ← ev.title
      - event_datetime    ← ev.date_time (ISO-8601; Postgres will coerce)
      - location          ← ev.location
      - raw_payload       ← minimal JSON with summary + source echo
      - source_message_id ← gmail_id   (idempotency key)

    Returns inserted row id (int) if new; None if duplicate (conflict).
    """
    payload: Dict[str, Any] = {
        "source": "gmail",
        "source_message_id": gmail_id,
        "title": ev.title,
        "summary": ev.summary,
        "location": ev.location,
        "date_time": ev.date_time,
    }

    sql = """
    INSERT INTO public.events
      (source_message_id, subject, sender, event_datetime, location, raw_payload)
    VALUES
      (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_message_id) DO NOTHING
    RETURNING id;
    """

    with _conn() as conn:
        ensure_unique_index(conn)
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    gmail_id,      # source_message_id
                    ev.title,      # subject
                    None,          # sender (unknown)
                    ev.date_time,  # event_datetime (TIMESTAMPTZ)
                    ev.location,   # location
                    payload,       # raw_payload (JSONB)
                ),
            )
            row = cur.fetchone()
            return row[0] if row else None

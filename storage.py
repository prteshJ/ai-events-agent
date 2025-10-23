"""
storage.py
----------
Data model + persistence for events.

Table expected:
  events(
    id varchar PRIMARY KEY,           -- Gmail message ID
    title varchar,
    start varchar,                    -- ISO-8601 datetime string
    end varchar,                      -- optional, may remain NULL
    location varchar,
    description text,
    recurring boolean DEFAULT FALSE,
    created_at timestamptz DEFAULT now()  -- optional if present
  )

Notes:
- "end" is a reserved keyword in SQL; we must quote it.
- We also quote "start" for consistency.
- DATABASE_URL must be set (Neon value).
"""

from __future__ import annotations

import os
import psycopg2
from pydantic import BaseModel, Field


class ExtractedEvent(BaseModel):
    """Structured event produced by the parser."""
    title: str = Field(..., description="Main subject/title")
    date_time: str = Field(..., description="ISO-8601 datetime (YYYY-MM-DDTHH:MM:SS)")
    location: str | None = Field(None, description="Physical/virtual location")
    summary: str | None = Field(None, description="One-sentence purpose")


def _conn():
    """Open a new psycopg2 connection using DATABASE_URL."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(url)  # Neon URL already carries sslmode etc.


def save_event(gmail_id: str, ev: ExtractedEvent) -> None:
    """
    Upsert event row using Gmail message ID as the primary key.

    Maps:
      ev.title     -> title
      ev.date_time -> "start"
      ev.location  -> location
      ev.summary   -> description
      "end"        -> NULL (for now)
      recurring    -> FALSE

    On conflict, update the mutable fields.
    """
    sql = """
    INSERT INTO events (id, title, "start", "end", location, description, recurring)
    VALUES (%s, %s, %s, NULL, %s, %s, FALSE)
    ON CONFLICT (id) DO UPDATE SET
      title = EXCLUDED.title,
      "start" = EXCLUDED."start",
      location = EXCLUDED.location,
      description = EXCLUDED.description
    """
    args = (gmail_id, ev.title, ev.date_time, ev.location, ev.summary)

    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)

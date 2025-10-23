"""
storage.py
----------
Data model + persistence for events.

Schema (relevant columns) based on your Neon errors:
  events(
    id varchar PRIMARY KEY,           -- we use Gmail message ID
    title varchar,
    "start" varchar,                  -- ISO-8601 datetime string
    "end" varchar,                    -- optional, may be NULL
    location varchar,
    description text,
    recurring boolean DEFAULT FALSE,
    source_type varchar NOT NULL,     -- e.g., 'gmail'
    source_message_id varchar NOT NULL, -- original Gmail message id
    created_at timestamptz DEFAULT now() -- optional if present
  )

Notes:
- "start"/"end" are quoted since they're reserved words.
- We now set source_type='gmail' and source_message_id=<gmail_id> on insert/update.

Env:
- DATABASE_URL
"""

from __future__ import annotations
import os
import psycopg2
from pydantic import BaseModel, Field


class ExtractedEvent(BaseModel):
    title: str = Field(..., description="Main subject/title")
    date_time: str = Field(..., description="ISO-8601 datetime (YYYY-MM-DDTHH:MM:SS)")
    location: str | None = Field(None, description="Physical/virtual location")
    summary: str | None = Field(None, description="One-sentence purpose")


def _conn():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(url)  # Neon URL includes SSL params


def save_event(gmail_id: str, ev: ExtractedEvent) -> None:
    """
    Upsert event using Gmail message ID as the primary key.

    Maps:
      ev.title     -> title
      ev.date_time -> "start"
      ev.location  -> location
      ev.summary   -> description
      "end"        -> NULL (for now)
      recurring    -> FALSE
      source_type        -> 'gmail'
      source_message_id  -> gmail_id
    """
    sql = """
    INSERT INTO events (id, title, "start", "end", location, description, recurring, source_type, source_message_id)
    VALUES (%s, %s, %s, NULL, %s, %s, FALSE, %s, %s)
    ON CONFLICT (id) DO UPDATE SET
      title = EXCLUDED.title,
      "start" = EXCLUDED."start",
      location = EXCLUDED.location,
      description = EXCLUDED.description,
      source_type = EXCLUDED.source_type,
      source_message_id = EXCLUDED.source_message_id
    """
    args = (gmail_id, ev.title, ev.date_time, ev.location, ev.summary, "gmail", gmail_id)

    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)

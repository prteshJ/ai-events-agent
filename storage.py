"""
storage.py
----------
Data model + persistence for events.

Schema (relevant NOT NULL columns per Neon):
  id (PK), title, recurring, source_type, source_message_id, created_at, updated_at
Other columns we write:
  "start", "end", location, description

We:
- Quote "start"/"end" (reserved identifiers).
- Insert source_type='gmail' and source_message_id=<gmail_id>.
- Set created_at=NOW() on first insert.
- Set updated_at=NOW() on every upsert.

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
    return psycopg2.connect(url)  # Neon URL includes ssl params


def save_event(gmail_id: str, ev: ExtractedEvent) -> None:
    """
    Upsert event using Gmail message ID as primary key.

    Maps:
      ev.title     -> title
      ev.date_time -> "start"
      ev.location  -> location
      ev.summary   -> description

    Constant fields:
      "end"               -> NULL (for now)
      recurring           -> FALSE
      source_type         -> 'gmail'
      source_message_id   -> gmail_id
      created_at          -> NOW() on first insert
      updated_at          -> NOW() on every upsert
    """
    sql = """
    INSERT INTO events
      (id, title, "start", "end", location, description,
       recurring, source_type, source_message_id, created_at, updated_at)
    VALUES
      (%s, %s, %s, NULL, %s, %s,
       FALSE, %s, %s, NOW(), NOW())
    ON CONFLICT (id) DO UPDATE SET
      title              = EXCLUDED.title,
      "start"            = EXCLUDED."start",
      location           = EXCLUDED.location,
      description        = EXCLUDED.description,
      source_type        = EXCLUDED.source_type,
      source_message_id  = EXCLUDED.source_message_id,
      updated_at         = NOW()
    """
    args = (gmail_id, ev.title, ev.date_time, ev.location, ev.summary, "gmail", gmail_id)

    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)

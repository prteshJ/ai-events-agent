"""
storage.py
----------
Postgres persistence for events.

Your table has NOT NULL on: id, title, recurring, source_type, source_message_id, created_at, updated_at.
We satisfy those and also handle reserved identifiers "start"/"end".

INSERT columns:
  id, title, "start", "end", location, description, recurring, source_type, source_message_id, created_at, updated_at
UPSERT (ON CONFLICT id) updates:
  title, "start", location, description, source_type, source_message_id, updated_at
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

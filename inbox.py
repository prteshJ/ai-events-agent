"""
inbox.py — mock inbox (fake emails for testing)

What this file does
-------------------
- Provides an async function `get_inbox()` that returns a short list of
  fake "emails" (simple dicts). This lets you test the whole pipeline
  without connecting to Gmail yet.

Later (when ready)
------------------
- You will replace get_inbox() with a real Gmail version that uses
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, and GMAIL_REFRESH_TOKEN.
"""

from typing import List, Dict


async def get_inbox() -> List[Dict]:
    """
    Return a few fake emails.
    Fields used by parser.py:
      id, subject, snippet, body, source, (optional) location
    """
    return [
        {
            "id": "mock-001",
            "subject": "Daily Standup",
            "snippet": "15 minutes, blockers & priorities",
            "body": "Calendar invite for the daily standup",
            "source": "mock",
        },
        {
            "id": "mock-002",
            "subject": "Client Kickoff — Acme",
            "snippet": "Scope, timeline, owners",
            "body": "Kickoff agenda attached",
            "source": "mock",
            # "location": "Zoom"  # optional
        },
        {
            "id": "mock-003",
            "subject": "Notes from marketing",
            "snippet": "Campaign ideas and dates",
            "body": "Q4 themes, early thoughts",
            "source": "mock",
        },
    ]

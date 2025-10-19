"""
parser.py — turns each email into one or more event dictionaries.

Simple rule-based logic (no AI yet)
-----------------------------------
It looks at the email subject or text to guess:
- if it's a daily standup → recurring event
- if it's a kickoff → one-time client meeting
- otherwise → simple non-recurring note

Each returned dict must include:
  _id, title, start, end, location, description,
  recurring, recurrence_rule, source_type, source_message_id, source_snippet
"""

from datetime import datetime, timezone
from typing import Dict, List


def parse_email_to_events(email: Dict) -> List[Dict]:
    """Convert one email (a dict) into a list of event dicts."""
    subject = (email.get("subject") or "").lower()

    # Rule 1 – “standup” means recurring daily meeting
    if "standup" in subject:
        return [make_event(
            email=email,
            suffix="standup",
            title="Team Standup",
            start_time=today_iso(9, 30),
            end_time=today_iso(9, 45),
            recurring=True,
            rrule="FREQ=DAILY",
            description=email.get("snippet") or email.get("body"),
        )]

    # Rule 2 – “kickoff” means one-time client meeting
    if "kickoff" in subject:
        return [make_event(
            email=email,
            suffix="kickoff",
            title="Client Kickoff",
            start_time=today_iso(14, 0),
            end_time=today_iso(15, 0),
            recurring=False,
            rrule=None,
            description=email.get("snippet") or email.get("body"),
        )]

    # Rule 3 – everything else becomes a non-recurring note
    return [make_event(
        email=email,
        suffix="note",
        title=email.get("subject") or "Untitled",
        start_time=None,
        end_time=None,
        recurring=False,
        rrule=None,
        description=email.get("snippet") or email.get("body"),
    )]


# helper functions below
def make_event(email: Dict, suffix: str, title: str,
               start_time: str | None, end_time: str | None,
               recurring: bool, rrule: str | None,
               description: str | None) -> Dict:
    """Return a standardized event dict."""
    msg_id = email.get("id", "unknown")
    source = email.get("source", "mock")
    return {
        "_id": f"{source}-{msg_id}#{suffix}",
        "title": title,
        "start": start_time,
        "end": end_time,
        "location": email.get("location"),
        "description": description,
        "recurring": recurring,
        "recurrence_rule": rrule,
        "source_type": source,
        "source_message_id": msg_id,
        "source_snippet": email.get("snippet"),
    }


def today_iso(hour: int, minute: int) -> str:
    """Return today’s date/time in ISO format (UTC)."""
    now = datetime.now(timezone.utc)
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return t.isoformat()

"""
inbox.py
--------
Gmail client:
- Uses long-lived refresh token (no manual reauth).
- Fetches unread emails by query, returns list of {id, text}.

Environment:
- GMAIL_CLIENT_ID
- GMAIL_CLIENT_SECRET
- GMAIL_REFRESH_TOKEN
- GMAIL_QUERY        (default: "in:inbox is:unread newer_than:14d")
- GMAIL_MAX_RESULTS  (default: 50)
"""

from __future__ import annotations

import base64
import os
from typing import List, Dict

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _service():
    """
    Build a Gmail API client using a stored refresh token.

    Tip: your Railway env vars must contain the three values exactly as created
    during OAuth: client_id, client_secret, refresh_token.
    """
    creds = Credentials.from_authorized_user_info(
        {
            "client_id": os.getenv("GMAIL_CLIENT_ID"),
            "client_secret": os.getenv("GMAIL_CLIENT_SECRET"),
            "refresh_token": os.getenv("GMAIL_REFRESH_TOKEN"),
        },
        scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_unread_emails(max_results: int | None = None) -> List[Dict[str, str]]:
    """
    Return a list of dicts:
      [{"id": <gmail_id>, "text": <plain_text>}, ...]
    """
    query = os.getenv("GMAIL_QUERY", "in:inbox is:unread newer_than:14d")
    limit_env = os.getenv("GMAIL_MAX_RESULTS", "50")
    limit = int(limit_env) if max_results is None else max_results

    svc = _service()
    res = svc.users().messages().list(userId="me", q=query, maxResults=limit).execute()
    msgs = res.get("messages", [])

    out: List[Dict[str, str]] = []
    for m in msgs:
        mid = m["id"]
        full = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        payload = full.get("payload", {})
        # Prefer text/plain; fall back to first available data
        text = _extract_body_text(payload) or full.get("snippet", "") or ""
        out.append({"id": mid, "text": text})
    return out


def _extract_body_text(payload) -> str:
    """Extract the best-effort text/plain body from a Gmail payload."""
    def _decode(b64: str) -> str:
        try:
            return base64.urlsafe_b64decode(b64.encode("utf-8")).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    # Case 1: body on the root
    body = payload.get("body", {})
    if body.get("data"):
        return _decode(body["data"])

    # Case 2: walk parts to find text/plain
    parts = payload.get("parts") or []
    for p in parts:
        if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
            return _decode(p["body"]["data"])

    # Case 3: fallback to any first part with data
    for p in parts:
        if p.get("body", {}).get("data"):
            return _decode(p["body"]["data"])

    return ""

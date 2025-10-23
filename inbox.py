"""
inbox.py
--------
Gmail client using a long-lived refresh token.
Returns list of {"id": <gmail_id>, "text": <readable email text>}.

Env
---
GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
GMAIL_QUERY (optional; default: in:inbox is:unread newer_than:14d)
GMAIL_MAX_RESULTS (optional; default: 50)
"""

from __future__ import annotations

import base64
import os
import re
from html import unescape
from typing import List, Dict

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _service():
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
        text = _extract_body_text(payload) or full.get("snippet", "") or ""
        out.append({"id": mid, "text": text.strip()})
    return out


def _decode(b64: str) -> str:
    try:
        return base64.urlsafe_b64decode(b64.encode("utf-8")).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    if not html:
        return ""
    # remove scripts/styles
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    # convert <br> & </p> to newlines
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p\s*>", "\n", html)
    # strip all tags
    text = re.sub(r"(?s)<.*?>", "", html)
    # unescape and tidy whitespace
    text = unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_body_text(payload) -> str:
    """Best-effort extraction of readable text (handles HTML + nested multiparts)."""
    # Case 1: direct body
    body = payload.get("body", {})
    if body.get("data"):
        raw = _decode(body["data"])
        mt = payload.get("mimeType", "")
        return _strip_html(raw) if mt.startswith("text/html") else raw.strip()

    # Case 2: parts -> prefer text/plain
    parts = payload.get("parts") or []
    for p in parts:
        if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
            return _decode(p["body"]["data"]).strip()

    # Case 3: try text/html
    for p in parts:
        if p.get("mimeType") == "text/html" and p.get("body", {}).get("data"):
            return _strip_html(_decode(p["body"]["data"]))

    # Case 4: recurse nested multiparts
    for p in parts:
        if p.get("parts"):
            nested = _extract_body_text(p)
            if nested:
                return nested

    # Fallback: any data we can decode
    for p in parts:
        if p.get("body", {}).get("data"):
            return _decode(p["body"]["data"]).strip()

    return ""

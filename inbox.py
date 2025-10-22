"""
inbox.py — Read your Gmail UNREAD emails safely (drop-in replacement)

What this file does (plain English)
-----------------------------------
- Uses a long-lived "refresh token" from Google to request a short-lived access token.
- Calls the Gmail API to list UNREAD messages in your Inbox (configurable query).
- For each message, downloads content and extracts a clean text "body".
- Returns a simple list of dicts that the rest of your app already understands.

Why this is safe and simple
---------------------------
- Read-only scope: we only need `gmail.readonly` (no sending/deleting email).
- Works with just 3 environment variables you add in Railway.
- You do NOT change any other file — API, parser, DB all continue to work.

Environment variables (set in Railway → Variables)
--------------------------------------------------
- GMAIL_CLIENT_ID        ← from Google Cloud (OAuth client, type: Web application)
- GMAIL_CLIENT_SECRET    ← from Google Cloud
- GMAIL_REFRESH_TOKEN    ← from OAuth 2.0 Playground (or your own flow)
- (optional) GMAIL_QUERY        default: "in:inbox is:unread newer_than:14d"
- (optional) GMAIL_MAX_RESULTS  default: "100"

Return format (unchanged)
-------------------------
Each email becomes a dict like:
{
  "id": "<gmail_message_id>",
  "subject": "Subject line",
  "snippet": "Short preview from Gmail",
  "body": "Plain text if available; otherwise stripped HTML; otherwise snippet",
  "source": "gmail"
}

Usage in the rest of the app
----------------------------
Your FastAPI import route already calls:
    emails = await inbox.get_inbox()
No changes needed elsewhere.
"""

from __future__ import annotations

import os
import base64
import html
import re
import asyncio
from typing import Dict, List, Optional

import requests

# --- Google endpoints (fixed) -------------------------------------------------
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

# --- Read configuration from environment -------------------------------------
# These are set in Railway → Variables
GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN", "")

# Search query for Gmail — adjust in Railway without code changes
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox is:unread newer_than:14d")

# Safety cap to avoid pulling thousands of emails at once
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", "100"))


# =============================================================================
# Small helpers (single-purpose, heavily commented)
# =============================================================================

def _require_env() -> None:
    """
    Ensure required Gmail variables exist.
    If something is missing, we fail early with a friendly message.
    """
    missing = [
        name for name, val in {
            "GMAIL_CLIENT_ID": GMAIL_CLIENT_ID,
            "GMAIL_CLIENT_SECRET": GMAIL_CLIENT_SECRET,
            "GMAIL_REFRESH_TOKEN": GMAIL_REFRESH_TOKEN,
        }.items() if not val
    ]
    if missing:
        raise RuntimeError(
            "Missing Gmail configuration: "
            + ", ".join(missing)
            + ". Add them in Railway → Variables."
        )


def _get_access_token() -> str:
    """
    Convert our long-lived REFRESH token into a short-lived ACCESS token.
    We call this whenever we import — Google handles rotation/expiry under the hood.
    """
    _require_env()
    data = {
        "client_id": GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "refresh_token": GMAIL_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }
    resp = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=20)
    resp.raise_for_status()  # shows a clear error if credentials are wrong
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("Could not obtain access token from Google (no access_token in response).")
    return token


def _gmail_headers(token: str) -> Dict[str, str]:
    """Authorization header for Gmail REST calls."""
    return {"Authorization": f"Bearer {token}"}


def _list_unread_message_ids(token: str, q: str, cap: int) -> List[str]:
    """
    Ask Gmail for message IDs that match our query (e.g., UNREAD in Inbox).
    We only collect IDs here; we fetch bodies in a second call per message.
    """
    ids: List[str] = []
    url = f"{GMAIL_API_BASE}/users/me/messages"
    params = {"q": q, "maxResults": 100}  # Gmail may cap internally; we page as needed

    while True:
        r = requests.get(url, headers=_gmail_headers(token), params=params, timeout=20)
        r.raise_for_status()
        data = r.json()

        for m in data.get("messages", []) or []:
            ids.append(m["id"])
            if len(ids) >= cap:
                return ids

        page = data.get("nextPageToken")
        if not page:
            break
        params["pageToken"] = page

    return ids


def _get_message(token: str, msg_id: str) -> Dict:
    """Fetch one message with headers + parts (format=full)."""
    url = f"{GMAIL_API_BASE}/users/me/messages/{msg_id}"
    params = {"format": "full"}
    r = requests.get(url, headers=_gmail_headers(token), params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def _header(headers: List[Dict], name: str) -> Optional[str]:
    """Find a header value by name (case-insensitive), e.g. Subject, From, Date."""
    name_lower = name.lower()
    for h in headers or []:
        if h.get("name", "").lower() == name_lower:
            return h.get("value")
    return None


def _decode_b64url(data: str) -> bytes:
    """
    Gmail uses URL-safe base64 without padding.
    This adds the missing padding and decodes safely.
    """
    pad = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _extract_body(payload: Dict) -> str:
    """
    Return best-effort plain text:
    1) Prefer text/plain
    2) Else strip tags from text/html
    3) Else return empty and let caller fall back to snippet
    """
    # Case A: single-part plain text
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode_b64url(payload["body"]["data"]).decode("utf-8", errors="ignore")

    # Case B: multipart — walk all parts and gather text/plain or text/html
    stack = [payload]
    plain_candidates: List[str] = []
    html_candidates: List[str] = []

    while stack:
        node = stack.pop()
        for p in node.get("parts") or []:
            mt = (p.get("mimeType") or "").lower()
            body = p.get("body", {}) or {}
            data = body.get("data")

            # Some providers nest parts; still descend even if no data.
            if not data:
                stack.append(p)
                continue

            try:
                text = _decode_b64url(data).decode("utf-8", errors="ignore")
            except Exception:
                text = ""

            if mt.startswith("text/plain"):
                plain_candidates.append(text)
            elif mt.startswith("text/html"):
                html_candidates.append(text)

    if plain_candidates:
        return "\n".join(plain_candidates).strip()

    if html_candidates:
        html_text = "\n".join(html_candidates)
        # Minimal HTML strip to readable text
        text = re.sub(r"<[^>]+>", " ", html.unescape(html_text))
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    return ""


def _normalize_message(msg: Dict) -> Dict:
    """
    Convert Gmail's message JSON into the simple dict our app expects.
    """
    payload = msg.get("payload", {}) or {}
    headers = payload.get("headers", []) or []
    subject = _header(headers, "Subject") or "(no subject)"
    snippet = msg.get("snippet") or ""
    body = _extract_body(payload) or snippet  # fall back to snippet if body is empty

    return {
        "id": msg.get("id"),
        "subject": subject,
        "snippet": snippet,
        "body": body,
        "source": "gmail",
    }


def _fetch_unread_sync() -> List[Dict]:
    """
    Synchronous worker:
    - get access token
    - list unread IDs by query
    - fetch each message and normalize it
    """
    token = _get_access_token()
    ids = _list_unread_message_ids(token, GMAIL_QUERY, GMAIL_MAX_RESULTS)
    out: List[Dict] = []
    for mid in ids:
        m = _get_message(token, mid)
        out.append(_normalize_message(m))
    return out


# =============================================================================
# Public async entry point — matches your previous mock signature exactly
# =============================================================================

async def get_inbox() -> List[Dict]:
    """
    Async wrapper so the rest of your FastAPI code remains untouched.
    We run the sync work in a background thread for simplicity.
    """
    return await asyncio.to_thread(_fetch_unread_sync)

"""
parser.py
---------
Gemini-driven structured event extraction.

Design goals
- ZERO regex/rule logic: pure LLM extraction.
- Strict JSON: we force application/json so the model returns valid JSON.
- Minimal dependencies: only 'requests'.
- Safe defaults: low temperature, 30s timeout, defensive parsing.

Environment
- GEMINI_API_KEY (required)
- GEMINI_MODEL (optional; default: models/gemini-2.5-flash)

Output
- dict with keys: title (str), date_time (ISO str), location (str|None), summary (str|None)
"""

from __future__ import annotations

import os
import json
import requests
from typing import Optional, Dict

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "models/gemini-2.5-flash")
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

# System-style instruction kept short and unambiguous
_SYSTEM = (
    "Extract event details from the email and return STRICT JSON with keys:\n"
    "title (string, required),\n"
    "date_time (string ISO-8601: YYYY-MM-DDTHH:MM:SS, required),\n"
    "location (string or null),\n"
    "summary (string or null, one sentence).\n"
    "If date/time is missing, infer best-effort and state assumption in summary."
)


def extract_event(email_text: str) -> Optional[Dict[str, object]]:
    """
    Call Gemini to extract a structured event from raw text.

    Returns:
        dict with keys {title, date_time, location, summary} or None on failure.
    """
    if not GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY not set")
        return None

    if not email_text or not email_text.strip():
        return None

    payload = {
        "contents": [{"role": "user", "parts": [{"text": f"{_SYSTEM}\n\nEMAIL:\n{email_text}"}]}],
        "generationConfig": {
            # This forces the model to emit JSON (no prose around it)
            "response_mime_type": "application/json",
            "temperature": 0.2,
        },
    }

    try:
        resp = requests.post(API_URL, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        # With response_mime_type=application/json, model returns a JSON string in parts[0].text
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        obj = json.loads(text)

        title = str(obj.get("title", "")).strip()
        date_time = str(obj.get("date_time", "")).strip()
        location = obj.get("location")
        summary = obj.get("summary")

        if not title or not date_time:
            return None

        return {
            "title": title,
            "date_time": date_time,
            "location": (None if location in ("", "null", None) else str(location)),
            "summary": (None if summary in ("", "null", None) else str(summary)),
        }

    except Exception as e:
        print("❌ Gemini parse error:", repr(e))
        return None

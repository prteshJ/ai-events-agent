"""
parser.py
---------
Gemini-driven structured event extraction (hardened):
- Sanitizes GEMINI_MODEL (removes leading 'models/')
- Sanitizes GEMINI_API_KEY (trims, strips quotes, leading '=')
- Builds a correct v1beta generateContent URL
- Forces JSON output; recovers JSON if wrapped
- Normalizes date_time to ISO 'YYYY-MM-DDTHH:MM:SS'
- Prints rich HTTP errors for quick diagnosis
"""

from __future__ import annotations
import os, json, re, requests
from datetime import datetime
from typing import Optional, Dict

def _clean_model(raw: str | None) -> str:
    m = (raw or "gemini-2.5-flash").strip().strip('"').strip("'")
    # remove any accidental 'models/' prefix
    if m.startswith("models/"):
        m = m[len("models/"):]
    return m or "gemini-2.5-flash"

def _clean_key(raw: str | None) -> str:
    k = (raw or "").strip().strip('"').strip("'")
    # remove accidental leading '=' (seen in logs)
    while k.startswith("="):
        k = k[1:]
    return k

_GEMINI_MODEL = _clean_model(os.getenv("GEMINI_MODEL"))
_GEMINI_API_KEY = _clean_key(os.getenv("GEMINI_API_KEY"))

_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent?key={_GEMINI_API_KEY}"

_SYSTEM = (
    "Extract event details from the email and return STRICT JSON with keys: "
    "title (string), date_time (ISO 'YYYY-MM-DDTHH:MM:SS'), location (string|null), summary (string|null). "
    "If date/time is missing, infer a reasonable next occurrence today at 09:00 and state the assumption in summary."
)

def _normalize_iso(dt_str: str) -> Optional[str]:
    s = (dt_str or "").strip()
    if not s:
        return None
    fmts = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d-%m-%Y %H:%M",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
    ]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            if "H" not in f:  # date-only → default time
                dt = dt.replace(hour=9, minute=0, second=0)
            elif "S" not in f:  # minute precision → set seconds=0
                dt = dt.replace(second=0)
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return None

def _recover_json(text: str) -> Optional[Dict]:
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None
    return None

def extract_event(email_text: str) -> Optional[Dict[str, object]]:
    if not _GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY not set or invalid")
        return None
    if not email_text or not email_text.strip():
        return None

    payload = {
        "contents": [{"role": "user", "parts": [{"text": f"{_SYSTEM}\n\nEMAIL:\n{email_text}"}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.2},
    }

    try:
        r = requests.post(_API_URL, json=payload, timeout=30)
        # If Google returns an error, show status + body for fast debugging
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            print("❌ Gemini HTTP error:", r.status_code, r.text[:500])
            raise

        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        obj = _recover_json(text)
        if not isinstance(obj, dict):
            return None

        title = str(obj.get("title", "")).strip()
        dt = _normalize_iso(str(obj.get("date_time", "")).strip())
        location = obj.get("location")
        summary = obj.get("summary")

        if not title or not dt:
            return None

        return {
            "title": title,
            "date_time": dt,
            "location": (None if not location or str(location).strip().lower() in {"", "null", "none"} else str(location)),
            "summary": (None if not summary or str(summary).strip().lower() in {"", "null", "none"} else str(summary)),
        }
    except Exception as e:
        print("❌ Gemini parse error:", repr(e))
        return None

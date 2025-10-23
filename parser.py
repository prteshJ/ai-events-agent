# parser.py
"""
Gemini-driven structured event extraction
-----------------------------------------
- Uses Google Gemini 2.5 Flash
- Forces JSON output (response_mime_type='application/json')
- Returns a dict that matches our Pydantic model fields
"""

import os
import requests
from typing import Optional

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "models/gemini-2.5-flash")

API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

SYSTEM_MSG = """You extract event details from raw emails.
Return strict JSON with keys:
- title (string, required)
- date_time (string, required, ISO-8601: YYYY-MM-DDTHH:MM:SS)
- location (string or null)
- summary (string or null, one sentence)
If date/time is missing or ambiguous, best-effort infer; if impossible, set to a plausible next occurrence today at 09:00 local and mention assumption in summary.
"""

def extract_event(email_text: str) -> Optional[dict]:
    if not GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY missing")
        return None

    payload = {
        "contents": [

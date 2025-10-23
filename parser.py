"""
parser.py
---------
Gemini-driven extraction with quota-aware errors.

- Sanitizes GEMINI_MODEL (removes 'models/' if present)
- Sanitizes GEMINI_API_KEY (trims quotes/leading '=')
- Forces JSON via response_mime_type
- Recovers JSON if wrapped in text
- Normalizes date_time to 'YYYY-MM-DDTHH:MM:SS'
- Raises RetryableError(wait_seconds) on 429/503 to inform backoff
"""

from __future__ import annotations
import os, json, re, requests
from datetime import datetime
from typing import Optional, Dict

class RetryableError(Exception):
    def __init__(self, msg: str, wait_seconds: float | None = None):
        super().__init__(msg)
        self.wait_seconds = wait_seconds

def _clean_model(raw: str | None) -> str:
    m = (raw or "gemini-2.5-flash").strip().strip('"').strip("'")
    if m.startswith("models/"):
        m = m[len("models/"):]
    return m or "gemini-2.5-flash"

def _clean_key(raw: str | None) -> str:
    k = (raw or "").strip().strip('"').strip("'")
    while k.startsw

"""
src/utils/horse_norm.py

Canonical horse-name normalization for join-key matching.

Rules (applied in order):
  1. Coerce to string, lowercase, strip outer whitespace
  2. Replace '&' with 'and'
  3. Remove: apostrophes, periods, commas, hyphens, parentheses
  4. Collapse any run of whitespace to a single space, re-strip

Example
-------
  "O'Brien's Pride"   → "obriens pride"
  "Lady (IRE)"        → "lady ire"
  "Stitch & Time"     → "stitch and time"
  "Dr. Fong"          → "dr fong"
  "Zia--Runner"       → "zia runner"
"""
from __future__ import annotations

import re

_REMOVE = re.compile(r"['\.,\-\(\)]")
_WS     = re.compile(r"\s+")


def normalize_horse_name(name: str) -> str:
    s = str(name).lower().strip()
    s = s.replace("&", "and")
    s = _REMOVE.sub("", s)
    s = _WS.sub(" ", s).strip()
    return s

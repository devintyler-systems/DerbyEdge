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
import unicodedata

def horse_key(value: str) -> str:
    """Return the single canonical key used for horse identity joins.

    The key is deliberately strict.  Callers may surface fuzzy candidates to
    an operator, but must never use them as an automatic join.
    """
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii").upper()
    text = text.replace("&", " AND ")
    text = text.replace("'", "")
    text = re.sub(r"[^A-Z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def normalize_horse_name(name: str) -> str:
    # Preserve the historical lower-space representation for existing DB
    # lookups while deriving it from the same strict canonicalizer.
    return horse_key(name).replace("_", " ").lower()

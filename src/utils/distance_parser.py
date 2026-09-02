"""Canonical distance parser shared across all ingestion paths.

All functions operate on raw human-readable strings and return numeric values.
Callers are responsible for converting to yards via: int(round(furlongs * 220)).
"""
from __future__ import annotations

import re
from typing import Optional


def parse_furlongs(raw: Optional[str]) -> Optional[float]:
    """Parse any human distance string to a float number of furlongs.

    Handles:
      "4 1/2F"          → 4.5
      "4½F"             → 4.5   (Unicode half)
      "4.5F Dirt/Fast"  → 4.5
      "4.5 furlongs"    → 4.5
      "6F"              → 6.0
      "1M"              → 8.0
      "1 1/16 Miles"    → 8.5
      "1.25M"           → 10.0

    Returns None when the string cannot be parsed.
    """
    if not raw:
        return None
    s = str(raw).strip()
    # Normalise non-breaking spaces and unicode half-character.
    s = s.replace(" ", " ").replace("½", " 1/2")
    s = re.sub(r"\s+", " ", s).upper()

    # Integer-fraction furlongs: "4 1/2F", "4 1/2 FURLONGS"
    m = re.search(r"(?<!\d)(\d+)\s+1/2\s*(?:FURLONGS?|F\b)", s)
    if m:
        return float(m.group(1)) + 0.5

    # Decimal or integer furlongs: "4.5F", "6F", "6.5 FURLONGS"
    m = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:FURLONGS?|F\b)", s)
    if m:
        return float(m.group(1))

    # Integer-fraction miles: "1 1/16 MILES", "1 1/8M"
    m = re.search(r"(?<!\d)(\d+)\s+(\d+)/(\d+)\s*(?:MILES?|M\b)", s)
    if m:
        whole = int(m.group(1))
        frac  = int(m.group(2)) / int(m.group(3))
        return (whole + frac) * 8.0

    # Decimal or integer miles: "1M", "1.0 MILES"
    m = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:MILES?|M\b)", s)
    if m:
        val = float(m.group(1))
        if 0.25 <= val <= 3.0:          # sanity-gate: avoid matching purse digits
            return val * 8.0

    return None

"""Sportsbook screenshot ingestor — DerbyEdge Operator Console.

Parses a sportsbook race-card screenshot via Claude Vision and returns a
structured ParsedScreenshot. The caller (src/services/screenshot_ingest.py)
handles PP lookup and UI integration.

Honest limits:
    - Extraction produces a race shell with ZERO past-performance history.
    - Use parse_screenshot() to get structured data; the service layer
      does PP fuzzy-matching against the main DB.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    anthropic = None  # type: ignore
    _HAS_ANTHROPIC = False

DEFAULT_MODEL = "claude-sonnet-4-5"

EXTRACTION_PROMPT = """You are extracting structured data from a horse-racing
sportsbook race-card screenshot. Return ONE JSON object, no prose. Schema:

{
  "track_name": "string or null",
  "track_id": "2-4 letter Equibase code or null",
  "race_date": "ISO YYYY-MM-DD or null",
  "race_number": "integer or null",
  "post_time": "string or null (e.g. 7:47 PM EST)",
  "distance_text": "verbatim string or null (e.g. 1 Mile)",
  "surface": "D for dirt / T for turf / E for all-weather / null",
  "race_type": "string or null (e.g. Maiden Claiming)",
  "purse_usd": "integer or null",
  "book_id": "betonline/fanduel/draftkings/twinspires or null",
  "runners": [
    {
      "program_number": "string (REQUIRED)",
      "horse_name": "string (REQUIRED)",
      "post_position": "integer or null",
      "jockey": "string or null",
      "trainer": "string or null",
      "morning_line": "string or null (e.g. 5-2)",
      "current_odds_decimal": "float or null",
      "current_odds_american": "integer or null (e.g. 450 or -110)",
      "current_odds_fractional": "string or null (e.g. 9/2)",
      "is_scratched": false
    }
  ]
}

Rules:
- Output JSON only. No markdown fences. No commentary.
- If a field is unreadable or absent, return null (not empty string or 0).
- For odds: fill whichever ONE format is shown in the screenshot. Do not convert.
- track_id: only fill if you see an explicit Equibase code. Do not guess from name.
- Include every runner row, including scratches (set is_scratched to true).
"""

TRACK_NAME_TO_ID: dict[str, str] = {
    "aqueduct": "AQU", "belmont": "BEL", "saratoga": "SAR",
    "churchill downs": "CD", "keeneland": "KEE", "ellis park": "ELP",
    "del mar": "DMR", "santa anita": "SA", "los alamitos": "LRC",
    "gulfstream": "GP", "tampa bay": "TAM", "tampa bay downs": "TAM",
    "fair grounds": "FG", "oaklawn": "OP", "lone star": "LS",
    "hawthorne": "HAW", "arlington": "AP",
    "delaware park": "DEL", "laurel": "LRL", "pimlico": "PIM",
    "monmouth": "MTH", "parx": "PRX", "penn national": "PEN",
    "presque isle": "PID", "thistledown": "TDN",
    "mountaineer": "MNR", "mountaineer park": "MNR",
    "remington": "RP", "will rogers": "WRD", "fonner": "FON",
    "louisiana downs": "LAD", "evangeline": "EVD",
    "finger lakes": "FL", "turfway": "TP",
    "woodbine": "WO", "century mile": "CTM", "century downs": "CTD",
    "hastings": "HST",
}


@dataclass
class ParsedScreenshot:
    track_id: str | None = None
    track_name: str | None = None
    race_date: str | None = None
    race_number: int | None = None
    post_time: str | None = None
    distance_text: str | None = None
    surface: str | None = None
    race_type: str | None = None
    purse_usd: int | None = None
    book_id: str | None = None
    runners: list[dict[str, Any]] = field(default_factory=list)
    raw_response: str = ""

    def race_id(self) -> str:
        if not (self.track_id and self.race_date and self.race_number):
            raise ValueError(
                "Need track_id + race_date + race_number to build race_id. "
                f"Got track_id={self.track_id!r}, race_date={self.race_date!r}, "
                f"race_number={self.race_number!r}."
            )
        return f"{self.track_id}|{self.race_date}|{self.race_number}"


def _read_image_bytes(image: bytes | str | Path) -> tuple[bytes, str]:
    if isinstance(image, (str, Path)):
        p = Path(image)
        raw = p.read_bytes()
        ext = p.suffix.lower().lstrip(".")
    else:
        raw = image
        ext = ""
    media = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    }.get(ext, "image/jpeg")
    return raw, media


def _strip_json_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"```\s*$", "", s)
    return s.strip()


def _backfill_track_id(parsed: dict[str, Any]) -> dict[str, Any]:
    if parsed.get("track_id"):
        return parsed
    name = (parsed.get("track_name") or "").lower().strip()
    if name in TRACK_NAME_TO_ID:
        parsed["track_id"] = TRACK_NAME_TO_ID[name]
    return parsed


def parse_screenshot(
    image: bytes | str | Path,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
) -> ParsedScreenshot:
    """Send screenshot to Claude Vision; return structured ParsedScreenshot."""
    if not _HAS_ANTHROPIC:
        raise RuntimeError("anthropic SDK not installed. Run: pip install anthropic")
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Set it in your environment."
        )

    raw, media = _read_image_bytes(image)
    b64 = base64.standard_b64encode(raw).decode("ascii")

    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media, "data": b64,
                }},
                {"type": "text", "text": EXTRACTION_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    cleaned = _strip_json_fences(text)
    try:
        parsed_dict = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Vision returned non-JSON: {e}\n---\n{text[:500]}")

    parsed_dict = _backfill_track_id(parsed_dict)
    return ParsedScreenshot(
        track_id=parsed_dict.get("track_id"),
        track_name=parsed_dict.get("track_name"),
        race_date=parsed_dict.get("race_date"),
        race_number=parsed_dict.get("race_number"),
        post_time=parsed_dict.get("post_time"),
        distance_text=parsed_dict.get("distance_text"),
        surface=parsed_dict.get("surface"),
        race_type=parsed_dict.get("race_type"),
        purse_usd=parsed_dict.get("purse_usd"),
        book_id=parsed_dict.get("book_id"),
        runners=parsed_dict.get("runners", []) or [],
        raw_response=text,
    )

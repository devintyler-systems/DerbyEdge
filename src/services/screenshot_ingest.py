"""Screenshot ingest service for Operator Console.

Wraps src.derbyedge.screenshot_ingest (vision parse) and adds
PP fuzzy lookup for the extracted runners.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from src.services.pp_lookup import lookup_horses


def ingest_sportsbook_screenshot(
    image_bytes: bytes,
    conn: sqlite3.Connection,
    api_key: str | None = None,
    model: str = "claude-sonnet-4-5",
) -> dict[str, Any]:
    """Parse screenshot → fuzzy-match PPs → return display dict.

    Returns:
        {
          "ok": bool,
          "error": str | None,
          "track_name": str | None,
          "race_date": str | None,
          "race_number": int | None,
          "book_id": str | None,
          "runners": [
              {
                "program_number": str,
                "horse_name": str,
                "morning_line": str | None,
                "current_odds": str | None,
                "is_scratched": bool,
                "has_pp_history": bool,
                "matched_name": str | None,
                "match_score": float,
                "last_5": list[dict],
                "warning": str,
              }
          ],
        }
    """
    from src.derbyedge import screenshot_ingest as _si  # lazy — optional dep

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {
            "ok": False,
            "error": "ANTHROPIC_API_KEY not set. Set it in your environment or .env file.",
            "runners": [],
        }

    try:
        parsed = _si.parse_screenshot(image_bytes, api_key=key, model=model)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "runners": []}

    active_names = [
        r["horse_name"]
        for r in parsed.runners
        if r.get("horse_name") and not r.get("is_scratched")
    ]

    pp_matches = lookup_horses(conn, active_names)
    pp_by_name = {m.query_name: m for m in pp_matches}

    runners_out: list[dict] = []
    for r in parsed.runners:
        name = r.get("horse_name") or ""
        pm = pp_by_name.get(name)

        dec = r.get("current_odds_decimal")
        am = r.get("current_odds_american")
        frac = r.get("current_odds_fractional")
        if dec is not None:
            odds_str = f"{dec:.2f}"
        elif am is not None:
            odds_str = f"{am:+d}"
        elif frac:
            odds_str = frac
        else:
            odds_str = None

        runners_out.append({
            "program_number": r.get("program_number"),
            "horse_name": name,
            "morning_line": r.get("morning_line"),
            "current_odds": odds_str,
            "is_scratched": bool(r.get("is_scratched")),
            "has_pp_history": pm.has_pp_history if pm else False,
            "matched_name": pm.matched_name if pm else None,
            "match_score": pm.match_score if pm else 0.0,
            "last_5": pm.last_5 if pm else [],
            "warning": pm.warning if pm else "Not found in DB.",
        })

    return {
        "ok": True,
        "error": None,
        "track_name": parsed.track_name,
        "race_date": parsed.race_date,
        "race_number": parsed.race_number,
        "book_id": parsed.book_id,
        "runners": runners_out,
    }

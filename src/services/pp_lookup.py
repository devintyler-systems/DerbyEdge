"""Past-performance fuzzy lookup service.

Given horse names (e.g. extracted from a screenshot), fuzzy-matches them
against the horses table, then fetches last-5 PPs from v_horse_last_5.
"""
from __future__ import annotations

import difflib
import sqlite3
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PPMatch:
    query_name: str
    matched_name: str | None = None
    horse_id: int | None = None
    match_score: float = 0.0
    has_pp_history: bool = False
    last_5: list[dict[str, Any]] = field(default_factory=list)
    warning: str = ""


def lookup_horses(
    conn: sqlite3.Connection,
    horse_names: list[str],
    threshold: float = 0.72,
) -> list[PPMatch]:
    """Fuzzy-match each name; attach last-5 PPs for matched horses.

    Returns one PPMatch per name in horse_names, in the same order.
    """
    candidate_rows = conn.execute(
        "SELECT horse_id, name FROM horses"
    ).fetchall()
    if not candidate_rows:
        return [
            PPMatch(
                query_name=n,
                warning="horses table is empty — run the ingest pipeline first.",
            )
            for n in horse_names
        ]

    cand_ids = [r[0] for r in candidate_rows]
    cand_names = [r[1] for r in candidate_rows]

    results: list[PPMatch] = []
    for name in horse_names:
        matches = difflib.get_close_matches(name, cand_names, n=1, cutoff=threshold)
        if not matches:
            results.append(PPMatch(
                query_name=name,
                has_pp_history=False,
                warning="No matching horse found in DB — may be an external or new runner.",
            ))
            continue

        matched = matches[0]
        idx = cand_names.index(matched)
        horse_id = cand_ids[idx]
        score = difflib.SequenceMatcher(None, name.lower(), matched.lower()).ratio()

        pp_rows = conn.execute(
            """SELECT card_date, distance_furlongs, surface,
                      finish_position, speed_figure, beyer_figure, lengths_behind
               FROM v_horse_last_5
               WHERE horse_id = ?
               ORDER BY recency_rank""",
            (horse_id,),
        ).fetchall()
        last_5 = [dict(r) for r in pp_rows]
        has_pp = len(last_5) > 0

        if not has_pp:
            warning = (
                f"Matched '{matched}' (score {score:.2f}) but no race history — "
                "seed-only install or horse is unraced in this DB."
            )
        elif score < 0.90:
            warning = (
                f"Fuzzy match: '{name}' → '{matched}' (score {score:.2f}). "
                "Verify the name manually."
            )
        else:
            warning = ""

        results.append(PPMatch(
            query_name=name,
            matched_name=matched,
            horse_id=horse_id,
            match_score=round(score, 3),
            has_pp_history=has_pp,
            last_5=last_5,
            warning=warning,
        ))

    return results

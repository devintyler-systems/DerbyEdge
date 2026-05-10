"""
Horse profile helpers — career stats, connections stats, and speed figures
derived from firstbet_pp_starts / firstbet_career_stats / race_results.

All functions accept a live sqlite3.Connection (row_factory=sqlite3.Row)
and an entry_id (int).  Missing data is returned as None; functions never
raise on missing tables or rows.

NOTE: DerbyEdge Speed figures (source='de_derived') are internal metrics
computed from finish position relative to field size.  They are NOT
official Beyer or TimeForm speed figures.
"""
from __future__ import annotations

import sqlite3
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict[str, Any]:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _safe_div(num: float | None, den: float | None) -> float | None:
    try:
        return num / den if den else None
    except (TypeError, ZeroDivisionError):
        return None


def _perf_score(finish_pos: Any, field_size: Any) -> float | None:
    """Map (finish_pos, field_size) to a DerbyEdge Speed index on [60, 120].

    Formula:
        perf  = (field_size - finish_pos) / (field_size - 1)   [0 .. 1]
        score = 60 + 60 * perf                                  [60 .. 120]

    Win in any field = 120; last in any field = 60.
    NOT a Beyer figure — internal finish-position proxy only.
    """
    try:
        fp = int(finish_pos)
        fs = int(field_size)
    except (TypeError, ValueError):
        return None
    if fs <= 1 or fp < 1:
        return None
    fp = max(1, min(fp, fs))
    return round(60.0 + 60.0 * (fs - fp) / (fs - 1), 1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_horse_profile(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any]:
    """Return a horse profile dict for the given entry.

    Source priority (highest first):
      1. entries seed columns  (career_starts / wins / places / shows)
      2. firstbet_career_stats  (W/P/S percentages, recent-5 counts)
      3. firstbet_pp_starts     (per-start data — last 5 shown only)

    All dict values default to None when unavailable.  Returns {} if
    entry_id is not found.
    """
    # ── Entry + horse + owner ────────────────────────────────────────────────
    e = _row(conn, """
        SELECT e.entry_id, e.card_id, e.horse_id,
               e.career_starts, e.career_wins, e.career_places, e.career_shows,
               e.career_earnings,
               e.dirt_starts, e.dirt_wins, e.dist_starts, e.dist_wins,
               e.last_race_days, e.last_race_finish,
               e.stamina_index, e.pace_style,
               h.sire, h.dam,
               p.full_name AS owner
        FROM entries e
        JOIN  horses h ON e.horse_id  = h.horse_id
        LEFT JOIN people p ON e.owner_id = p.person_id
        WHERE e.entry_id = ?
    """, (entry_id,))
    if not e:
        return {}

    # ── firstbet_career_stats (table may not exist on old DBs) ──────────────
    try:
        fb = _row(conn, """
            SELECT career_win_pct, career_place_pct, career_itm_pct,
                   recent_5_itm, recent_5_wins
            FROM firstbet_career_stats WHERE entry_id = ?
        """, (entry_id,))
    except Exception:
        fb = {}

    # ── firstbet_pp_starts — last 5 starts in rank order ────────────────────
    try:
        pp = _rows(conn, """
            SELECT start_rank, race_date, track_code,
                   finish_position, field_size, surface, distance_text, race_class
            FROM firstbet_pp_starts WHERE entry_id = ?
            ORDER BY start_rank LIMIT 5
        """, (entry_id,))
    except Exception:
        pp = []

    # ── Derive counts from pp_starts -----------------------------------------
    pp_v      = [r for r in pp if r.get("finish_position") is not None]
    pp_wins   = sum(1 for r in pp_v if r["finish_position"] == 1)
    pp_places = sum(1 for r in pp_v if r["finish_position"] == 2)
    pp_shows  = sum(1 for r in pp_v if r["finish_position"] == 3)

    # ── Career counts (entries is primary; pp is last-5 only, not total) ────
    cs  = e.get("career_starts")
    cw  = e.get("career_wins")
    cp  = e.get("career_places")
    csh = e.get("career_shows")

    # ── Win% / ITM%  (entries > firstbet > pp_derived) ──────────────────────
    fb_wp = fb.get("career_win_pct")
    fb_ip = fb.get("career_itm_pct")
    if cs and cs > 0 and cw is not None:
        win_pct = round(_safe_div(cw, cs) or 0.0, 4)
        itm_pct = round(_safe_div((cw or 0) + (cp or 0) + (csh or 0), cs) or 0.0, 4)
        pct_src = "entries"
    elif fb_wp is not None:
        win_pct, itm_pct, pct_src = fb_wp, fb_ip, "firstbet"
    elif pp_v:
        n       = len(pp_v)
        win_pct = round(_safe_div(pp_wins, n) or 0.0, 4)
        itm_pct = round(_safe_div(pp_wins + pp_places + pp_shows, n) or 0.0, 4)
        pct_src = "pp_derived"
    else:
        win_pct = itm_pct = None
        pct_src = "none"

    # ── Dirt / distance last-5 (entries first; pp-derive if null) ───────────
    dirt_s = e.get("dirt_starts")
    if dirt_s is None:
        dirt_s = sum(1 for r in pp if (r.get("surface") or "").upper() == "D")

    dirt_w = e.get("dirt_wins")
    if dirt_w is None:
        dirt_w = sum(
            1 for r in pp_v
            if (r.get("surface") or "").upper() == "D" and r["finish_position"] == 1
        )

    # ── Last race (entries first; pp[0] fallback) ────────────────────────────
    lrd = e.get("last_race_days")
    lrf = e.get("last_race_finish")
    last_race_date = pp[0].get("race_date") if pp else None
    if lrf is None and pp:
        lrf = pp[0].get("finish_position")

    return {
        # Full career record (None when entries seed is absent)
        "career_starts":  cs,
        "career_wins":    cw,
        "career_places":  cp,
        "career_shows":   csh,
        # Last-5 counts from pp_starts (always computed when PPs exist)
        "last5_starts":   len(pp_v),
        "last5_wins":     pp_wins,
        "last5_places":   pp_places,
        "last5_shows":    pp_shows,
        # Career percentages (best available source)
        "career_win_pct": win_pct,
        "career_itm_pct": itm_pct,
        "pct_source":     pct_src,
        # Financials
        "lifetime_earnings": e.get("career_earnings"),
        # Surface / distance
        "dirt_last5_starts":     dirt_s,
        "dirt_last5_wins":       dirt_w,
        "distance_last5_starts": e.get("dist_starts"),
        "distance_last5_wins":   e.get("dist_wins"),
        # Last race
        "last_race_days":   lrd,
        "last_race_finish": lrf,
        "last_race_date":   last_race_date,
        # Recent-5 summary from firstbet PDF
        "recent_5_wins": fb.get("recent_5_wins"),
        "recent_5_itm":  fb.get("recent_5_itm"),
        # Bloodstock (from horses table; may be None for seed-only records)
        "sire":  e.get("sire"),
        "dam":   e.get("dam"),
        "owner": e.get("owner"),
        # Raw pp rows (list[dict], most-recent = index 0)
        "pp_starts": pp,
    }


def get_connections_stats(
    conn: sqlite3.Connection,
    trainer_id: int | None,
    jockey_id: int | None,
) -> dict[str, Any]:
    """Return win stats for trainer, jockey, and their combination.

    Source: race_results joined through entries (local ingested data only).
    sparse=True when < 5 starts are available; win_pct is still included
    but should be read with caution.
    """

    def _person_stats(id_col: str, person_id: int | None) -> dict[str, Any]:
        if not person_id:
            return {"starts": 0, "wins": 0, "win_pct": None, "sparse": True}
        try:
            row = _row(conn, f"""
                SELECT COUNT(*) AS starts,
                       SUM(CASE WHEN rr.official_finish = 1
                                 AND rr.is_disqualified = 0 THEN 1 ELSE 0 END) AS wins
                FROM race_results rr
                JOIN entries e ON rr.entry_id = e.entry_id
                WHERE e.{id_col} = ? AND rr.is_scratched = 0
            """, (person_id,))
        except Exception:
            return {"starts": 0, "wins": 0, "win_pct": None, "sparse": True}
        starts = int(row.get("starts") or 0)
        wins   = int(row.get("wins")   or 0)
        win_pct = round(wins / starts, 3) if starts > 0 else None
        return {"starts": starts, "wins": wins, "win_pct": win_pct, "sparse": starts < 5}

    def _combo_stats(t_id: int | None, j_id: int | None) -> dict[str, Any]:
        if not t_id or not j_id:
            return {"starts": 0, "wins": 0, "win_pct": None, "sparse": True}
        try:
            row = _row(conn, """
                SELECT COUNT(*) AS starts,
                       SUM(CASE WHEN rr.official_finish = 1
                                 AND rr.is_disqualified = 0 THEN 1 ELSE 0 END) AS wins
                FROM race_results rr
                JOIN entries e ON rr.entry_id = e.entry_id
                WHERE e.trainer_id = ? AND e.jockey_id = ? AND rr.is_scratched = 0
            """, (t_id, j_id))
        except Exception:
            return {"starts": 0, "wins": 0, "win_pct": None, "sparse": True}
        starts = int(row.get("starts") or 0)
        wins   = int(row.get("wins")   or 0)
        win_pct = round(wins / starts, 3) if starts > 0 else None
        return {"starts": starts, "wins": wins, "win_pct": win_pct, "sparse": starts < 5}

    return {
        "trainer": _person_stats("trainer_id", trainer_id),
        "jockey":  _person_stats("jockey_id",  jockey_id),
        "combo":   _combo_stats(trainer_id, jockey_id),
    }


def get_speed_figures(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any]:
    """Return speed figures for the given entry.

    Source priority:
      1. entries seed columns  ({last,best,avg}_speed_fig, beyer_fig)
      2. race_results.speed_figure for this horse's ingested past starts
      3. DerbyEdge Speed derived from firstbet_pp_starts finish positions
         (source='de_derived') — NOT an official speed figure.

    DerbyEdge Speed formula (source='de_derived'):
        score = 60 + 60 * (field_size - finish_pos) / (field_size - 1)
    Range: win=120, last=60.
    """
    # Step 1 — entries seed figures
    e = _row(conn, """
        SELECT e.horse_id,
               e.last_speed_fig, e.best_speed_fig, e.avg_speed_fig, e.beyer_fig
        FROM entries e WHERE e.entry_id = ?
    """, (entry_id,))
    if not e:
        return {"speed_last": None, "speed_best": None, "speed_avg": None,
                "beyer": None, "source": "none"}

    s_last = e.get("last_speed_fig")
    s_best = e.get("best_speed_fig")
    s_avg  = e.get("avg_speed_fig")
    beyer  = e.get("beyer_fig")

    if any(v is not None for v in (s_last, s_best, s_avg)):
        return {"speed_last": s_last, "speed_best": s_best, "speed_avg": s_avg,
                "beyer": beyer, "source": "entries"}

    # Step 2 — race_results speed figures for this horse's ingested starts
    horse_id = e.get("horse_id")
    if horse_id:
        try:
            rr_rows = _rows(conn, """
                SELECT rr.speed_figure
                FROM race_results rr
                JOIN entries e2 ON rr.entry_id = e2.entry_id
                WHERE e2.horse_id = ? AND rr.is_scratched = 0
                  AND rr.speed_figure IS NOT NULL
                ORDER BY rr.ingested_at DESC LIMIT 5
            """, (horse_id,))
            figs = [int(r["speed_figure"]) for r in rr_rows if r.get("speed_figure")]
            if figs:
                return {
                    "speed_last": figs[0],
                    "speed_best": max(figs),
                    "speed_avg":  round(sum(figs) / len(figs), 1),
                    "beyer":      None,
                    "source":     "race_results",
                }
        except Exception:
            pass

    # Step 3 — DerbyEdge Speed derived from pp_starts finish positions
    try:
        pp = _rows(conn, """
            SELECT finish_position, field_size
            FROM firstbet_pp_starts WHERE entry_id = ?
            ORDER BY start_rank LIMIT 5
        """, (entry_id,))
    except Exception:
        pp = []

    scores = [_perf_score(r.get("finish_position"), r.get("field_size")) for r in pp]
    scores = [s for s in scores if s is not None]

    if not scores:
        return {"speed_last": None, "speed_best": None, "speed_avg": None,
                "beyer": None, "source": "none"}

    return {
        "speed_last": scores[0],
        "speed_best": max(scores),
        "speed_avg":  round(sum(scores) / len(scores), 1),
        "beyer":      None,
        "source":     "de_derived",
    }

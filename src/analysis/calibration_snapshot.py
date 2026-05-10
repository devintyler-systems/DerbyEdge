"""Offline calibration snapshot builder.

Produces one CSV row per (run_id, card_id) with outcome, confidence, and
slice fields for post-hoc model performance analysis.

Usage:
    python -m src.analysis.calibration_snapshot
"""
from __future__ import annotations

import csv
import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

ROOT       = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "output"

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core SQL — extends _OUTCOMES_SQL with run_id, card_id, and confidence.
# Confidence fields are pulled from the rank-1 entry_scores row per run.
# ---------------------------------------------------------------------------
_SNAPSHOT_SQL = """
WITH base AS (
    SELECT
        sr.run_id, sr.card_id, sr.run_timestamp, sr.model_type,
        COALESCE(sr.chaos_active, sr.derby_override_active, 0) AS chaos_active,
        mr.model_name,
        t.abbrev    AS track_code,
        rc.card_date, rc.race_number, rc.distance_furlongs,
        rc.surface, rc.race_class,
        COALESCE(
            rc.field_size,
            (SELECT COUNT(*) FROM entries e2
             WHERE e2.card_id = rc.card_id AND e2.scratch_flag = 0)
        ) AS field_size,
        es.rank, es.horse_name, es.win_probability, es.morning_line_odds,
        COALESCE(rr.post_position, es.post_position) AS post_position,
        rr.finish_position, rr.official_finish,
        COALESCE(rr.is_scratched, e.scratch_flag, 0) AS is_scratched,
        COALESCE(rr.is_disqualified, 0)              AS is_disqualified,
        rr.official_odds_decimal,
        es.confidence_score,
        es.confidence_bucket,
        es.confidence_reasons,
        es.confidence_flag,
        es.missing_data_flag
    FROM score_runs sr
    JOIN model_registry mr ON sr.model_id   = mr.model_id
    JOIN race_cards     rc ON sr.card_id    = rc.card_id
    JOIN tracks          t ON rc.track_id   = t.track_id
    JOIN entry_scores   es ON es.run_id     = sr.run_id
    JOIN entries         e ON e.entry_id    = es.entry_id
    LEFT JOIN race_results rr
           ON rr.entry_id = es.entry_id AND rr.card_id = sr.card_id
    WHERE EXISTS (SELECT 1 FROM race_results x WHERE x.card_id = sr.card_id)
),
conf AS (
    -- Confidence comes from the model's rank-1 entry (top pick) per run.
    SELECT run_id, confidence_score, confidence_bucket, confidence_reasons,
           confidence_flag, missing_data_flag
    FROM base WHERE rank = 1
),
tp AS (
    SELECT run_id,
           horse_name         AS top_pick_name,
           win_probability    AS top_pick_win_prob,
           finish_position    AS top_pick_finish_pos,
           is_scratched       AS original_tp_scratched,
           CASE WHEN official_finish = 1 AND is_disqualified = 0 THEN 1 ELSE 0 END
               AS top_pick_won
    FROM base WHERE rank = 1
),
eff_tp_rk AS (
    SELECT run_id, horse_name, rank AS original_rank,
           finish_position, official_finish, is_disqualified,
           ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY rank) AS eff_rk
    FROM base WHERE is_scratched = 0
),
eff_tp AS (
    SELECT run_id,
           horse_name      AS effective_tp_name,
           original_rank   AS effective_tp_rank,
           finish_position AS effective_tp_finish,
           CASE WHEN official_finish = 1 AND is_disqualified = 0 THEN 1 ELSE 0 END
               AS effective_tp_won
    FROM eff_tp_rk WHERE eff_rk = 1
),
winner AS (
    SELECT run_id,
           horse_name            AS winner_name,
           official_odds_decimal AS winner_official_odds,
           rank                  AS winner_model_rank
    FROM base
    WHERE official_finish = 1 AND is_disqualified = 0 AND is_scratched = 0
),
ptf_rk AS (
    -- Tie-break: lowest post_position wins when odds are identical.
    SELECT run_id, horse_name, official_odds_decimal,
           rank            AS ptf_model_rank,
           win_probability AS ptf_model_win_prob,
           ROW_NUMBER() OVER (PARTITION BY run_id
                              ORDER BY official_odds_decimal, post_position) AS rk
    FROM base
    WHERE official_odds_decimal IS NOT NULL AND is_scratched = 0
),
ptf AS (
    SELECT run_id,
           horse_name            AS ptf_horse_name,
           official_odds_decimal AS ptf_odds,
           ptf_model_rank,
           ptf_model_win_prob
    FROM ptf_rk WHERE rk = 1
),
ml_rk AS (
    SELECT run_id, horse_name,
           rank AS ml_fav_model_rank,
           RANK() OVER (PARTITION BY run_id ORDER BY morning_line_odds) AS rk
    FROM base
    WHERE morning_line_odds IS NOT NULL AND is_scratched = 0
),
mf AS (
    SELECT run_id,
           horse_name      AS ml_fav_name,
           ml_fav_model_rank
    FROM ml_rk WHERE rk = 1
),
meta AS (
    SELECT DISTINCT run_id, card_id, run_timestamp, model_type, chaos_active,
                    model_name, track_code, card_date, race_number, distance_furlongs,
                    surface, race_class, field_size
    FROM base
)
SELECT
    meta.run_id,
    meta.card_id,
    meta.track_code,
    meta.card_date                                              AS race_date,
    meta.race_number,
    meta.distance_furlongs                                      AS distance_f,
    UPPER(SUBSTR(COALESCE(meta.surface, '?'), 1, 1))           AS surface_code,
    meta.race_class                                             AS race_type,
    meta.field_size,
    meta.model_type                                             AS quality_tier,
    meta.chaos_active,
    meta.model_name,
    meta.run_timestamp                                          AS run_created_at,
    tp.top_pick_name,
    tp.top_pick_win_prob,
    tp.top_pick_finish_pos,
    tp.top_pick_won,
    tp.original_tp_scratched,
    eff_tp.effective_tp_name,
    eff_tp.effective_tp_rank,
    eff_tp.effective_tp_finish,
    eff_tp.effective_tp_won,
    mf.ml_fav_name,
    mf.ml_fav_model_rank,
    CASE WHEN mf.ml_fav_name = winner.winner_name THEN 1 ELSE 0 END
                                                                AS ml_favorite_won,
    ptf.ptf_horse_name,
    ptf.ptf_odds,
    ptf.ptf_model_rank,
    ptf.ptf_model_win_prob,
    CASE WHEN ptf.ptf_horse_name = winner.winner_name THEN 1 ELSE 0 END
                                                                AS ptf_won,
    winner.winner_name,
    winner.winner_official_odds,
    winner.winner_model_rank,
    conf.confidence_score,
    conf.confidence_bucket,
    conf.confidence_reasons,
    conf.confidence_flag,
    conf.missing_data_flag
FROM meta
LEFT JOIN tp     ON tp.run_id     = meta.run_id
LEFT JOIN eff_tp ON eff_tp.run_id = meta.run_id
LEFT JOIN mf     ON mf.run_id     = meta.run_id
LEFT JOIN ptf    ON ptf.run_id    = meta.run_id
LEFT JOIN winner ON winner.run_id = meta.run_id
LEFT JOIN conf   ON conf.run_id   = meta.run_id
ORDER BY meta.card_date DESC, meta.run_timestamp DESC
"""

# Column names corresponding to the SELECT above — order must match.
_SQL_COLS = [
    "run_id", "card_id", "track_code", "race_date", "race_number", "distance_f",
    "surface_code", "race_type", "field_size", "quality_tier", "chaos_active",
    "model_name", "run_created_at",
    "top_pick_name", "top_pick_win_prob", "top_pick_finish_pos", "top_pick_won",
    "original_tp_scratched",
    "effective_tp_name", "effective_tp_rank", "effective_tp_finish", "effective_tp_won",
    "ml_fav_name", "ml_fav_model_rank", "ml_favorite_won",
    "ptf_horse_name", "ptf_odds", "ptf_model_rank", "ptf_model_win_prob", "ptf_won",
    "winner_name", "winner_official_odds", "winner_model_rank",
    "confidence_score", "confidence_bucket", "confidence_reasons",
    "confidence_flag", "missing_data_flag",
]

# Final column order written to the CSV.
SNAPSHOT_COLS: list[str] = [
    # Identifiers
    "run_id", "card_id",
    # Race metadata
    "track_code", "race_date", "race_number",
    "distance_f", "distance_bucket",
    "surface_code", "race_type", "field_size", "field_size_bucket",
    "quality_tier", "chaos_active", "model_name", "run_created_at",
    # Original model rank-1 pick
    "top_pick_name", "model_top_prob", "top_pick_hit",
    "top_pick_finish_pos", "original_tp_scratched",
    # Scratch-aware effective top pick
    "effective_tp_name", "effective_tp_rank", "effective_tp_finish", "effective_tp_won",
    # Race winner
    "winner_name", "winner_rank", "winner_official_odds", "implied_winner_prob",
    # ML favorite
    "ml_fav_name", "ml_fav_rank", "ml_favorite_won",
    # Post-time favorite
    "ptf_horse_name", "ptf_odds", "ptf_rank", "ptf_prob",
    "ptf_won", "ptf_aligned", "implied_ptf_prob",
    # Value overlays
    "value_gap_top_vs_ptf", "value_gap_ptf",
    # Alignment flags
    "model_top_is_ml_fav", "model_top_is_ptf",
    # Confidence
    "confidence_score", "confidence_score_bucket", "confidence_bucket",
    "confidence_reasons", "confidence_flag", "missing_data_flag",
]


# ---------------------------------------------------------------------------
# Bucketing helpers
# ---------------------------------------------------------------------------

def _dist_bucket(distance_f: float | None) -> str:
    if distance_f is None:
        return "unknown"
    return "sprint" if distance_f < 8.5 else "route"


def _field_size_bucket(field_size: int | None) -> str:
    if field_size is None:
        return "unknown"
    if field_size <= 6:
        return "small"
    if field_size <= 10:
        return "medium"
    return "large"


def _confidence_score_bucket(score: float | None) -> str | None:
    if score is None:
        return None
    if score < 0.40:
        return "0.0-0.4"
    if score < 0.60:
        return "0.4-0.6"
    if score < 0.80:
        return "0.6-0.8"
    return "0.8-1.0"


def _implied_prob(decimal_odds: float | None) -> float | None:
    if decimal_odds is None or decimal_odds <= 0:
        return None
    return round(1.0 / (decimal_odds + 1.0), 6)


# ---------------------------------------------------------------------------
# Derived-metric computation (mutates the row dict in place)
# ---------------------------------------------------------------------------

def _derive(row: dict[str, Any]) -> dict[str, Any]:
    top_prob  = row.get("top_pick_win_prob")
    ptf_odds  = row.get("ptf_odds")
    win_odds  = row.get("winner_official_odds")
    ptf_prob  = row.get("ptf_model_win_prob")
    top_name  = row.get("top_pick_name")
    ptf_name  = row.get("ptf_horse_name")
    ml_name   = row.get("ml_fav_name")

    implied_ptf = _implied_prob(ptf_odds)
    implied_win = _implied_prob(win_odds)

    row["distance_bucket"]          = _dist_bucket(row.get("distance_f"))
    row["field_size_bucket"]        = _field_size_bucket(row.get("field_size"))
    row["confidence_score_bucket"]  = _confidence_score_bucket(row.get("confidence_score"))
    row["implied_ptf_prob"]         = implied_ptf
    row["implied_winner_prob"]      = implied_win
    row["value_gap_top_vs_ptf"]     = (
        round(top_prob - implied_ptf, 6)
        if top_prob is not None and implied_ptf is not None else None
    )
    row["value_gap_ptf"]            = (
        round(ptf_prob - implied_ptf, 6)
        if ptf_prob is not None and implied_ptf is not None else None
    )
    row["ptf_aligned"]              = 1 if top_name and ptf_name and top_name == ptf_name else 0
    row["model_top_is_ml_fav"]      = 1 if top_name and ml_name  and top_name == ml_name  else 0
    row["model_top_is_ptf"]         = row["ptf_aligned"]
    row["top_pick_hit"]             = row.get("top_pick_won")
    row["winner_rank"]              = row.get("winner_model_rank")
    row["ml_fav_rank"]              = row.get("ml_fav_model_rank")
    row["ptf_rank"]                 = row.get("ptf_model_rank")
    row["model_top_prob"]           = top_prob
    row["ptf_prob"]                 = ptf_prob
    return row


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_snapshot(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return calibration rows for all races with ingested results.

    Each row corresponds to one (run_id, card_id) pair and contains
    outcome, market, confidence, and slice fields ready for analysis.
    """
    from src.utils.db import ensure_entry_scores_columns, ensure_score_runs_columns
    ensure_score_runs_columns(conn)
    ensure_entry_scores_columns(conn)

    raw = conn.execute(_SNAPSHOT_SQL).fetchall()
    result: list[dict[str, Any]] = []
    for r in raw:
        row = dict(zip(_SQL_COLS, r))
        row = _derive(row)
        result.append({col: row.get(col) for col in SNAPSHOT_COLS})
    return result


def write_snapshot(
    rows: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> Path:
    """Write rows to output/calibration_snapshot_YYYYMMDD.csv. Returns path."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = (today or date.today()).strftime("%Y%m%d")
    path  = OUTPUT_DIR / f"calibration_snapshot_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SNAPSHOT_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _log_summary(rows: list[dict[str, Any]]) -> None:
    tracks     = {r["track_code"] for r in rows if r.get("track_code")}
    dates      = {r["race_date"]  for r in rows if r.get("race_date")}
    race_types = {r["race_type"]  for r in rows if r.get("race_type")}
    log.info(
        "Snapshot: %d rows | %d tracks | %d race dates | %d race types",
        len(rows), len(tracks), len(dates), len(race_types),
    )
    slices: dict[tuple[Any, Any], int] = {}
    for r in rows:
        key = (r.get("confidence_bucket"), r.get("chaos_active"))
        slices[key] = slices.get(key, 0) + 1
    for (bucket, chaos), count in sorted(slices.items(), key=lambda x: (x[0][0] or "", x[0][1] or 0)):
        log.info(
            "  confidence_bucket=%-6s  chaos_active=%s  → %d rows",
            bucket, chaos, count,
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from src.utils.db import get_connection
    conn = get_connection()
    try:
        rows = build_snapshot(conn)
        _log_summary(rows)
        path = write_snapshot(rows)
        log.info("Written → %s", path)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

"""
src/services/observations.py

Persist per-starter labeled observations into starter_observations.

Each row joins:
  • entry_scores  (pre-race model output — the prediction)
  • race_results  (post-race result    — the label)
  • supporting tables for race/entry context

Public API
----------
append_observations(conn, card_id) -> int
    Upsert all scorable+labeled entries for card_id.
    Called automatically after ingest_results() succeeds.
    Returns number of rows written (0 if no results for this card yet).

backfill_all_observations(conn) -> int
    Backfill every card that has both entry_scores and race_results.
    Idempotent — uses INSERT OR REPLACE on (race_id, post).
"""
from __future__ import annotations

import logging
import sqlite3

from src.utils.horse_norm import normalize_horse_name as _norm_horse

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Query: join pre-race scores with post-race results
# ---------------------------------------------------------------------------
# Only rows where a race_result EXISTS are returned — that is the labeled set.
# Scratched runners that appear in race_results (is_scratched=1) are included
# with win_flag=0 so the model learns about pre-scratch signals.
# Runners not in race_results at all (implicit scratches from backfill) are
# excluded because we have no label row to anchor them.
_QUERY = """
SELECT
    rc.card_id                                                   AS race_id,
    rc.card_date                                                 AS race_date,
    t.abbrev                                                     AS track,
    rc.race_number                                               AS race_no,
    rc.surface,
    rc.distance_furlongs,
    CASE WHEN rc.distance_furlongs < 8.5 THEN 'sprint' ELSE 'route' END
                                                                 AS distance_bucket,
    COALESCE(rc.field_size,
        (SELECT COUNT(*) FROM entries fe
         WHERE fe.card_id = rc.card_id AND fe.scratch_flag = 0)) AS field_size,
    h.name                                                       AS horse,
    e.post_position                                              AS post,
    ptr.full_name                                                AS trainer,
    pjk.full_name                                               AS jockey,
    e.morning_line_odds                                          AS ml_odds,
    es.win_probability                                           AS pred_win_prob,
    es.fair_odds                                                 AS pred_fair_odds,
    es.rank                                                      AS pred_rank,
    es.model_edge                                                AS edge,
    es.bet_tag                                                   AS tag,
    es.pace_fit_score                                            AS pace_fit,
    es.form_score                                                AS form_score,
    es.surface_dist_fit                                          AS sudist_fit,
    es.chaos_score                                               AS chaos_pct,
    es.chaos_tier                                                AS tier,
    COALESCE(rr.is_scratched, e.scratch_flag, 0)                AS scratched,
    rr.official_finish                                           AS finish_pos,
    CASE WHEN rr.official_finish = 1
          AND COALESCE(rr.is_disqualified, 0) = 0
         THEN 1 ELSE 0 END                                       AS win_flag,
    rr.official_odds_decimal                                     AS off_odds,
    sr.model_type                                                AS model_version
FROM entry_scores  es
JOIN score_runs    sr  ON sr.run_id      = es.run_id
JOIN race_cards    rc  ON rc.card_id     = sr.card_id
JOIN tracks        t   ON t.track_id     = rc.track_id
JOIN entries       e   ON e.entry_id     = es.entry_id
JOIN horses        h   ON h.horse_id     = e.horse_id
LEFT JOIN people   ptr ON ptr.person_id  = e.trainer_id
LEFT JOIN people   pjk ON pjk.person_id  = e.jockey_id
-- Inner-join semantics via WHERE: only rows with a result row
JOIN race_results  rr  ON rr.card_id     = rc.card_id
                       AND rr.entry_id   = e.entry_id
WHERE rc.card_id = ?
"""

_INSERT = """
INSERT OR REPLACE INTO starter_observations (
    race_id, race_date, track, race_no,
    surface, distance_furlongs, distance_bucket, field_size,
    horse, horse_norm, post, trainer, jockey,
    ml_odds, pred_win_prob, pred_fair_odds, pred_rank,
    edge, tag, pace_fit, form_score, sudist_fit, chaos_pct, tier,
    scratched, finish_pos, win_flag, off_odds,
    model_version, source_prediction_file, source_result_file
) VALUES (
    :race_id, :race_date, :track, :race_no,
    :surface, :distance_furlongs, :distance_bucket, :field_size,
    :horse, :horse_norm, :post, :trainer, :jockey,
    :ml_odds, :pred_win_prob, :pred_fair_odds, :pred_rank,
    :edge, :tag, :pace_fit, :form_score, :sudist_fit, :chaos_pct, :tier,
    :scratched, :finish_pos, :win_flag, :off_odds,
    :model_version, NULL, NULL
)
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append_observations(conn: sqlite3.Connection, card_id: int) -> int:
    """Upsert labeled starter observations for *card_id*.

    Safe to call multiple times — INSERT OR REPLACE on (race_id, post).
    Returns the number of rows written.
    """
    from src.utils.db import ensure_starter_observations
    ensure_starter_observations(conn)

    rows = conn.execute(_QUERY, (card_id,)).fetchall()
    if not rows:
        _log.debug("append_observations: no labeled rows for card_id=%s", card_id)
        return 0

    n = 0
    for row in rows:
        d = dict(row)
        d["horse_norm"] = _norm_horse(d.get("horse") or "")
        conn.execute(_INSERT, d)
        n += 1
    conn.commit()
    _log.info("append_observations: wrote %d rows for card_id=%s", n, card_id)
    return n


def backfill_all_observations(conn: sqlite3.Connection) -> int:
    """Backfill observations for every race that has both scores and results.

    Idempotent — safe to run repeatedly.  Returns total rows written.
    """
    from src.utils.db import ensure_starter_observations
    ensure_starter_observations(conn)

    # Find cards that have at least one score run AND at least one result row.
    card_ids = [
        row[0] for row in conn.execute("""
            SELECT DISTINCT sr.card_id
            FROM   score_runs sr
            WHERE  EXISTS (
                SELECT 1 FROM race_results rr
                JOIN entries e ON e.entry_id = rr.entry_id
                WHERE e.card_id = sr.card_id
                  AND rr.official_finish IS NOT NULL
            )
            ORDER BY sr.card_id
        """).fetchall()
    ]

    total = 0
    for cid in card_ids:
        total += append_observations(conn, cid)

    _log.info(
        "backfill_all_observations: %d total rows across %d race(s)",
        total, len(card_ids),
    )
    return total

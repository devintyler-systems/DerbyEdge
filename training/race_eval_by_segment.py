"""Export race-level top-pick performance by segment from v_race_eval_tool_enriched."""

import csv
import os
from src.utils.db import get_connection

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")
SEGMENT_CSV = os.path.join(OUTPUT_DIR, "race_eval_by_segment.csv")
TIER_CSV = os.path.join(OUTPUT_DIR, "race_eval_by_tier.csv")

SEGMENT_COLS = [
    "tier_name", "surface", "dist_category", "field_size_bucket",
    "races", "tp_wins", "tp_win_rate", "avg_eff_tp_finish",
    "scratched_orig_tp_races", "scratched_orig_tp_win_rate",
]
TIER_COLS = [
    "tier_name", "races", "tp_wins", "tp_win_rate", "avg_eff_tp_finish",
    "scratched_orig_tp_races", "scratched_orig_tp_win_rate",
]

_BUCKET_CASE = """
    CASE
        WHEN field_size >= 13 THEN 'Full (13+)'
        WHEN field_size >= 10 THEN 'Large (10-12)'
        WHEN field_size >= 7  THEN 'Medium (7-9)'
        ELSE 'Small (<=6)'
    END
""".strip()

_SEGMENT_SQL = f"""
SELECT
    tier_name,
    surface,
    dist_category,
    {_BUCKET_CASE} AS field_size_bucket,
    COUNT(*)                                                    AS races,
    SUM(eff_tp_won)                                             AS tp_wins,
    ROUND(CAST(SUM(eff_tp_won) AS REAL) / COUNT(*), 4)         AS tp_win_rate,
    ROUND(AVG(CASE WHEN eff_tp_finish_pos IS NOT NULL
                   THEN CAST(eff_tp_finish_pos AS REAL) END), 2) AS avg_eff_tp_finish,
    SUM(orig_tp_scratched)                                      AS scratched_orig_tp_races,
    CASE WHEN SUM(orig_tp_scratched) > 0
         THEN ROUND(
                CAST(SUM(CASE WHEN orig_tp_scratched = 1 AND eff_tp_won = 1 THEN 1 ELSE 0 END) AS REAL)
                / SUM(orig_tp_scratched), 4)
         ELSE NULL
    END                                                         AS scratched_orig_tp_win_rate
FROM v_race_eval_tool_enriched
GROUP BY tier_name, surface, dist_category, field_size_bucket
ORDER BY tier_name, surface, dist_category, field_size_bucket
"""

_TIER_SQL = """
SELECT
    tier_name,
    COUNT(*)                                                    AS races,
    SUM(eff_tp_won)                                             AS tp_wins,
    ROUND(CAST(SUM(eff_tp_won) AS REAL) / COUNT(*), 4)         AS tp_win_rate,
    ROUND(AVG(CASE WHEN eff_tp_finish_pos IS NOT NULL
                   THEN CAST(eff_tp_finish_pos AS REAL) END), 2) AS avg_eff_tp_finish,
    SUM(orig_tp_scratched)                                      AS scratched_orig_tp_races,
    CASE WHEN SUM(orig_tp_scratched) > 0
         THEN ROUND(
                CAST(SUM(CASE WHEN orig_tp_scratched = 1 AND eff_tp_won = 1 THEN 1 ELSE 0 END) AS REAL)
                / SUM(orig_tp_scratched), 4)
         ELSE NULL
    END                                                         AS scratched_orig_tp_win_rate
FROM v_race_eval_tool_enriched
GROUP BY tier_name
ORDER BY races DESC, tier_name
"""


def _write_csv(path: str, cols: list, rows: list) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({c: row[c] for c in cols})
    return len(rows)


def main():
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(_SEGMENT_SQL)
        seg_rows = [dict(r) for r in cur.fetchall()]

        cur.execute(_TIER_SQL)
        tier_rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    seg_written = _write_csv(SEGMENT_CSV, SEGMENT_COLS, seg_rows)
    tier_written = _write_csv(TIER_CSV, TIER_COLS, tier_rows)

    total_races = sum(r["races"] for r in tier_rows)
    print(f"segment_rows_written : {seg_written}")
    print(f"tier_rows_written    : {tier_written}")
    print(f"total_races_covered  : {total_races}")


if __name__ == "__main__":
    main()

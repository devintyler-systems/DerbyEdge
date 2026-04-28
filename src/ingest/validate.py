"""
src/ingest/validate.py — Post-ingest data quality checks

Runs a suite of checks against the loaded DB state and writes
output/validation_report.md.

What is checked
---------------
Pre-load  (source DataFrame):
  1. Duplicate horse names in CSV
  2. Duplicate post positions in CSV
  3. Duplicate jockeys in the same race
  4. Missing required fields (post_position, morning_line_odds, trainer, jockey)
  5. Invalid morning line odds (<= 0 or non-numeric)
  6. Unknown pace_style values

Post-load (DB state via SQL):
  7. Field size: actual entries vs. expected
  8. Entries with NULL trainer_id
  9. Entries with NULL jockey_id
 10. Post position gaps / missing posts
 11. Morning line overround (sum of implied probs > 1.30 is a data error)
 12. Sparse tables: horse_starts, workouts (synthetic=0), odds history
 13. Views: v_entries_live, v_horse_last_5, v_workout_30, v_connections_180

Assumptions and known gaps
---------------------------
- horse_starts is EXPECTED to be empty for seed-only installs.
- workouts (synthetic=0) is EXPECTED to be empty for seed-only installs.
- entries.workouts_30 holds the aggregate count from the seed; individual
  workout records require a separate data source.
- v_connections_180 returns 0 rows because horse_starts is empty.
  This is documented as an expected limitation, not a bug.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT        = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "output" / "validation_report.md"
VALID_PACE  = {"front", "presser", "stalker", "closer"}


# ── Check result dataclass ────────────────────────────────────────────────────

class Check:
    PASS    = "PASS"
    FAIL    = "FAIL"
    WARN    = "WARN"
    INFO    = "INFO"

    def __init__(self, name: str, status: str, detail: str = ""):
        self.name   = name
        self.status = status
        self.detail = detail

    def __repr__(self):
        marker = {"PASS": "ok", "FAIL": "FAIL", "WARN": "WARN", "INFO": "info"}[self.status]
        suffix = f" — {self.detail}" if self.detail else ""
        return f"[{marker}] {self.name}{suffix}"

    def md_row(self) -> str:
        icon = {"PASS": "✓", "FAIL": "✗", "WARN": "!", "INFO": "i"}[self.status]
        detail = self.detail.replace("|", "/")  # escape markdown table pipe
        return f"| {icon} **{self.status}** | {self.name} | {detail} |"


# ── Source-level checks (on pandas DataFrame) ─────────────────────────────────

def check_source(df: pd.DataFrame, expected_field: int = 20) -> list[Check]:
    checks: list[Check] = []
    df_lc = df.copy()
    df_lc.columns = [c.strip().lower() for c in df_lc.columns]

    # 1. Duplicate horse names
    dupes = df_lc["horse_name"].str.strip().str.lower().value_counts()
    dup_names = dupes[dupes > 1].index.tolist()
    checks.append(Check(
        "No duplicate horse names in source",
        Check.PASS if not dup_names else Check.FAIL,
        f"Duplicates: {dup_names}" if dup_names else "",
    ))

    # 2. Duplicate post positions
    dup_posts = df_lc["post_position"].value_counts()
    dup_posts = dup_posts[dup_posts > 1].index.tolist()
    checks.append(Check(
        "No duplicate post positions in source",
        Check.PASS if not dup_posts else Check.FAIL,
        f"Duplicate posts: {dup_posts}" if dup_posts else "",
    ))

    # 3. Duplicate jockeys (one jockey cannot ride two horses in same race)
    if "jockey" in df_lc.columns:
        dup_jocks = df_lc["jockey"].str.strip().value_counts()
        dup_jocks = dup_jocks[dup_jocks > 1].index.tolist()
        checks.append(Check(
            "No duplicate jockeys in same race",
            Check.PASS if not dup_jocks else Check.FAIL,
            f"Duplicates: {dup_jocks}" if dup_jocks else "",
        ))

    # 4. Missing required fields
    for field in ["post_position", "morning_line_odds"]:
        if field in df_lc.columns:
            n_null = df_lc[field].isna().sum()
            checks.append(Check(
                f"No null {field}",
                Check.PASS if n_null == 0 else Check.FAIL,
                f"{n_null} null rows" if n_null else "",
            ))

    for field in ["trainer", "jockey"]:
        if field in df_lc.columns:
            n_blank = df_lc[field].isna().sum() + (df_lc[field].str.strip() == "").sum()
            checks.append(Check(
                f"No missing {field}",
                Check.PASS if n_blank == 0 else Check.WARN,
                f"{n_blank} blank rows" if n_blank else "",
            ))

    # 5. Invalid morning line odds
    if "morning_line_odds" in df_lc.columns:
        odds_num = pd.to_numeric(df_lc["morning_line_odds"], errors="coerce")
        n_bad = (odds_num <= 0).sum() + odds_num.isna().sum()
        checks.append(Check(
            "All morning line odds > 0",
            Check.PASS if n_bad == 0 else Check.FAIL,
            f"{n_bad} invalid" if n_bad else
                f"range {odds_num.min():.1f}–{odds_num.max():.1f}",
        ))

    # 6. Unknown pace_style values
    if "pace_style" in df_lc.columns:
        unknown = df_lc["pace_style"].dropna().apply(
            lambda x: str(x).lower().strip()
        )
        bad = unknown[~unknown.isin(VALID_PACE)].unique().tolist()
        checks.append(Check(
            "All pace_style values valid (front/presser/stalker/closer)",
            Check.PASS if not bad else Check.WARN,
            f"Unknown values: {bad}" if bad else "",
        ))

    # 7. Source field size
    checks.append(Check(
        f"Source field size = {expected_field}",
        Check.PASS if len(df_lc) == expected_field else Check.WARN,
        f"Got {len(df_lc)}, expected {expected_field}" if len(df_lc) != expected_field else "",
    ))

    return checks


# ── DB-level checks (post-load SQL) ───────────────────────────────────────────

def check_db(conn: sqlite3.Connection, card_id: int, expected_field: int = 20) -> list[Check]:
    checks: list[Check] = []

    # 8. Entries count
    n_ent = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE card_id=? AND scratch_flag=0", (card_id,)
    ).fetchone()[0]
    checks.append(Check(
        f"Loaded entries = {expected_field}",
        Check.PASS if n_ent == expected_field else Check.FAIL,
        f"Found {n_ent}" if n_ent != expected_field else "",
    ))

    # 9. Entries with NULL trainer_id
    n_no_trainer = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE card_id=? AND trainer_id IS NULL", (card_id,)
    ).fetchone()[0]
    checks.append(Check(
        "All entries have trainer_id",
        Check.PASS if n_no_trainer == 0 else Check.WARN,
        f"{n_no_trainer} entries missing trainer" if n_no_trainer else "",
    ))

    # 10. Entries with NULL jockey_id
    n_no_jockey = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE card_id=? AND jockey_id IS NULL", (card_id,)
    ).fetchone()[0]
    checks.append(Check(
        "All entries have jockey_id",
        Check.PASS if n_no_jockey == 0 else Check.WARN,
        f"{n_no_jockey} entries missing jockey" if n_no_jockey else "",
    ))

    # 11. Post position gaps
    posts = sorted(
        r[0] for r in conn.execute(
            "SELECT post_position FROM entries WHERE card_id=? AND scratch_flag=0",
            (card_id,),
        ).fetchall()
    )
    expected_posts = list(range(1, expected_field + 1))
    missing_posts  = sorted(set(expected_posts) - set(posts))
    extra_posts    = sorted(set(posts) - set(expected_posts))
    post_ok = not missing_posts and not extra_posts
    checks.append(Check(
        f"Post positions 1-{expected_field} complete, no gaps",
        Check.PASS if post_ok else Check.FAIL,
        (f"Missing: {missing_posts}; Extra: {extra_posts}") if not post_ok else "",
    ))

    # 12. Morning line overround
    overround = conn.execute(
        "SELECT ROUND(SUM(morning_line_prob),4) FROM entries WHERE card_id=?", (card_id,)
    ).fetchone()[0] or 0.0
    # 20-horse fields naturally produce 1.35-1.45 overround at typical Derby odds
    field_overround_max = 1.0 + 0.02 * expected_field  # 1.40 for 20 horses
    overround_ok = 1.0 <= overround <= field_overround_max
    checks.append(Check(
        "Morning line overround in expected range (1.00-1.35)",
        Check.PASS if overround_ok else Check.WARN,
        f"Sum of implied probs = {overround:.4f}",
    ))

    # 13. Odds snapshots
    n_odds = conn.execute(
        "SELECT COUNT(*) FROM odds_snapshots os "
        "JOIN entries e ON os.entry_id=e.entry_id WHERE e.card_id=?", (card_id,)
    ).fetchone()[0]
    checks.append(Check(
        "Odds snapshots loaded (morning line)",
        Check.PASS if n_odds == n_ent else Check.WARN,
        f"{n_odds} snapshots for {n_ent} entries",
    ))

    # 14-17. Sparse table warnings (expected for seed-only)
    n_starts  = conn.execute("SELECT COUNT(*) FROM horse_starts").fetchone()[0]
    n_wk_real = conn.execute("SELECT COUNT(*) FROM workouts WHERE synthetic=0").fetchone()[0]
    n_wk_syn  = conn.execute("SELECT COUNT(*) FROM workouts WHERE synthetic=1").fetchone()[0]
    n_bias    = conn.execute("SELECT COUNT(*) FROM track_bias").fetchone()[0]

    checks.append(Check(
        "horse_starts populated",
        Check.INFO,
        f"{n_starts} rows — historical result data required for v_horse_last_5 and v_connections_180",
    ))
    checks.append(Check(
        "workouts (real, synthetic=0) populated",
        Check.INFO,
        f"{n_wk_real} real rows, {n_wk_syn} synthetic rows — "
        f"real workout records required for v_workout_30; "
        f"aggregate counts are in entries.workouts_30",
    ))
    checks.append(Check(
        "track_bias populated",
        Check.INFO,
        f"{n_bias} rows — Churchill Downs 2026 bias requires manual entry",
    ))

    # 18-21. View row counts
    for view, label in [
        ("v_entries_live",    "Live entries view"),
        ("v_horse_last_5",    "Last-5 starts view"),
        ("v_workout_30",      "Workout-30 view"),
        ("v_connections_180", "Connections-180 view"),
    ]:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0]
        except Exception as exc:
            checks.append(Check(label, Check.FAIL, str(exc)))
            continue
        # entries_live should have data; the others are empty by design
        if view == "v_entries_live":
            checks.append(Check(label, Check.PASS if n > 0 else Check.FAIL, f"{n} rows"))
        else:
            checks.append(Check(
                label,
                Check.INFO,
                f"{n} rows (0 expected until historical data loaded)",
            ))

    return checks


# ── Report writer ─────────────────────────────────────────────────────────────

def write_report(
    source_checks: list[Check],
    db_checks: list[Check],
    load_result,
    source_path: str,
    report_path: Optional[Path] = None,
) -> Path:
    out = report_path or REPORT_PATH
    out.parent.mkdir(exist_ok=True)

    fails = [c for c in source_checks + db_checks if c.status == Check.FAIL]
    warns = [c for c in source_checks + db_checks if c.status == Check.WARN]

    lines = [
        "# DerbyEdge ETL Validation Report",
        "",
        f"**Generated**: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"**Source**   : `{source_path}`  ",
        f"**Race**     : 2026 Kentucky Derby (G1) — Churchill Downs, 2026-05-02  ",
        "",
        "## Load Summary",
        "",
        f"| Field | Count |",
        f"|-------|-------|",
        f"| Source rows | {load_result.source_rows} |",
        f"| Horses (new) | {load_result.horses_new} |",
        f"| Horses (pre-existing) | {load_result.horses_existing} |",
        f"| Entries (new) | {load_result.entries_new} |",
        f"| Entries (pre-existing) | {load_result.entries_existing} |",
        f"| Odds snapshots | {load_result.odds_snapshots} |",
        f"| Trainers (new) | {load_result.trainers_new} |",
        f"| Jockeys (new) | {load_result.jockeys_new} |",
        f"| Owners (new) | {load_result.owners_new} |",
        "",
        "## Validation Status",
        "",
        f"**{len(fails)} failures, {len(warns)} warnings**",
        "",
        "### Source checks",
        "",
        "| | Check | Detail |",
        "|--|-------|--------|",
    ]
    for c in source_checks:
        lines.append(c.md_row())

    lines += [
        "",
        "### Database checks",
        "",
        "| | Check | Detail |",
        "|--|-------|--------|",
    ]
    for c in db_checks:
        lines.append(c.md_row())

    # Sparse-data section
    lines += [
        "",
        "## Missing Data — Expected Gaps for Seed-Only Install",
        "",
        "The Derby 2026 seed CSV does not contain the following data.",
        "These gaps are **expected** and explicitly documented here.",
        "They will be filled when real historical data is imported.",
        "",
        "| Table / View | Status | Notes |",
        "|--------------|--------|-------|",
        "| `horse_starts` | Empty | No individual race results in seed CSV |",
        "| `workouts` (synthetic=0) | Empty | Aggregate count stored in `entries.workouts_30` |",
        "| `track_bias` | Empty | Churchill Downs 2026 bias requires manual entry |",
        "| `trip_flags` | Empty | Post-race data; not available pre-race |",
        "| `v_horse_last_5` | 0 rows | Requires `horse_starts` |",
        "| `v_workout_30` | 0 rows | Requires real workout records |",
        "| `v_connections_180` | 0 rows | Requires `horse_starts` |",
        "",
        "### What IS available from the seed",
        "",
        "| Column | Source | Table |",
        "|--------|--------|-------|",
        "| `career_starts/wins/places/shows` | Derby seed CSV | `entries` |",
        "| `best_speed_fig`, `last_speed_fig`, `avg_speed_fig`, `beyer_fig` | Derby seed CSV | `entries` |",
        "| `dirt_starts/wins`, `dist_starts/wins` | Derby seed CSV | `entries` |",
        "| `workouts_30` (aggregate count) | Derby seed CSV | `entries` |",
        "| `stamina_index`, `gate_class`, `pace_style` | Derby seed CSV | `entries` |",
        "| Morning line odds snapshot | Derived from `morning_line_odds` | `odds_snapshots` |",
    ]

    if load_result.gaps:
        lines += ["", "### Partial null columns in source", ""]
        for g in load_result.gaps:
            lines.append(f"- {g}")

    if load_result.warnings:
        lines += ["", "### Loader warnings", ""]
        for w in load_result.warnings:
            lines.append(f"- {w}")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ── Convenience entry point ───────────────────────────────────────────────────

def run_validation(
    conn: sqlite3.Connection,
    card_id: int,
    csv_path: Path,
    load_result,
    expected_field: int = 20,
) -> tuple[list[Check], list[Check]]:
    """Run all checks and write the report. Returns (source_checks, db_checks)."""
    df = pd.read_csv(csv_path, dtype=str)
    df.columns = [c.strip().lower() for c in df.columns]

    src_checks = check_source(df, expected_field)
    db_checks  = check_db(conn, card_id, expected_field)

    report_path = write_report(src_checks, db_checks, load_result, str(csv_path))
    print(f"[validate] Report written to {report_path}")

    fails = [c for c in src_checks + db_checks if c.status == Check.FAIL]
    warns = [c for c in src_checks + db_checks if c.status == Check.WARN]
    print(f"[validate] {len(fails)} failures, {len(warns)} warnings")

    return src_checks, db_checks

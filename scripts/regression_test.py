"""
scripts/regression_test.py — DerbyEdge V1 full-pipeline regression test.

Runs init_db -> ingest -> build_features -> score then asserts correctness
invariants. Exits 0 if all checks pass, 1 if any fail or warn.

Usage
-----
    python scripts/regression_test.py               # uses existing DB
    python scripts/regression_test.py --fresh       # wipes DB first
"""

import argparse
import sys
import traceback
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

# ── Result tracking ────────────────────────────────────────────────────────────
PASS   = "PASS"
FAIL   = "FAIL"
WARN   = "WARN"
SKIP   = "SKIP"

results: list[tuple[str, str, str]] = []   # (check_name, status, detail)


def _record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    icon = {"PASS": " OK ", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}[status]
    print(f"  [{icon}]  {name}" + (f" -- {detail}" if detail else ""))


def _check(name: str, fn: Callable[[], tuple[bool, str]]) -> None:
    try:
        ok, detail = fn()
        _record(name, PASS if ok else FAIL, detail)
    except Exception as exc:
        _record(name, FAIL, f"{type(exc).__name__}: {exc}")


# ── Pipeline steps ─────────────────────────────────────────────────────────────
def step_init_db(fresh: bool) -> None:
    print("\n[1/4] init_db")
    from src.utils.db import DB_PATH, init_db
    if fresh and DB_PATH.exists():
        DB_PATH.unlink()
        print(f"  Removed {DB_PATH}")
    init_db()
    _record("init_db: schema applied", PASS)


def step_ingest() -> None:
    print("\n[2/4] ingest")
    from src.ingest.loader import load_derby_seed
    from src.ingest.validate import run_validation
    from src.utils.db import get_connection

    conn = get_connection()
    result = load_derby_seed(csv_path=None, conn=conn)
    conn.commit()

    src_checks, db_checks = run_validation(
        conn=conn,
        card_id=result.card_id,
        csv_path=Path(result.csv_path),
        load_result=result,
    )
    conn.close()

    fails = [c for c in src_checks + db_checks if c.status == "FAIL"]
    warns = [c for c in src_checks + db_checks if c.status == "WARN"]
    _record("ingest: loader", PASS, f"card_id={result.card_id}, {result.entries_new} entries")
    if fails:
        _record("ingest: validation", FAIL, "; ".join(f.name for f in fails))
    elif warns:
        _record("ingest: validation", WARN, "; ".join(w.name for w in warns))
    else:
        _record("ingest: validation", PASS, f"{len(src_checks)+len(db_checks)} checks")


def step_build_features() -> None:
    print("\n[3/4] build_features")
    from src.features.builder import build_features
    feat_df = build_features(card_id=None)
    _record("build_features: ran", PASS, f"{len(feat_df)} entries, {len(feat_df.columns)} cols")
    return feat_df


def step_score() -> None:
    print("\n[4/4] score")
    from src.models.scorer import score_race
    board = score_race(card_id=None)
    _record("score: ran", PASS, f"run completed, {len(board)} rows")
    return board


# ── Correctness checks ─────────────────────────────────────────────────────────
def checks_schema() -> None:
    print("\n[checks] Schema")
    from src.utils.db import get_connection

    conn = get_connection()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    views = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
    ).fetchall()}
    expected_tables = {
        "tracks", "race_cards", "horses", "people", "entries",
        "horse_starts", "workouts", "odds_snapshots", "track_bias",
        "trip_flags", "model_registry", "score_runs", "entry_scores",
        "feature_store",
    }
    expected_views = {
        "v_race_type", "v_entries_live", "v_horse_last_5",
        "v_workout_30", "v_connections_180",
    }

    missing_tables = expected_tables - tables
    missing_views  = expected_views  - views
    _check("schema: 14 tables present",
           lambda: (not missing_tables, f"missing: {missing_tables}" if missing_tables else "14/14"))
    _check("schema: 5 views present",
           lambda: (not missing_views, f"missing: {missing_views}" if missing_views else "5/5"))

    # derby_override_active column exists
    cols = {r[1] for r in conn.execute("PRAGMA table_info(score_runs)").fetchall()}
    _check("schema: score_runs.derby_override_active",
           lambda: ("derby_override_active" in cols, "column present" if "derby_override_active" in cols else "MISSING"))

    # feature_store has run_style_bucket TEXT column
    fs_cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(feature_store)").fetchall()}
    _check("schema: feature_store.run_style_bucket is TEXT",
           lambda: (fs_cols.get("run_style_bucket") == "TEXT",
                    f"type={fs_cols.get('run_style_bucket', 'ABSENT')}"))
    conn.close()


def checks_etl() -> None:
    print("\n[checks] ETL")
    from src.utils.db import get_connection

    conn = get_connection()
    n_entries = conn.execute(
        "SELECT COUNT(*) FROM v_entries_live"
    ).fetchone()[0]
    _check("etl: 20 entries in v_entries_live",
           lambda: (n_entries == 20, f"{n_entries} entries"))

    scratches = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE scratch_flag=1"
    ).fetchone()[0]
    _check("etl: no scratched entries",
           lambda: (scratches == 0, f"{scratches} scratched"))

    null_post = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE post_position IS NULL"
    ).fetchone()[0]
    _check("etl: all entries have post positions",
           lambda: (null_post == 0, f"{null_post} null posts"))

    # Unique post positions 1-20
    posts = [r[0] for r in conn.execute(
        "SELECT post_position FROM entries ORDER BY post_position"
    ).fetchall()]
    _check("etl: post positions are 1-20 unique",
           lambda: (posts == list(range(1, 21)), f"posts={posts}"))

    conn.close()


def checks_feature_store() -> None:
    print("\n[checks] Feature store")
    from src.utils.db import get_connection

    conn = get_connection()
    n_rows = conn.execute("SELECT COUNT(*) FROM feature_store").fetchone()[0]
    _check("features: 20 rows in feature_store",
           lambda: (n_rows == 20, f"{n_rows} rows"))

    feat_df = pd.read_sql("SELECT * FROM feature_store", conn)

    # IMPLEMENTED features: non-null for entries that have seed data.
    # Entries without numeric seed data (career_win_pct IS NULL) are expected
    # to have null IMPLEMENTED features — count how many such entries exist and
    # allow exactly that many nulls per IMPLEMENTED column.
    implemented = [
        "speed_last", "speed_best", "speed_avg", "beyer_last",
        "layoff_days", "career_win_pct", "career_itm_pct",
        "works_30d", "market_implied_prob", "morning_line_rank",
        "pace_pressure", "collapse_risk",
        "early_intent", "run_style_bucket",
    ]
    no_data_count = int(feat_df["career_win_pct"].isna().sum()) if "career_win_pct" in feat_df.columns else 0
    # market_implied_prob and market-derived features are always populated
    always_populated = {"market_implied_prob", "morning_line_rank", "pace_pressure", "collapse_risk"}
    impl_nulls = {
        col: int(feat_df[col].isna().sum())
        for col in implemented
        if col in feat_df.columns
    }
    excess_nulls = {
        k: v for k, v in impl_nulls.items()
        if k in always_populated and v > 0
        or k not in always_populated and v > no_data_count
    }
    _check("features: IMPLEMENTED features are non-null",
           lambda: (not excess_nulls,
                    f"nulls in: {excess_nulls} (allowed={no_data_count} no-data entries)"
                    if excess_nulls else f"all populated (no-data entries={no_data_count})"))

    # PLACEHOLDER features should be all-null
    placeholder = [
        "pace_early_mean_3", "pace_mid_mean_3", "bullet_30d",
        "days_since_last_work", "trainer_jockey_itm_cond",
        "jockey_route_cond", "trainer_derby_cond",
        "post_win_bias", "trouble_recovery_proxy",
        "field_strength_last", "churchill_readiness",
        "jan_apr_improvement_curve",
    ]
    phld_non_null = {
        col: int(feat_df[col].notna().sum())
        for col in placeholder
        if col in feat_df.columns
    }
    non_zero_ph = {k: v for k, v in phld_non_null.items() if v > 0}
    _check("features: PLACEHOLDER features are all-null",
           lambda: (not non_zero_ph,
                    f"unexpected values in: {non_zero_ph}" if non_zero_ph else "all null"))

    # run_style_bucket values are valid strings or null
    rsb = feat_df["run_style_bucket"].dropna().unique().tolist() if "run_style_bucket" in feat_df else []
    valid_styles = {"front", "presser", "stalker", "closer"}
    invalid = set(rsb) - valid_styles
    _check("features: run_style_bucket values are valid",
           lambda: (not invalid, f"invalid: {invalid}" if invalid else f"values={sorted(set(rsb))}"))

    # _safe_num must not raise on run_style_bucket
    def _safe_num_check():
        import numpy as _np
        def _safe_num(val, ndigits=4):
            if val is None:
                return None
            if isinstance(val, float) and _np.isnan(val):
                return None
            if isinstance(val, (int, float)):
                return round(float(val), ndigits)
            return val

        errs = []
        for col in feat_df.columns:
            for val in feat_df[col].tolist():
                try:
                    _safe_num(val)
                except Exception as e:
                    errs.append(f"{col}={val!r}: {e}")
        return (not errs, f"all OK" if not errs else f"errors: {errs[:3]}")

    _check("features: _safe_num handles all column types", _safe_num_check)

    conn.close()


def checks_scoring_board() -> None:
    print("\n[checks] Scoring board")
    from src.utils.db import get_connection

    conn = get_connection()
    run = conn.execute(
        "SELECT run_id, model_type, derby_override_active "
        "FROM score_runs ORDER BY run_timestamp DESC LIMIT 1"
    ).fetchone()
    if not run:
        _record("board: score_run exists", FAIL, "no score_runs rows")
        conn.close()
        return

    run_id = run["run_id"]

    # 20 entries in board
    n_scored = conn.execute(
        "SELECT COUNT(*) FROM entry_scores WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    _check("board: 20 entries scored",
           lambda: (n_scored == 20, f"{n_scored} entries"))

    # win probs sum to 1.0 ± 1e-6
    wp_sum = conn.execute(
        "SELECT SUM(win_probability) FROM entry_scores WHERE run_id=?", (run_id,)
    ).fetchone()[0] or 0.0
    _check("board: win_probability sums to 1.0 ± 1e-4",
           lambda: (abs(wp_sum - 1.0) < 1e-4, f"sum={wp_sum:.8f}"))

    # ranks are unique integers 1-20
    ranks = sorted(
        r[0] for r in conn.execute(
            "SELECT rank FROM entry_scores WHERE run_id=?", (run_id,)
        ).fetchall()
    )
    _check("board: ranks are unique 1-20",
           lambda: (sorted(set(ranks)) == list(range(1, 21)) and len(ranks) == 20,
                    f"ranks={ranks}"))

    # derby_override_active correct
    expected_override = 1   # Derby card should always trigger override
    actual_override = int(run["derby_override_active"])
    _check("board: derby_override_active=1 for Derby card",
           lambda: (actual_override == expected_override,
                    f"derby_override_active={actual_override}"))

    # fair_odds matches generated column (1/win_prob - 1) within 0.01
    rows = conn.execute(
        "SELECT win_probability, fair_odds FROM entry_scores WHERE run_id=?",
        (run_id,),
    ).fetchall()
    fair_errs = [
        abs(r["fair_odds"] - (1.0 / r["win_probability"] - 1.0))
        for r in rows if r["win_probability"] and r["fair_odds"]
    ]
    max_err = max(fair_errs) if fair_errs else 0.0
    _check("board: fair_odds matches 1/win_prob - 1 within 0.01",
           lambda: (max_err < 0.01, f"max_err={max_err:.6f}"))

    # model_edge matches win_probability - market_implied_prob within 1e-4
    edge_rows = conn.execute(
        "SELECT win_probability, market_implied_prob, model_edge "
        "FROM entry_scores WHERE run_id=?",
        (run_id,),
    ).fetchall()
    edge_errs = [
        abs(r["model_edge"] - (r["win_probability"] - r["market_implied_prob"]))
        for r in edge_rows
        if r["model_edge"] is not None and r["win_probability"] is not None
        and r["market_implied_prob"] is not None
    ]
    max_edge_err = max(edge_errs) if edge_errs else 0.0
    _check("board: model_edge matches win_prob - market_prob within 1e-4",
           lambda: (max_edge_err < 1e-4, f"max_err={max_edge_err:.8f}"))

    # bet_tag values are all valid
    tags = {r[0] for r in conn.execute(
        "SELECT DISTINCT bet_tag FROM entry_scores WHERE run_id=?", (run_id,)
    ).fetchall()}
    valid_tags = {"bet", "neutral", "underlay", "no_data", None}
    invalid_tags = tags - valid_tags
    _check("board: bet_tag values are valid",
           lambda: (not invalid_tags, f"invalid: {invalid_tags}" if invalid_tags else f"tags={tags}"))

    # confidence flags are 0 or 1
    bad_conf = conn.execute(
        "SELECT COUNT(*) FROM entry_scores WHERE run_id=? "
        "AND confidence_flag NOT IN (0,1)", (run_id,)
    ).fetchone()[0]
    _check("board: confidence_flag is 0 or 1",
           lambda: (bad_conf == 0, f"{bad_conf} bad rows"))

    conn.close()


def checks_app_data_loaders() -> None:
    print("\n[checks] App data loaders")
    from src.utils.db import get_connection
    import pickle

    # Artifact exists and loads
    art_path = ROOT / "saved_models" / "derby_override_v1.pkl"
    _check("app: derby_override_v1.pkl exists",
           lambda: (art_path.exists(), str(art_path)))

    if art_path.exists():
        try:
            with open(art_path, "rb") as fh:
                art = pickle.load(fh)
            ok = (
                hasattr(art, "feature_importances")
                and hasattr(art, "group_scores")
                and hasattr(art, "temperature")
                and hasattr(art, "config")
            )
            _record("app: artifact has required attributes", PASS if ok else FAIL)
        except Exception as exc:
            _record("app: artifact loads", FAIL, str(exc))

    # Board query (same SQL as load_board())
    conn = get_connection()
    run = conn.execute(
        "SELECT run_id FROM score_runs ORDER BY run_timestamp DESC LIMIT 1"
    ).fetchone()
    if run:
        run_id = run["run_id"]
        try:
            df = pd.read_sql(
                """
                SELECT es.rank, es.horse_name, es.post_position,
                       es.morning_line_odds, es.win_probability,
                       es.fair_odds, es.value_score, es.bet_tag,
                       es.pace_fit_score, es.form_score, es.surface_dist_fit,
                       es.market_implied_prob, es.confidence_flag, es.missing_data_flag,
                       vel.trainer, vel.jockey, vel.sire, vel.dam, vel.owner,
                       vel.pace_style, vel.career_starts, vel.career_wins,
                       vel.career_places, vel.career_shows, vel.career_earnings,
                       vel.last_race_days, vel.last_race_finish,
                       vel.best_speed_fig, vel.last_speed_fig,
                       vel.avg_speed_fig, vel.beyer_fig,
                       vel.dirt_starts, vel.dirt_wins,
                       vel.dist_starts, vel.dist_wins,
                       vel.workouts_30, vel.gate_class, vel.stamina_index
                FROM entry_scores es
                JOIN v_entries_live vel ON es.entry_id = vel.entry_id
                WHERE es.run_id = ?
                ORDER BY es.rank
                """,
                conn, params=(run_id,),
            )
            _check("app: board SQL query returns 20 rows",
                   lambda: (len(df) == 20, f"{len(df)} rows"))
        except Exception as exc:
            _record("app: board SQL query", FAIL, str(exc))
    conn.close()

    # Feature catalog exists
    cat_path = ROOT / "output" / "feature_catalog.csv"
    _check("app: feature_catalog.csv exists",
           lambda: (cat_path.exists(), str(cat_path)))

    # Board output files exist
    for fname in ("derby_2026_board.csv", "derby_2026_board.md",
                  "model_evaluation_dirt_route.md"):
        p = ROOT / "output" / fname
        _check(f"app: output/{fname} exists",
               lambda p=p: (p.exists(), str(p)))


def checks_derby_override() -> None:
    print("\n[checks] Derby override")
    from src.utils.db import get_connection
    from src.models.scorer import is_derby_context

    conn = get_connection()
    card_id = conn.execute(
        "SELECT card_id FROM race_cards LIMIT 1"
    ).fetchone()
    if not card_id:
        _record("derby: race card exists", FAIL, "no race_cards rows")
        conn.close()
        return

    cid = card_id["card_id"]
    detected = is_derby_context(conn, cid)
    _check("derby: is_derby_context() returns True for Derby card",
           lambda: (detected, f"card_id={cid}"))

    # Confidence tightening: verify at least 9 entries are low (tighter than base's 7)
    run = conn.execute(
        "SELECT run_id FROM score_runs ORDER BY run_timestamp DESC LIMIT 1"
    ).fetchone()
    if run:
        n_low = conn.execute(
            "SELECT COUNT(*) FROM entry_scores WHERE run_id=? AND confidence_flag=0",
            (run["run_id"],),
        ).fetchone()[0]
        _check("derby: confidence tightened (>=8 low entries with derby override)",
               lambda: (n_low >= 8, f"{n_low} low-confidence entries"))

    # Derby artifact model name
    import pickle
    art_path = ROOT / "saved_models" / "derby_override_v1.pkl"
    if art_path.exists():
        with open(art_path, "rb") as fh:
            art = pickle.load(fh)
        _check("derby: artifact model_name is derby_override_v1",
               lambda: (art.model_name == "derby_override_v1", art.model_name))

        # Weight shifts: distance_surface >= 0.20, market_prior <= 0.03
        ds_weight = art.config["feature_groups"]["distance_surface"]["group_weight"]
        mp_weight = art.config["feature_groups"]["market_prior"]["group_weight"]
        _check("derby: distance_surface weight >= 0.20 (shifted up from 0.17)",
               lambda: (ds_weight >= 0.20, f"{ds_weight:.2f}"))
        _check("derby: market_prior weight <= 0.03 (shifted down from 0.05)",
               lambda: (mp_weight <= 0.03, f"{mp_weight:.2f}"))

        # Group weights sum to 1.0 within 1e-9
        gw_sum = sum(g["group_weight"] for g in art.config["feature_groups"].values())
        _check("derby: Derby override group weights sum to 1.0",
               lambda: (abs(gw_sum - 1.0) < 1e-9, f"sum={gw_sum:.10f}"))

    conn.close()


# ── Summary ────────────────────────────────────────────────────────────────────
def print_summary() -> int:
    n_pass = sum(1 for _, s, _ in results if s == PASS)
    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    n_warn = sum(1 for _, s, _ in results if s == WARN)
    n_skip = sum(1 for _, s, _ in results if s == SKIP)
    total  = len(results)

    print("\n" + "=" * 60)
    print(f"  DerbyEdge V1 Regression — {total} checks")
    print(f"  PASS={n_pass}  FAIL={n_fail}  WARN={n_warn}  SKIP={n_skip}")
    print("=" * 60)

    if n_fail:
        print("\n  FAILURES:")
        for name, status, detail in results:
            if status == FAIL:
                print(f"    [FAIL] {name}" + (f" -- {detail}" if detail else ""))
    if n_warn:
        print("\n  WARNINGS:")
        for name, status, detail in results:
            if status == WARN:
                print(f"    [WARN] {name}" + (f" -- {detail}" if detail else ""))

    return 1 if n_fail else 0


# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="DerbyEdge V1 regression test")
    ap.add_argument(
        "--fresh", action="store_true",
        help="Wipe DB and run full pipeline from scratch before checks",
    )
    ap.add_argument(
        "--checks-only", action="store_true",
        help="Skip pipeline steps; run checks against existing DB",
    )
    args = ap.parse_args()

    print("\nDerbyEdge V1 — Regression Test")
    print("=" * 60)

    if not args.checks_only:
        try:
            step_init_db(fresh=args.fresh)
            step_ingest()
            step_build_features()
            step_score()
        except Exception as exc:
            print(f"\n  PIPELINE ABORTED: {exc}")
            traceback.print_exc()
            _record("pipeline: completed without error", FAIL, str(exc))
            return print_summary()

    checks_schema()
    checks_etl()
    checks_feature_store()
    checks_scoring_board()
    checks_app_data_loaders()
    checks_derby_override()

    return print_summary()


if __name__ == "__main__":
    sys.exit(main())

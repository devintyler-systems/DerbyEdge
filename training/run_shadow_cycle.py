"""
training/run_shadow_cycle.py

Single entry point for the full ML shadow evaluation cycle.

Steps (in order, each can be skipped):
  1. Schema check (always runs unless --report-only)
  2. horse_norm migration check + optional auto-migrate
  3. Shadow scoring  (DERBYEDGE_ML_MODE=shadow)
  4. Backfill outcome join  (backfill_shadow_eval)
  5. Promotion evaluation   (evaluate_shadow_vs_baseline)
  6. Generate report        (generate_promotion_report)
  7. Terminal summary

Usage
-----
    python -m training.run_shadow_cycle
    python -m training.run_shadow_cycle --skip-score
    python -m training.run_shadow_cycle --auto-migrate --skip-score
    python -m training.run_shadow_cycle --report-only
    python -m training.run_shadow_cycle --card-id 7

Flags
-----
  --skip-score        Skip the scoring step (use existing shadow_log.csv)
  --skip-migration    Skip the migration check entirely
  --auto-migrate      Run migrate_horse_norm automatically if needed (no prompt)
  --report-only       Only re-generate the markdown report from the last eval run
  --card-id INT       Card ID to pass to score.py (default: Derby card)

Safety
------
  This command always runs in shadow mode.  DERBYEDGE_ML_MODE is set to
  "shadow" before scoring.  If it is already set to "live" in the environment
  the script will abort — use --skip-score to evaluate without re-scoring.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Force UTF-8 output so box chars and dashes render correctly on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_REPO   = Path(__file__).resolve().parents[1]
_OUTPUT = _REPO / "output"

sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _header(title: str) -> None:
    print(f"\n{'-' * 60}")
    print(f"  {title}")
    print(f"{'-' * 60}")


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _warn(msg: str) -> None:
    print(f"  WARN  {msg}")


def _err(msg: str) -> None:
    print(f"  ERROR  {msg}", file=sys.stderr)


def _run_python(module_or_script: str, *extra_args: str, env: dict | None = None) -> int:
    """Run a Python module or script as a subprocess.  Returns exit code."""
    cmd = [sys.executable]
    if module_or_script.startswith("scripts/") or module_or_script.startswith("scripts\\"):
        cmd += [module_or_script]
    else:
        cmd += ["-m", module_or_script]
    cmd += list(extra_args)

    sys.stdout.flush()
    sys.stderr.flush()
    run_env = {**os.environ, **(env or {})}
    result  = subprocess.run(cmd, cwd=str(_REPO), env=run_env)
    return result.returncode


# ---------------------------------------------------------------------------
# Step 1 — Schema check
# ---------------------------------------------------------------------------

def _check_schema(auto_migrate: bool, skip_migration: bool) -> bool:
    """Return True if schema is ready.  May run migration if auto_migrate."""
    from src.utils.db import get_connection
    from training.schema_check import (
        SchemaError,
        check_race_cards,
        check_starter_observations,
    )

    conn = get_connection()
    try:
        check_race_cards(conn)
        _ok("race_cards schema OK")
    except SchemaError as exc:
        _err(str(exc))
        conn.close()
        return False

    # Check for horse_norm — this is the migration column.
    # If the table doesn't exist yet (no observations loaded), migration is a no-op;
    # observations.py will write horse_norm automatically on first insert.
    cur  = conn.execute("PRAGMA table_info(starter_observations)")
    cols = {row[1] for row in cur.fetchall()}
    conn.close()

    table_exists    = bool(cols)
    needs_migration = table_exists and "horse_norm" not in cols

    if needs_migration and not skip_migration:
        _warn("starter_observations is missing horse_norm — migration needed")
        if auto_migrate:
            print("  Running migrate_horse_norm automatically (--auto-migrate)...")
            rc = _run_python("training.migrate_horse_norm")
            if rc != 0:
                _err("Migration failed (exit code %d)" % rc)
                return False
            _ok("Migration complete")
        else:
            print()
            print("  ACTION REQUIRED: run the migration before evaluating:")
            print("    python -m training.migrate_horse_norm")
            print()
            print("  Or pass --auto-migrate to run it automatically,")
            print("  or --skip-migration to skip the check (not recommended).")
            return False

    elif needs_migration and skip_migration:
        _warn("horse_norm migration not applied — join key will use name-only fallback")

    elif not table_exists:
        _ok("starter_observations not yet populated — horse_norm will be written on first insert")
    elif not needs_migration:
        _ok("horse_norm column present")

    return True


# ---------------------------------------------------------------------------
# Step 2 — Shadow scoring
# ---------------------------------------------------------------------------

def _run_scoring(card_id: int | None) -> bool:
    """Score in shadow mode.  Refuses if env is already live."""
    current_mode = os.environ.get("DERBYEDGE_ML_MODE", "off").lower()
    if current_mode == "live":
        _err(
            "DERBYEDGE_ML_MODE=live is set in the environment.\n"
            "  This command only runs in shadow mode.\n"
            "  Either unset the variable or pass --skip-score."
        )
        return False

    env_override = {"DERBYEDGE_ML_MODE": "shadow"}
    extra: list[str] = []
    if card_id is not None:
        extra += ["--card-id", str(card_id)]

    rc = _run_python("scripts/score.py", *extra, env=env_override)
    if rc != 0:
        _err(f"Scoring failed (exit code {rc})")
        return False
    _ok("Shadow scoring complete — shadow_log.csv updated")
    return True


# ---------------------------------------------------------------------------
# Step 3 — Backfill outcome join
# ---------------------------------------------------------------------------

def _run_backfill() -> bool:
    rc = _run_python("training.backfill_shadow_eval")
    if rc != 0:
        _err(f"Backfill failed (exit code {rc})")
        return False
    _ok("Backfill complete — shadow_eval.csv written")
    return True


# ---------------------------------------------------------------------------
# Step 4 — Promotion evaluation
# ---------------------------------------------------------------------------

def _run_evaluation() -> bool:
    rc = _run_python("training.promote_check")
    if rc != 0:
        _err(f"Promotion evaluation failed (exit code {rc})")
        return False
    _ok("Evaluation complete — eval_run_* artifacts written")
    return True


# ---------------------------------------------------------------------------
# Step 5 — Generate report
# ---------------------------------------------------------------------------

def _run_report() -> bool:
    rc = _run_python("training.generate_promotion_report")
    if rc != 0:
        _err(f"Report generation failed (exit code {rc})")
        return False
    _ok("Report written — output/ml_promotion_report.md")
    return True


# ---------------------------------------------------------------------------
# Terminal summary
# ---------------------------------------------------------------------------

def _latest_eval_dir() -> Path | None:
    dirs = sorted(_OUTPUT.glob("eval_run_*"), key=lambda p: p.name, reverse=True)
    return dirs[0] if dirs else None


def _print_summary() -> None:
    W = 60
    print()
    print("+" + "=" * (W - 2) + "+")
    print("|  Shadow Cycle Summary" + " " * (W - 24) + "|")
    print("+" + "=" * (W - 2) + "+")

    def _row(label: str, value: str) -> None:
        label_w = 26
        val_w   = 28
        label   = label[:label_w].ljust(label_w)
        value   = str(value)[:val_w].ljust(val_w)
        print(f"|  {label}  {value}  |")

    # Join diagnostics
    jdiag_path = _OUTPUT / "join_diagnostics.json"
    if jdiag_path.exists():
        try:
            jd = json.loads(jdiag_path.read_text(encoding="utf-8"))
            _row("Shadow rows", str(jd.get("total_shadow_rows", "—")))
            mr = jd.get("match_rate", None)
            mr_str = f"{mr:.1%}" if isinstance(mr, float) else "—"
            _row("Join match rate", mr_str)
        except Exception:
            _row("Join diagnostics", "read error")
    else:
        _row("Join diagnostics", "not found")

    # Evaluation metrics
    eval_dir = _latest_eval_dir()
    if eval_dir:
        summary_path  = eval_dir / "metrics_summary.json"
        decision_path = eval_dir / "promotion_decision.json"

        if summary_path.exists():
            try:
                s = json.loads(summary_path.read_text(encoding="utf-8"))
                _row("Races with outcomes", str(s.get("n_races_total", "—")))
                ml = s.get("ml") or {}
                _row("ML races evaluated",  str(ml.get("n_races", "—")))
            except Exception:
                pass

        if decision_path.exists():
            try:
                d = json.loads(decision_path.read_text(encoding="utf-8"))
                decision = d.get("decision", "UNKNOWN")
                _row("Overall decision", decision)
                rec = d.get("recommended_action", "")
                # Wrap recommended action to fit
                if len(rec) > 30:
                    rec = rec[:28] + ".."
                _row("Recommended action", rec)
                insuf = d.get("insufficient_segments", [])
                if insuf:
                    segs = ", ".join(s["segment"] for s in insuf)
                    _row("Insufficient segments", segs)
            except Exception:
                _row("Promotion decision", "read error")
    else:
        _row("Evaluation", "no eval_run_* found")

    # Report path
    report = _OUTPUT / "ml_promotion_report.md"
    if report.exists():
        _row("Report", "output/ml_promotion_report.md")

    print("+" + "=" * (W - 2) + "+")

    # Artifact paths
    eval_dir_for_paths = _latest_eval_dir()
    artifacts = [
        ("join_diagnostics.json",     _OUTPUT / "join_diagnostics.json"),
        ("unmatched_shadow_rows.csv", _OUTPUT / "unmatched_shadow_rows.csv"),
        ("metrics_summary.json",      eval_dir_for_paths / "metrics_summary.json" if eval_dir_for_paths else None),
        ("segment_metrics.csv",       eval_dir_for_paths / "segment_metrics.csv"  if eval_dir_for_paths else None),
        ("ml_promotion_report.md",    _OUTPUT / "ml_promotion_report.md"),
    ]
    print()
    print("  Artifacts:")
    label_w = 28
    for name, path in artifacts:
        if path is None:
            status = "MISSING  (no eval run found)"
        elif path.exists():
            status = str(path.relative_to(_REPO))
        else:
            status = "MISSING"
        print(f"    {name:<{label_w}}  {status}")

    # Final action hint outside the box
    eval_dir = _latest_eval_dir()
    if eval_dir:
        decision_path = eval_dir / "promotion_decision.json"
        if decision_path.exists():
            try:
                d = json.loads(decision_path.read_text(encoding="utf-8"))
                decision = d.get("decision", "")
                print()
                if decision == "PASS":
                    print("  PASS — promote to live:")
                    print("    $env:DERBYEDGE_ML_MODE='live'; python scripts/score.py")
                elif decision in ("HOLD", "INSUFFICIENT_DATA"):
                    print("  Score more races in shadow mode:")
                    print("    $env:DERBYEDGE_ML_MODE='shadow'; python scripts/score.py")
                elif decision == "FAIL":
                    print("  FAIL — investigate degraded segments, retrain, then re-run:")
                    print("    python -m training.run_shadow_cycle --skip-score")
            except Exception:
                pass
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the full ML shadow evaluation cycle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--skip-score",      action="store_true",
                    help="Skip shadow scoring (use existing shadow_log.csv)")
    ap.add_argument("--skip-migration",  action="store_true",
                    help="Skip the horse_norm migration check")
    ap.add_argument("--auto-migrate",    action="store_true",
                    help="Run migrate_horse_norm automatically if needed")
    ap.add_argument("--report-only",     action="store_true",
                    help="Only regenerate the markdown report from last eval run")
    ap.add_argument("--card-id",         type=int, default=None,
                    help="Card ID to pass to score.py")
    args = ap.parse_args()

    print()
    print("DerbyEdge - Shadow Evaluation Cycle")
    print("=" * 44)

    # ── report-only fast path ─────────────────────────────────────────────
    if args.report_only:
        _header("Generating report")
        if not _run_report():
            return 1
        _print_summary()
        return 0

    # ── Step 1: Schema check ───────────────────────────────────────────────
    _header("Schema check")
    if not _check_schema(
        auto_migrate=args.auto_migrate,
        skip_migration=args.skip_migration,
    ):
        return 1

    # ── Step 2: Shadow scoring ─────────────────────────────────────────────
    if not args.skip_score:
        _header("Shadow scoring  (DERBYEDGE_ML_MODE=shadow)")
        if not _run_scoring(args.card_id):
            return 1
    else:
        _header("Shadow scoring  [SKIPPED]")
        shadow_log = _OUTPUT / "shadow_log.csv"
        if shadow_log.exists():
            _ok(f"Using existing shadow_log.csv ({shadow_log.stat().st_size // 1024}KB)")
        else:
            _warn("shadow_log.csv not found — backfill will produce empty results")

    # ── Step 3: Backfill outcome join ─────────────────────────────────────
    _header("Backfill outcome join")
    if not _run_backfill():
        return 1

    # ── Step 4: Promotion evaluation ──────────────────────────────────────
    _header("Promotion evaluation")
    if not _run_evaluation():
        return 1

    # ── Step 5: Generate report ───────────────────────────────────────────
    _header("Generate report")
    if not _run_report():
        return 1

    # ── Summary ───────────────────────────────────────────────────────────
    _print_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())

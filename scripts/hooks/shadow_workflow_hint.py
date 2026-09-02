"""
scripts/hooks/shadow_workflow_hint.py

Claude Code Stop hook — checks shadow pipeline state and prints the next
suggested command if the workflow is in an actionable state.

Reads stdin JSON provided by Claude Code (not required; falls back gracefully).
Prints nothing if the pipeline is fully up to date or no shadow data exists.

Hook config (in .claude/settings.json):
    "Stop": [{"hooks": [{"type": "command",
                         "command": "python scripts/hooks/shadow_workflow_hint.py"}]}]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO   = Path(__file__).resolve().parents[2]
_OUTPUT = _REPO / "output"

_SHADOW_LOG  = _OUTPUT / "shadow_log.csv"
_SHADOW_EVAL = _OUTPUT / "shadow_eval.csv"


def _latest_eval_dir() -> Path | None:
    dirs = sorted(_OUTPUT.glob("eval_run_*"), key=lambda p: p.name, reverse=True)
    return dirs[0] if dirs else None


def _mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else 0.0


def main() -> None:
    # Read stdin (Claude Code passes hook context); ignore errors
    try:
        hook_data = json.load(sys.stdin)
    except Exception:
        hook_data = {}

    # Never fire if we're inside a tool call (only want end-of-turn suggestions)
    if hook_data.get("hook_event_name") not in (None, "Stop", ""):
        return

    # No shadow log → workflow hasn't started; stay silent
    if not _SHADOW_LOG.exists():
        return

    shadow_log_mtime  = _mtime(_SHADOW_LOG)
    shadow_eval_mtime = _mtime(_SHADOW_EVAL)

    # shadow_log exists but shadow_eval missing or stale
    if shadow_eval_mtime < shadow_log_mtime:
        _hint(
            "shadow_log.csv has new rows that haven't been backfilled yet.",
            "python -m training.backfill_shadow_eval",
            "or run the full cycle:  python -m training.run_shadow_cycle --skip-score",
        )
        return

    # shadow_eval exists — check if evaluation is up to date
    eval_dir = _latest_eval_dir()
    if eval_dir is None:
        _hint(
            "shadow_eval.csv is ready but no promotion evaluation found.",
            "python -m training.promote_check",
        )
        return

    eval_dir_mtime = eval_dir.stat().st_mtime
    if shadow_eval_mtime > eval_dir_mtime:
        _hint(
            "shadow_eval.csv is newer than the last promotion check.",
            "python -m training.promote_check",
        )
        return

    # Evaluation is current — show decision reminder only for actionable states
    decision_path = eval_dir / "promotion_decision.json"
    if not decision_path.exists():
        return

    try:
        d        = json.loads(decision_path.read_text(encoding="utf-8"))
        decision = d.get("decision", "")
        action   = d.get("recommended_action", "")
    except Exception:
        return

    if decision == "PASS":
        _hint(
            f"Last promotion decision: PASS",
            "$env:DERBYEDGE_ML_MODE='live'; python scripts/score.py",
            action,
        )
    elif decision in ("HOLD", "INSUFFICIENT_DATA"):
        n_races = None
        summary_path = eval_dir / "metrics_summary.json"
        if summary_path.exists():
            try:
                s       = json.loads(summary_path.read_text(encoding="utf-8"))
                n_races = (s.get("ml") or {}).get("n_races")
            except Exception:
                pass
        from_30 = f"  ({n_races}/30 races)" if n_races is not None else ""
        _hint(
            f"Last promotion decision: {decision}{from_30}",
            "$env:DERBYEDGE_ML_MODE='shadow'; python scripts/score.py",
            "then:  python -m training.run_shadow_cycle --skip-score",
        )


def _hint(situation: str, *commands: str) -> None:
    print()
    print(f"[DerbyEdge] {situation}")
    for cmd in commands:
        print(f"  >>> {cmd}")


if __name__ == "__main__":
    main()

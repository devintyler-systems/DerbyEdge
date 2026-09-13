"""Read-only, fail-closed eligibility audit; it never scores or rebuilds."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.services.score_eligibility import (evaluate_score_eligibility, resolve_race_context, resolve_scoring_context, write_score_eligibility_artifacts)  # noqa: E402
from src.models.trainer import load_model_artifact  # noqa: E402
from src.utils.db import DB_PATH  # noqa: E402

def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only fail-closed score eligibility audit")
    parser.add_argument("--card-id", type=int, required=True); parser.add_argument("--entry-id", type=int, required=True)
    parser.add_argument("--db-path", type=Path, default=DB_PATH); parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "acceptance")
    args = parser.parse_args()
    try:
        race = resolve_race_context(args.db_path, args.card_id, args.entry_id)
        feature, decision, model = resolve_scoring_context(args.db_path, args.card_id, args.entry_id)
        artifact_path = Path(str(model.get("artifact_path") or ""))
        if not artifact_path.exists(): raise LookupError(f"Artifact unavailable: {artifact_path}")
        result = evaluate_score_eligibility(race_context=race, feature_row=feature, decision=decision, model=model, artifact=load_model_artifact(artifact_path))
    except Exception as exc:
        print(f"FAIL card_id={args.card_id} entry_id={args.entry_id}: {exc}"); return 1
    active, summary, failures = write_score_eligibility_artifacts(result, args.output_dir)
    print(f"card_id={args.card_id} entry_id={args.entry_id} race_family={race['race_family_classification']}")
    print(f"active_features_csv={active}\nsummary_json={summary}")
    if failures: print(f"failures_csv={failures}")
    print(("PASS" if result["score_valid"] else "FAIL") + f" active_features={len(result['active_rows'])} invalid_active_features={len(result['invalid_rows'])}")
    for row in result["invalid_rows"]: print(f"{row['feature_name']} weight={row['effective_weight']} value={row['feature_value']} status={row['lineage_status']} source={row['lineage_source']} evidence={row['evidence_count']} reason={row['failure_reason']}")
    return 0 if result["score_valid"] else 1
if __name__ == "__main__": sys.exit(main())

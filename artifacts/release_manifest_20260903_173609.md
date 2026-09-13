# Release manifest — DK ingestion-run contract + enrichment + model-policy
Generated 2026-09-03T17:36:09-07:00   HEAD 0da3552 (uncommitted)

## Changed files by group

### A. Parser / gating baseline — FROZEN, byte-identical to WIP (NOT modified)
 src/ingest/draftkings_pdf.py     | 623 +++++++++++++++++++++++++++++++++++----
 src/ingest/run_state.py          |  87 ++++--
 tests/test_data_quality_gate.py  |   8 +-
 tests/test_effective_run_mode.py |  16 +-
 tests/test_feature_coverage.py   |   8 +-
 5 files changed, 644 insertions(+), 98 deletions(-)
(also 4 prior-session DK test files, untouched: test_card_run_state_audit, test_dk_identity_gating, test_dk_upload_run_integration, test_dkhorse_program_sections)

### B. Contract / enrichment (this work)
new:
  311 src/ingest/ingestion_run.py
  182 src/services/ingest_upload.py
  363 src/services/dk_enrichment.py
   57 src/utils/ingest_trace.py
  348 tests/test_ingestion_run_contract.py
  270 tests/test_dk_enrichment_contract.py
extended existing:
  db/schema.sql                    +1 -0
  scripts/build_features.py        +25 -2
  src/app/app.py                   +96 -5
  src/app/board_formatting.py      +57 -0
  src/features/builder.py          +21 -9
  src/services/pdf_ingest.py       +83 -0
  src/services/run_mode.py         +193 -4

### C. Model-policy hardening (this work)
  202 src/services/dk_model_policy.py
  191 tests/test_dk_model_policy.py
  (also part of the src/services/run_mode.py, src/app/*, src/app/board_formatting.py deltas above:
   CardRunState.scoring_state, _apply_dk_model_policy, FEATURE_LIMITED_NO_SCORING guidance + banners)

## Migration behavior for existing race_cards
- Additive column race_cards.ingestion_run_id TEXT (schema.sql + ensure_ingestion_run_column auto-migrate, same pattern as feature_store).
- Existing cards have ingestion_run_id = NULL -> get_card_run_state keeps the legacy load_latest_card_audit path unchanged (no regression).
- A card only switches to the immutable-run path after a fresh upload binds it (bind_card_to_ingestion_run).
- New nullable columns on horse_starts / workouts / feature_store (ingestion_run_id + DK availability flags) via _ensure_provenance_columns; NULL for pre-existing rows.
- New table dk_card_enrichment created on demand.

## Files intentionally excluded from commit (all .gitignore-matched)
- db/derbyedge.db (holds the 3 live browser cards) — .gitignore *.db / db/derbyedge.db
- artifacts/** (baseline capture, pytest logs + exit codes, phase2_live_verification.json, this manifest) — .gitignore artifacts/ + *.log ; retained locally as evidence, not version-controlled
- data/runs/** (immutable ingestion-run dirs + parsed-race snapshots) — .gitignore data/runs/
- .venv/** (Playwright installed here for the live browser drive) — .gitignore .venv/

## To be committed
  new: docs/superpowers/
  new: src/ingest/ingestion_run.py
  new: src/services/dk_enrichment.py
  new: src/services/dk_model_policy.py
  new: src/services/ingest_upload.py
  new: src/utils/ingest_trace.py
  new: tests/test_card_run_state_audit.py
  new: tests/test_dk_enrichment_contract.py
  new: tests/test_dk_identity_gating.py
  new: tests/test_dk_model_policy.py
  new: tests/test_dk_upload_run_integration.py
  new: tests/test_dkhorse_program_sections.py
  new: tests/test_ingestion_run_contract.py
  mod: db/schema.sql
  mod: scripts/build_features.py
  mod: src/app/app.py
  mod: src/app/board_formatting.py
  mod: src/features/builder.py
  mod: src/services/pdf_ingest.py
  mod: src/services/run_mode.py
  mod(frozen WIP, pre-existing): src/ingest/draftkings_pdf.py, src/ingest/run_state.py, tests/test_{data_quality_gate,effective_run_mode,feature_coverage}.py

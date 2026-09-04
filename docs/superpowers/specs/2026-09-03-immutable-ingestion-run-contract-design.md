# Immutable ingestion-run contract — design

Date: 2026-09-03
Branch: feature/methodology-evidence-governance
Status: approved (build-on-WIP; parser frozen)

## Problem

Every uploaded DraftKings race (SAR R10, IND R5, DMR R4) renders 0% starter match,
0 linked PP starts, and generic "Re-upload the original 1/ST PDF" guidance — even
though the parser produces fully valid, model-ready output at parse time
(verified: all four DK fixtures parse to `source_format=dkhorse_program_pdf`,
`active_entry_count` 8–12, `total_pp_records_linked` 46–106, `identity_resolution_rate=1.0`,
zero block reasons — see `artifacts/contract_baseline_20260903_160300/`).

### Root cause (integration, not parser)

- `src/app/app.py:2703` — the DK branch does `_pr5 = _parsed_pr5` and stashes the
  raw parsed dict in `st.session_state["_pdf5_parse_cache"]`. It never calls
  `persist_dk_upload_run`, never produces an `ingest_run_id`, so the
  `bind_run_to_card` call at `app.py:2938` is skipped for DK.
- `src/ingest/firstbet_pdf.py::load_latest_card_audit` — globs **every**
  `data/runs/*/card_binding.json`, filters by `card_id`, returns the **newest**
  by `run_id`. "Latest by card" lookup, exactly what the spec forbids.
- `src/services/run_mode.py::get_card_run_state` → `load_latest_card_audit` →
  for a DK card nothing is bound → `audit=None` →
  `data_quality_from_card` DB branch → no `horse_starts` rows → 0/0 → BLOCKED
  with generic guidance.

## Architecture

### `src/ingest/ingestion_run.py` (new)

```python
@dataclass(frozen=True)
class IngestionRun:
    ingestion_run_id: str
    upload_sha256: str
    parser_pipeline_version: str
    source_format: str | None
    parser_selected: str | None
    parse_status: Literal["parsed", "blocked", "failed"]
    race_key: str | None
    created_at_utc: str
    feature_audit: dict
    normalized_race_payload: dict
    error: dict | None
```

- `build_ingestion_run(pdf_bytes, filename, parse_result)` — pure; derives every
  field from the already-computed `parse_race_pdf` result. No parser calls.
  - `source_format`  ← `parse_result["parser_diagnostics"]["source_format"]`
  - `parser_selected` ← `parse_result["parser"]["adapter_selected"]`
  - `parse_status`   ← `failed` if `not ok`; `blocked` if run-mode BLOCKED; else `parsed`
  - `race_key`       ← `f"{track_code}|{race_date}|R{race_number}"` when all present
  - `feature_audit`  ← enriched diagnostics (reuse `persist_dk_upload_run`'s audit builder)
  - `normalized_race_payload` ← `parse_result["race"]`
- `persist_ingestion_run(run, runs_root)` — `mkdir(exist_ok=False)` under
  `data/runs/<ingestion_run_id>/`, writes `ingestion_run.json` (whole record),
  plus `parsed_pp.json` / `feature_audit.json` for back-compat. Returns paths +
  `audit_sha256` / `payload_sha256` (sha256 of the canonical JSON of each).
- `load_ingestion_run(run_id, runs_root)` — read `ingestion_run.json` **by id only**.
- `IngestionRunBindingInvalid(reason="ingestion_run_binding_invalid", detail=...)`.
- `validate_ingestion_run(run, *, upload_sha256=None, parser_pipeline_version=None)`
  — raises `IngestionRunBindingInvalid` when the audit or payload is absent /
  malformed, or `upload_sha256` / `parser_pipeline_version` disagree.

### Schema / binding

- Auto-migrate (same pattern as `feature_store`): add
  `race_cards.ingestion_run_id TEXT` if missing. Helper
  `ensure_ingestion_run_column(conn)`.
- `bind_card_to_ingestion_run(conn, card_id, run_id)` — `UPDATE race_cards SET
  ingestion_run_id=? WHERE card_id=?`. Exact pointer; last write wins **by
  explicit re-bind only** (never "newest run dir").
- `load_card_ingestion_run(conn, card_id, runs_root)` — read the column, then
  `load_ingestion_run`.

### `src/services/ingest_upload.py` (new) — the only upload flow

```
ingest_uploaded_race_pdf(pdf_bytes, filename, conn, *, runs_root) -> dict
  bytes = pdf_bytes                       # caller passes ONE getvalue()
  upload_sha256 = sha256(bytes)
  pipeline_version = PARSER_PIPELINE_VERSION
  parse_result = parse_race_pdf(bytes, filename=filename)
  run = build_ingestion_run(bytes, filename, parse_result)
  persist_ingestion_run(run)              # atomic; before any card write
  card_id, ... = create_or_resync_card(conn, parse_result)   # existing find_or_create_race
  bind_card_to_ingestion_run(conn, card_id, run.ingestion_run_id)
  return {"ingestion_run_id": ..., "card_id": ..., + trace fields}
```

Native 1/ST migrates onto the same contract: `ingest_firstbet_pdf` result is
wrapped in an `IngestionRun` (`source_format="firstbet_racedetail_pdf"`), persisted,
and bound the same way. Its existing audit shape is preserved inside `feature_audit`.

### `src/services/run_mode.py::get_card_run_state`

1. `run = load_card_ingestion_run(conn, card_id, runs_root)`.
2. If the card has an `ingestion_run_id`:
   - `validate_ingestion_run(run)` — on `IngestionRunBindingInvalid` return
     `CardRunState(RunMode.BLOCKED, ["ingestion_run_binding_invalid: <detail>"],
     {"binding_invalid": True, "source_format": <if known>})`. **No fallback.**
   - Build `DataQuality` from `run.feature_audit`; scoring consumes
     `run.normalized_race_payload`.
3. If the card has **no** `ingestion_run_id` (legacy): unchanged
   `load_latest_card_audit` path (req 10 — no regression).

### `src/app/board_formatting.py::blocked_state_guidance`

- `audit.get("binding_invalid")` → the spec's exact "Upload state mismatch:"
  message.
- `source_format == "dkhorse_program_pdf"` → DK diagnostics only, even when blocked.
- Generic 1/ST guidance **only** when `source_format in (None, "unknown", "unsupported")`.

### `src/utils/ingest_trace.py` (new)

`trace_ingest(event, **fields)` — emits ONE `json.dumps` line to logger
`derbyedge.ingest.trace` at `upload | parse | persist | bind | render | score`
with: `event, ingestion_run_id, card_id, upload_sha256, parser_pipeline_version,
source_format, parser_selected, race_key, active_entry_count,
total_pp_records_found, total_pp_records_linked, identity_resolution_rate,
audit_sha256, payload_sha256`. Never logs PDF text.

### `src/app/app.py`

- DK + 1/ST branches call `ingest_uploaded_race_pdf`; only
  `ingestion_run_id` goes into `st.session_state` (`_pdf5_ingestion_run_id`).
- Card-create button binds by that id.
- Render path already flows through `get_card_run_state`; add `trace_ingest`
  at render + score.

## Tests — `tests/test_ingestion_run_contract.py`

1. **SAR valid DK run survives to render** — real `parse_race_pdf` of the SAR R10
   fixture as producer baseline; assert `source_format`, `active_entry_count==12`,
   `total_pp_records_linked>0` (==46), `identity_resolution_rate>=0.90`,
   `card.ingestion_run_id == persisted_run.ingestion_run_id`, and render-time
   values equal parse-time values. Fail if they differ — never edit the parser.
2. **Same race key, different uploads** — failed run then valid run; second
   card/render uses only the second `ingestion_run_id`.
3. **Same bytes, changed parser version** — `dkhorse_sections_v1` vs `_v2`;
   separate runs; newer never reuses old audit/payload.
4. **Card binding mismatch** — card bound to missing/different run → fail-closed
   `ingestion_run_binding_invalid`; no generic parser diagnostics.
5. **DK blocked messaging** — persisted DK run, `total_pp_records_linked=0` →
   DK-specific guidance, never native 1/ST guidance.
6. **Existing non-DK behavior** — native 1/ST fixture flow unchanged.

## Constraints

- `src/ingest/draftkings_pdf.py` and `src/ingest/run_state.py` rule changes are
  frozen. No regex / header / sectioning / threshold / reconciliation edits.
- No commit, no PR, no scoring-threshold changes.
- Everything stays uncommitted until the full contract suite + `pytest -q` pass.

---

## Phase 2 — DK enrichment wired to usable scoring (2026-09-03)

- `src/services/dk_enrichment.py` — `enrich_card_from_ingestion_run(conn, card_id)`:
  loads the parsed-race snapshot (`data/runs/<id>/dk_parsed_race.pkl`, written at
  ingest) **by the card's bound `ingestion_run_id` only**, runs the existing
  idempotent `ingest_draftkings_to_canonical` (dedupes `horse_starts` by
  `source_row_id`), stamps every canonical + `feature_store` row with
  `ingestion_run_id`, records per-runner feature availability (PP-derived columns
  stay NULL — never 0 — for `resolved_no_history` runners, with
  `has_completed_start_history` / `workout_forward_low_history` / `*_available`
  flags), then `build_features(card_id, conn=conn)`.
- State machine `dk_card_enrichment(card_id, ingestion_run_id, …, state)` —
  `NOT_STARTED → ENRICHING → ENRICHED → FAILED`, provenance-bound (upload sha,
  pipeline version, `dk_enrich_v1`).
- Enrichment failure → `get_card_run_state` returns `BLOCKED` +
  `audit["enrichment_failed"]` + the exact DK-specific message; never 0/0, never
  generic 1/ST, never stale DB history.
- `scripts/build_features.py` runs DK enrichment first for DK-bound cards.
- Fixed a latent `KeyError: field_size_last` crash in
  `builder._canonical_history_overlay` (empty `valid_surface` → 0-column frame).

## Phase 3 — model-family separation hardening (2026-09-03)

- `src/services/dk_model_policy.py` — `decide_dk_model_policy(...)`:
  - `feature_availability_mask` per family (`speed`, `trip` structurally absent
    from DK PP; `pace`/`form`/`surface_distance` from the verified frame).
  - If a DK card would be scoreable but speed+pace+form+trip are all
    unavailable: select a registered `limited_history_proxy` model if one exists
    (wagering still disabled, `confidence_tier = limited_data_proxy`), otherwise
    final state `FEATURE_LIMITED_NO_SCORING` — `scoring_eligibility = False`,
    no win probabilities / fair odds / rankings / betting.
  - DK `betting_eligibility` is always `False`.
- `CardRunState.scoring_state` refines `mode` (`FEATURE_LIMITED_NO_SCORING` maps
  to `mode = BLOCKED`, so `score_race` refuses it).
- Audit persists `model_family_selected`, `model_version`,
  `model_feature_schema_version`, `feature_availability_mask`,
  `calibration_version`, `confidence_tier`, `scoring_eligibility`,
  `betting_eligibility`, `disabled_capability_reasons`.
- App renders "🔒 FEATURE-LIMITED — NO SCORING" (or the limited-data-proxy label
  when a proxy exists); never plain "Model Ready".
- No `limited_history_proxy` artifact exists in `model_registry`
  (CHECK constraint would also need widening), so all real DK uploads currently
  resolve to `FEATURE_LIMITED_NO_SCORING`.

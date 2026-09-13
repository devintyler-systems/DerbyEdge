# Forward-capture contract dry-run

`scripts/run_forward_capture_dry_run.py` executes the complete forward-capture
workflow against **synthetic fixtures only** and checks that the contract behaves
as specified.

```
raw pre-race artifact + result reference
  → evidence ledger        (src/services/forward_capture_evidence.py)
  → forward-capture prefill (src/services/forward_capture_prefill.py)
  → operator-completed manifest
  → historical snapshot contract audit (src/services/historical_snapshot_contract_audit.py)
```

Outputs (deterministic, gitignored, under `output/acceptance/`):
`forward_capture_dry_run_report.json`,
`forward_capture_dry_run_completed_manifest.json`,
`forward_capture_dry_run_failure_matrix.json`, plus a synthetic fixture pair
under `output/acceptance/forward_capture_dry_run_fixtures/`.

## The synthetic fixture boundary

Every byte the harness touches is fabricated in-process:

- a DraftKings-style pre-race card `SAR_DK_Horse_R6_9-4-26.md` (candidate key
  `SAR|2026-09-04|R6`);
- an Equibase-style full-card result `eqb_SAR_2026-09-04_fullcard.pdf`;
- a hard-coded set of "operator evidence" values (tz-qualified `source_as_of`
  strictly before a tz-qualified scheduled post, tier/provenance, surface,
  distance, starter counts `6 == 6`, `COMPLETE` identity/field/vector,
  `raw_artifact_retained` / `pre_race_fields_only` true, an outcome reference
  with provider + parser version + `OPERATOR_ATTESTED` provenance).

No production or canonical source artifact is read. `data/`, `db/`, and every
source directory are untouched. The harness never imports DB utilities /
`sqlite3`, ingestion / persistence / parse-and-persist, feature build, readiness,
training, scoring, calibration, promotion, market, fair-odds, or wager code — and
a static import scan of the orchestrated services is part of the report.

## What a passing audit here proves

- The four stages are wired together and exchange the expected shapes.
- The contract audit enforces its semantic rules beyond JSON-schema validation:
  timezone presence on both timestamps; **strict** `source_as_of <
  scheduled_post`; explicit `source_as_of_tier`; `expected == observed` active
  starters and `>= 2`; `identity_reconciliation_status`,
  `field_completeness_status`, `feature_vector_status` all `COMPLETE`;
  `raw_artifact_retained` and `pre_race_fields_only` both `true`; outcome
  provenance `PROVEN` or `OPERATOR_ATTESTED`; local raw-artifact SHA-256 match.
- Every intermediate object stays non-eligible: the evidence bundle is
  `DRAFT_NOT_ELIGIBLE`, the prefill draft is `DRAFT_NOT_ELIGIBLE`, and the
  auto-generated prefill draft **fails** the audit (it still carries operator
  placeholders).
- The failure matrix: each of the following independently produces a rejection —
  no timezone on `source_as_of_timestamp`; `source_as_of` equal to / after post;
  missing tier/provenance; mismatched pre-race/result candidate key; mismatched
  result SHA-256; `expected != observed` starter count; non-`COMPLETE`
  identity/field/vector; `raw_artifact_retained` false; `pre_race_fields_only`
  false; incomplete outcome provenance/reference.

## What it does NOT prove

- That any real race has been captured leakage-free.
- That the DraftKings or Equibase parsers extract a complete, correct pre-race
  field or a correct result — the harness supplies those facts by fiat.
- That the retained bytes are genuinely immutable or independently sourced.
- Anything about feature generation, readiness, scoring, calibration, or wagering.

## Audit passing here is not canonical ingestion approval

A green dry-run is a **wiring and semantics** check on fabricated data. It grants
no eligibility. The report is labelled `DRY_RUN_ONLY_NOT_ELIGIBLE`; the completed
manifest file is synthetic and exists only to be re-audited.

## Promotion condition

A real historical pair may enter the training corpus only when a genuine future
capture passes this *same* workflow with:

- an independently retained, unmodified raw pre-race artifact and its SHA-256;
- a separately retained official result artifact and its SHA-256;
- operator-supplied `source_as_of` evidence (timezone-qualified, tier +
  provenance) recorded at capture time in the evidence ledger;
- a complete non-scratched starter field with `COMPLETE` identity / field /
  feature-vector reconciliation;
- a manifest that exits `0` from
  `scripts/audit_historical_snapshot_contract.py`.

Only then does a separate, human-reviewed ingestion step apply.

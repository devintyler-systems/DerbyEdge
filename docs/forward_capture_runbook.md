# Forward-capture runbook — first contract-passing historical race pair

Goal: collect **one** future race as a leakage-safe training pair —

1. a DraftKings pre-race race-card artifact captured **before** scheduled post; and
2. a matched Equibase official **result** artifact for the *same exact race*.

This runbook is operator scaffolding. Nothing here ingests, scores, calibrates,
or makes an artifact eligible. Eligibility happens only when a completed manifest
passes `scripts/audit_historical_snapshot_contract.py`.

Related:
[`docs/historical_pre_race_snapshot_contract.md`](historical_pre_race_snapshot_contract.md) ·
[`docs/historical_source_acquisition_sop.md`](historical_source_acquisition_sop.md) ·
[`schemas/historical_pre_race_snapshot_manifest.schema.json`](../schemas/historical_pre_race_snapshot_manifest.schema.json)

## 0. Choose the target race
Pick a single upcoming race and record its identity now:
`track code | race date (YYYY-MM-DD) | race number`. Everything below is bound to
that one `candidate_key`.

## 1. Capture the DK race card before post
- Pull the **complete entered field** for the target race from DraftKings
  **before** the scheduled post time. Not after; not a later PP snapshot that
  merely lists the race in history (that is `POST_RACE_TARGET_SNAPSHOT_LEAKAGE`).
- Save the file with the standard name so filename parsing works:
  `{TRACK}_DK_Horse_R{n}_{M-D-YY}.md` (or `.pdf`).
- **Do not** re-save, reformat, re-export, or "clean" it.

## 2. Preserve the raw bytes and hash them
- Store the untouched file under the capture directory (section 7).
- Record its SHA-256 (`certutil -hashfile <file> SHA256`, or the prefill helper).

## 3. Record the source as-of evidence (operator, mandatory)
The contract needs a **timezone-qualified** capture/as-of time. Record, in order
of preference:
- `PUBLISHER_TIMESTAMP` — a timestamp on the content itself;
- `SYSTEM_CAPTURED` — an HTTP response header / download-manager log entry;
- `OPERATOR_ATTESTED` — a dated note: exactly how, from where, and at what
  instant (with timezone) the capture happened.

Then set:
- `source_as_of_timestamp` = that instant, ISO-8601 **with offset or `Z`**;
- `source_as_of_tier` = `PROVEN` (only with publisher/system proof) or
  `OPERATOR_ATTESTED`;
- `source_as_of_provenance` accordingly.

A filename date or a filesystem `mtime` is **never** acceptable proof and can
never be `PROVEN`. Also confirm the strict ordering
`source_as_of_timestamp < target_scheduled_post_timestamp`.

## 4. After the race is official, capture the Equibase result
- Wait for official status. Download the Equibase chart / full-card result that
  covers the target race (`eqb_{TRACK}_{YYYY-MM-DD}_fullcard.pdf`).
- Preserve raw bytes unmodified; record its own SHA-256, provider, and
  parser/extraction version.
- Optionally run `python scripts/slice_equibase_results.py --root <dir>` to get a
  race-level row (`candidate_key`, winner/official detection) for the target
  race. The slice's official/winner detection is a **heuristic**, not provenance.
- Set `outcome_reference` (path, sha256, provider, parser_version) and
  `outcome_provenance_status` = `PROVEN` or `OPERATOR_ATTESTED`.

## 5. Reconcile the complete field (operator + parse)
The contract rejects partial fields and partial vectors. Establish and record:
- `expected_active_starter_count` = `observed_active_starter_count` ≥ 2
  (complete non-scratched field);
- `identity_reconciliation_status` = `COMPLETE` (every active starter maps to a
  stable identity);
- `field_completeness_status` = `feature_vector_status` = `COMPLETE`;
- `target_surface`, `target_distance_furlongs` confirmed;
- `raw_artifact_retained` = `pre_race_fields_only` = `true`.

## 6. Draft the manifest, then audit
- Prefill the deterministic identity fields:
  ```
  python scripts/prefill_forward_capture_manifest.py \
      --dk-card <dk card path> \
      --result-row-json <result row json>
  ```
  Outputs under `output/acceptance/`:
  `forward_capture_manifest_prefill_<key>.json` and `..._gaps.json`, both
  labelled `DRAFT_NOT_ELIGIBLE`.
- Start the worksheet from
  [`templates/forward_capture_manifest_draft.template.json`](../templates/forward_capture_manifest_draft.template.json)
  and replace every `__OPERATOR_REQUIRED__` value using sections 3–5.
- Transcribe the completed worksheet into
  [`templates/historical_pre_race_snapshot_manifest.template.json`](../templates/historical_pre_race_snapshot_manifest.template.json)
  form and run:
  ```
  python scripts/audit_historical_snapshot_contract.py --manifest <manifest.json>
  ```
- **Ingest only if the audit exits 0.** A non-passing manifest is never ingested,
  and no manifest is auto-generated from incomplete evidence.

## 7. Suggested capture directory layout
Local, gitignored, one directory per pair keyed by `candidate_key`:

```
data/raw/forward_capture/<TRACK>/<YYYY-MM-DD>/R<n>/
  pre_race/    {TRACK}_DK_Horse_R{n}_{M-D-YY}.md        # unmodified DK card
  result/      eqb_{TRACK}_{YYYY-MM-DD}_fullcard.pdf     # unmodified Equibase result
  manifest/    manifest.json                            # completed, audited
  notes/       as_of_attestation.md                     # operator evidence, timezone-qualified
```

`data/raw/` is already gitignored; keep raw bytes immutable once written.

## No-go boundaries
- No step ingests into canonical tables, mutates SQLite, trains, scores,
  calibrates, or changes readiness/eligibility.
- The prefill helper and templates are worksheets only — `DRAFT_NOT_ELIGIBLE`.
- Runtime provenance (what a live card is scored from) stays separate from
  training provenance (this pair).
- Filename dates and `mtime` are metadata, never as-of proof.

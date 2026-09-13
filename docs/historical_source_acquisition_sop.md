# Historical pre-race source acquisition SOP

This SOP governs how an operator acquires **one** leakage-safe historical target
race for the first supervised-training corpus. Reconnaissance
(`scripts/recon_historical_sources.py`) only tells you which local files *might*
be assembled and what evidence is still missing; it is **not** validation,
ingestion, or training eligibility. Every reconnaissance output is labelled
`RECON_ONLY_NOT_ELIGIBLE`.

The binding contract is
[`docs/historical_pre_race_snapshot_contract.md`](historical_pre_race_snapshot_contract.md);
the machine gate is `scripts/audit_historical_snapshot_contract.py`.

## Per-race procedure

1. **Capture a full race card before post.**
   Capture the complete entered field for the target race *before its scheduled
   post time*. A card pulled after post — or a later past-performance snapshot
   that merely lists the race in its history — is not a pre-race card. Using a
   later PP snapshot as a historical pre-race training card is
   `POST_RACE_TARGET_SNAPSHOT_LEAKAGE` and is prohibited.

2. **Preserve the unmodified raw bytes.**
   Store the file exactly as delivered. Do not re-save, re-export, reformat, or
   "clean" it. Record its SHA-256. The retained artifact and its hash are what
   the contract binds to.

3. **Capture a timezone-qualified source timestamp.**
   Record when the source content was current, with an explicit timezone, from
   one of:
   - a publisher/provider timestamp on the content itself (`PUBLISHER_TIMESTAMP`);
   - an HTTP response header or download-manager log (`SYSTEM_CAPTURED`);
   - a dated operator attestation describing exactly how and when the capture
     happened (`OPERATOR_ATTESTED`).
   A filename date or a filesystem modification time is **never** acceptable
   proof — `FILENAME_DERIVED` can never claim `PROVEN`, and reconnaissance never
   derives an as-of time from a filename or `mtime`.

4. **Capture a separately retained results artifact after official status.**
   After the race is declared official, acquire a *distinct* result/outcome
   document (chart / official result feed). Preserve its raw bytes, record its
   own SHA-256, provider, and parser version, and its own provenance
   (`PROVEN` or `OPERATOR_ATTESTED`). Labels stay separate from the pre-race
   card; a result file alone cannot make a race training-eligible.

5. **Create a manifest, then run the contract audit.**
   Start from
   [`templates/historical_pre_race_snapshot_manifest.template.json`](../templates/historical_pre_race_snapshot_manifest.template.json)
   (it fails validation until every value is replaced). Fill in the target-race
   identity, strict temporal ordering
   (`source_as_of_timestamp < target_scheduled_post_timestamp`), the complete
   non-scratched starter field, complete per-starter feature-vector status, and
   the outcome reference. Then:

   ```text
   python scripts/audit_historical_snapshot_contract.py --manifest path/to/manifest.json
   ```

   Only a manifest that exits `0` may be submitted for ingestion. Do **not**
   auto-generate a manifest from incomplete reconnaissance evidence.

## Provider recommendation

Begin with **DraftKings race-card Markdown**, and *only* because a deterministic
parser for it already exists (`src/ingest/draftkings_markdown.py`). This is not a
claim that the DraftKings path is production-complete: it is not proven until a
real manifest passes `audit_historical_snapshot_contract.py`. The DraftKings
Markdown parser also does not, on its own, supply a timezone-qualified
`source_as_of_timestamp` (its `as_of` defaults to run time) or a scheduled post
*timestamp* (it extracts only a wall-clock string) — those still come from
step 3 and the manifest.

DraftKings PDF and XLSX exports, Equibase SIMD XML entry cards, and Equibase
full-card result PDFs may appear in reconnaissance inventory, but no parser
currently extracts a contract-complete pre-race field from them.

## What reconnaissance can and cannot establish

| Can (metadata only) | Cannot (needs manifest + parse + results) |
|---|---|
| Candidate classification (pre-race card / result / unknown / unsupported) | Source-as-of timestamp, tier, or provenance |
| SHA-256 of exact bytes (read-only) | Timezone-qualified scheduled post timestamp |
| Provider candidate from filename/header | Complete non-scratched starter field |
| Track code/name, race number, filename date (marked `FILENAME_DERIVED_NOT_PROVEN`) | Per-starter feature-vector completeness / identity reconciliation |
| Deterministic pairing on exact track + date + race number | Confirmed surface / distance |
| Per-pair list of missing contract evidence | A retained, provenance-bearing outcome artifact |

# Historical pre-race snapshot acquisition contract

This is an acquisition gate for one historical target race. It does not ingest a
source, create records, build features, or make a race/model eligible on its own.
Its purpose is to ensure a subsequently acquired artifact can be bound to the
race it was captured to predict, rather than merely to a later card containing
past-performance history.

## Non-negotiable conditions

- Retain the immutable raw document or a durable immutable reference, and record
  the SHA-256 of its exact bytes.
- Record provider, parser/extraction version, and a source capture/as-of time
  with an explicit timezone.
- Declare an as-of tier of `PROVEN` or `OPERATOR_ATTESTED`. A filename-derived
  date is never `PROVEN`.
- Bind the document to exactly one historical target: track code, race date,
  race number, surface, distance, and scheduled post timestamp with timezone.
- Require strict temporal order: `source_as_of_timestamp <
  target_scheduled_post_timestamp`. Equality is not pre-race proof.
- Supply the complete non-scratched starter field. Every active starter must
  reconcile to a stable identity and every active starter must have a complete
  pre-race feature vector. Partial fields and partial vectors are rejected.
- Keep labels separate: provide a distinct, durable result/outcome reference
  with its own SHA-256, provider, parser version, and provenance status.
- The pre-race artifact may contain only information available before the target
  scheduled post. Post-race content is rejected.

A PP document captured for a later card can still be valid runtime evidence for
that later card. For an earlier race represented in its past performances it is
`POST_RACE_TARGET_SNAPSHOT_LEAKAGE`, never supervised-training evidence.

## Manifest fields

| Field | Type | Definition |
|---|---|---|
| `artifact_path_or_uri` | string | Local retained file path or durable immutable URI. |
| `sha256` | 64-char hex | SHA-256 of exact raw artifact bytes. |
| `source_provider` / `parser_version` | string | Provider and deterministic parser/extraction version. |
| `source_as_of_timestamp` | ISO-8601 date-time | Capture/as-of timestamp including `Z` or UTC offset. Date-only is rejected. |
| `source_as_of_tier` | enum | `PROVEN` or `OPERATOR_ATTESTED`. |
| `source_as_of_provenance` | enum | Timestamp provenance; `FILENAME_DERIVED` cannot claim `PROVEN`. |
| `target_track_code`, `target_race_date`, `target_race_number` | string/date/integer | Stable target-race identity. |
| `target_scheduled_post_timestamp` | ISO-8601 date-time | Timezone-qualified decision boundary for strict ordering. |
| `target_surface`, `target_distance_furlongs` | enum/number | Target race conditions. |
| `expected_active_starter_count`, `observed_active_starter_count` | integer | Both must agree and be at least two. |
| `identity_reconciliation_status` | enum | Must be `COMPLETE`; no unresolved active starter is allowed. |
| `field_completeness_status`, `feature_vector_status` | enum | Both must be `COMPLETE`; no partial-field/vector fallback. |
| `raw_artifact_retained`, `pre_race_fields_only` | boolean | Both must be `true`. |
| `outcome_reference` | object | Separate result artifact's immutable location, SHA-256, provider, parser version. |
| `outcome_provenance_status` | enum | Must be `PROVEN` or `OPERATOR_ATTESTED`. |

The JSON Schema is [historical_pre_race_snapshot_manifest.schema.json](../schemas/historical_pre_race_snapshot_manifest.schema.json).
Start from [historical_pre_race_snapshot_manifest.template.json](../templates/historical_pre_race_snapshot_manifest.template.json); it intentionally fails validation until every required value is replaced.

## Rejection codes

The validator reports machine-readable codes including:

- `MANIFEST_SCHEMA_INVALID`
- `SOURCE_AS_OF_TIMESTAMP_DATE_ONLY`
- `SOURCE_AS_OF_TIMESTAMP_INVALID_OR_TIMEZONE_MISSING`
- `TARGET_SCHEDULED_POST_TIMESTAMP_INVALID_OR_TIMEZONE_MISSING`
- `SOURCE_AS_OF_NOT_STRICTLY_BEFORE_TARGET_POST`
- `FILENAME_DERIVED_CANNOT_CLAIM_PROVEN`
- `ACTIVE_STARTER_COUNT_INSUFFICIENT`
- `IDENTITY_RECONCILIATION_INCOMPLETE`
- `PARTIAL_STARTER_FIELD` / `PARTIAL_FEATURE_VECTOR`
- `RAW_ARTIFACT_NOT_RETAINED` / `RAW_ARTIFACT_SHA256_MISMATCH`
- `PRE_RACE_FIELDS_ONLY_REQUIRED`
- `OUTCOME_PROVENANCE_INSUFFICIENT`

For an accessible local artifact, the audit calculates SHA-256 and reports a
match or mismatch. For a non-local or inaccessible durable reference, it reports
`DURABLE_REFERENCE_NOT_LOCALLY_VERIFIED`; it never fetches remote data.

## Running the audit

```text
python scripts/audit_historical_snapshot_contract.py --manifest path/to/manifest.json
```

The command is filesystem-only. It writes one deterministic result JSON below
`output/acceptance/`, exits `0` on a passing contract, and exits `2` otherwise.

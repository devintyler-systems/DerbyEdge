# Data and model provenance

DerbyEdge records provenance as small JSON manifests alongside metadata, never
inside the raw, model, or generated asset directories.  The contracts are JSON
Schema Draft 2020-12 documents in `schemas/`; validate a manifest with
`src.provenance.validate_manifest` before it is accepted or referenced.

## Lifecycle

1. A retrieved input is assigned an immutable `manifest_id` and a
   `raw_input_snapshot` manifest.  A checked-in synthetic or curated seed uses
   the same contract with `asset_class: curated_seed_fixture`, plus its stable
   `fixture_id` and `fixture_schema_version`.
2. A fitted model receives a `fitted_model_artifact` manifest that links its
   training input manifest IDs, feature contract, source commit, evaluation,
   promotion decision, and artifact checksum.
3. Each scoring decision receives a `scored_race_run` manifest linking the
   model, raw-input manifests, odds timestamp, decision-time code commit, and
   checksums of all delivered outputs.

Manifests are validated before persistence.  Validation fails closed: missing
fields, unknown fields, non-UTC timestamps, malformed checksums, empty lineage,
or an invalid probability sum reject the manifest.  The validator never fills
defaults or attempts to repair data.

## Immutable IDs and checksums

`manifest_id`, `fixture_id`, `model_id`, `run_id`, and `race_id` are stable,
opaque identifiers.  Do not reuse an ID for changed content.  A content change
creates a new manifest and a new ID; existing manifests remain append-only.

All `*_sha256` values are lowercase hexadecimal SHA-256 digests of the exact
bytes stored or delivered. `file_size_bytes` is the exact raw-input byte count.
`schema_fingerprint` and `feature_columns_sha256` are SHA-256 digests of the
canonical serialized schema/ordered feature-column contract. `output_sha256s`
maps each named scored output to its exact-file digest.

## Retention boundaries

Raw downloads, working extracts, fitted model binaries, generated score runs,
and evaluation-run artifacts are local or governed storage and are ignored by
Git. They must not be restored, staged, or rewritten as part of provenance
work. Version-controlled content is limited to contracts, documentation,
manifests, and deliberately small synthetic test fixtures. Production manifests
may be retained in governed metadata storage for the applicable data-license,
model-governance, and audit retention period; deleting an asset does not permit
rewriting its historical manifest.

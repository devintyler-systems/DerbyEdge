"""Fail-closed validation for DerbyEdge asset provenance manifests.

This module validates metadata only.  It deliberately does not read, copy, or
mutate the raw inputs, fitted artifacts, or scored output files they describe.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError


ManifestKind = Literal["raw_input", "model", "run"]

_SCHEMA_FILES: dict[ManifestKind, str] = {
    "raw_input": "raw_input_manifest.schema.json",
    "model": "model_manifest.schema.json",
    "run": "run_manifest.schema.json",
}
_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
_FORMAT_CHECKER = FormatChecker()


class ManifestValidationError(ValueError):
    """Raised when a manifest is incomplete or violates its contract."""


def _schema_for(kind: ManifestKind) -> dict[str, Any]:
    try:
        schema_file = _SCHEMA_FILES[kind]
    except KeyError as exc:
        raise ManifestValidationError(f"Unknown manifest kind: {kind!r}") from exc

    try:
        schema = json.loads((_SCHEMA_DIR / schema_file).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        # A missing or malformed contract must never be treated as validation success.
        raise ManifestValidationError(f"Cannot load {kind} manifest schema") from exc
    return schema


def validate_manifest(manifest: Mapping[str, Any], kind: ManifestKind) -> None:
    """Validate one manifest, raising on every missing or invalid field.

    No fields are defaulted and validation does not coerce input values.  Callers
    must persist a manifest only after this function returns successfully.
    """
    if not isinstance(manifest, Mapping):
        raise ManifestValidationError("Manifest must be a JSON object")

    validator = Draft202012Validator(_schema_for(kind), format_checker=_FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(dict(manifest)), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(
            f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}"
            for error in errors
        )
        raise ManifestValidationError(details)


def load_and_validate_manifest(path: str | Path, kind: ManifestKind) -> dict[str, Any]:
    """Read a JSON manifest and validate it before returning its contents."""
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f"Cannot read JSON manifest: {manifest_path}") from exc
    validate_manifest(payload, kind)
    return payload

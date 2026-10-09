"""Which engine produced a score run.

``engine_version`` = ``code-<fingerprint>/<model_name>@<model_version>``.

* The fingerprint is a hash of the source files that decide the probabilities (``ENGINE_PATHS``), with line endings
  normalised so Windows and Linux checkouts agree.  Editing docs, tests, ingest or the app does not change it; editing the
  scorer, trainer, features, policy, chaos or confidence code does.
* Every evaluation groups by this value, so races scored by different engines are never pooled into one verdict.
* ``NULL`` in ``score_runs.engine_version`` means the run predates the stamp and is reported as ``legacy``.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE_PATHS = ("src/models", "src/features")
LEGACY = "legacy"


@lru_cache(maxsize=4)
def code_fingerprint(root: str | None = None, paths: tuple[str, ...] = ENGINE_PATHS) -> str:
    base = Path(root) if root else ROOT
    digest = hashlib.sha256()
    for rel in paths:
        for f in sorted((base / rel).rglob("*.py")):
            digest.update(f.relative_to(base).as_posix().encode())
            digest.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:10]


def engine_version(artifact=None) -> str:
    """Never raises and never returns an empty value: a failure to fingerprint is itself a labelled cohort."""
    try:
        code = code_fingerprint()
    except Exception:
        code = "unknown"
    try:
        model = f"{artifact.model_name}@{artifact.version}" if artifact is not None else "no-model"
    except Exception:
        model = "unknown-model"
    return f"code-{code}/{model}"


def label(value: str | None) -> str:
    return value or LEGACY

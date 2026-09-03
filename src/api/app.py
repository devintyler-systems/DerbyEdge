"""
DerbyEdge HTTP API Service.

Provides REST endpoints for PDF race intake and model inference.
Endpoint: POST /api/ingest/pdf
"""
from __future__ import annotations

from email.parser import BytesParser
import json
from pathlib import Path
from typing import Any, Callable

from src.services.pdf_ingest import parse_race_pdf


def _json_response(data: dict[str, Any], status_code: int = 200) -> tuple[str, list[tuple[str, str]], list[bytes]]:
    status = f"{status_code} OK" if status_code == 200 else f"{status_code} Error"
    headers = [("Content-Type", "application/json")]

    def _default(obj: Any) -> Any:
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        if isinstance(obj, set):
            return list(obj)
        return str(obj)

    body = [json.dumps(data, default=_default).encode("utf-8")]
    return status, headers, body


def app(environ: dict[str, Any], start_response: Callable) -> list[bytes]:
    """WSGI application for DerbyEdge API."""
    path = environ.get("PATH_INFO", "")
    method = environ.get("REQUEST_METHOD", "GET").upper()

    if path == "/api/health":
        status, headers, body = _json_response({"status": "ok"})
        start_response(status, headers)
        return body

    if path == "/api/ingest/pdf" and method == "POST":
        content_type = environ.get("CONTENT_TYPE", "")
        content_length = int(environ.get("CONTENT_LENGTH", 0) or 0)
        req_body = environ["wsgi.input"].read(content_length)

        # Parse multipart form data
        msg = BytesParser().parsebytes(
            b"Content-Type: " + content_type.encode("latin1") + b"\r\n\r\n" + req_body
        )

        pdf_bytes = b""
        filename = "upload.pdf"
        for part in msg.walk():
            fn = part.get_filename()
            if fn:
                filename = fn
                pdf_bytes = part.get_payload(decode=True) or b""
                break

        if not pdf_bytes:
            status, headers, body = _json_response(
                {"ok": False, "error": "No file uploaded in multipart request"},
                status_code=400,
            )
            start_response(status, headers)
            return body

        # Temporary / cached upload location
        upload_dir = Path("data/runs/uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored_path = upload_dir / filename
        try:
            stored_path.write_bytes(pdf_bytes)
            stored_path_str = str(stored_path.resolve())
        except Exception:
            stored_path_str = str(Path(filename).resolve())

        result = parse_race_pdf(
            pdf_bytes,
            filename=filename,
            stored_path=stored_path_str,
        )

        clean_result = dict(result)
        if "parsed_race" in clean_result:
            del clean_result["parsed_race"]

        status, headers, body = _json_response(clean_result, status_code=200)
        start_response(status, headers)
        return body

    status, headers, body = _json_response({"error": "Not Found"}, status_code=404)
    start_response(status, headers)
    return body

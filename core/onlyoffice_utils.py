# core/onlyoffice_utils.py
from __future__ import annotations

import os
import re
import time
import json
import hashlib
from typing import Tuple, Dict, Any
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from django.conf import settings
from django.core.signing import Signer, BadSignature

# Use a stable salt so tokens are consistent across processes
_signer = Signer(salt=getattr(settings, "SIGNED_URL_SALT", "onlyoffice-download"))

# Keys that should be redacted when we show URLs in logs / responses
SENSITIVE_QS_KEYS = {
    "token",
    "access_token",
    "signature",
    "sig",
    "key",
    "X-Amz-Signature",
    "X-Amz-Credential",
    "X-Amz-Security-Token",
}


def make_signed_download_url(file_id: str, request=None, viewer_id=None) -> str:
    """
    Build a URL that OnlyOffice can call back to fetch the source file.
    We sign (file_id:exp) with Django's signer. The view will verify it.
    viewer_id: set for read-only opening when the owner's organisation watermarks copies; the signed
    download then serves a copy stamped for that person (file_id:exp:v<id>).

    The base URL is taken from settings.API_BASE_URL if provided,
    otherwise from the incoming request's scheme/host.
    """
    ttl = int(settings.ONLYOFFICE.get("DOWNLOAD_TTL", 300))
    expires = int(time.time()) + ttl
    token = _signer.sign(f"{file_id}:{expires}" + (f":v{int(viewer_id)}" if viewer_id else ""))
    qs = urlencode({"token": token, "file_id": file_id})

    base = (getattr(settings, "API_BASE_URL", "") or "").rstrip("/")
    if not base and request is not None:
        base = f"{request.scheme}://{request.get_host()}"

    return f"{base}/api/v1/core/onlyoffice/signed-download/?{qs}"


def verify_signed_download_token(token: str, expected_file_id: str) -> bool:
    """
    Validate the signed token and ensure the file_id matches and the token hasn't expired.
    """
    return signed_download_claims(token, expected_file_id)[0]


def signed_download_claims(token: str, expected_file_id: str):
    """(valid, viewer_id or None). viewer_id is present when the copy must be watermarked for that person."""
    try:
        parts = _signer.unsign(token).split(":")
        if len(parts) not in (2, 3):
            return False, None
        file_id, exp = parts[0], parts[1]
        viewer = int(parts[2][1:]) if len(parts) == 3 and parts[2].startswith("v") else None
        if len(parts) == 3 and viewer is None:
            return False, None
        return (file_id == str(expected_file_id) and int(exp) >= int(time.time())), viewer
    except (BadSignature, ValueError):
        return False, None


def ext_from_filename(name: str, fallback: str = "docx") -> str:
    """
    Extract the file extension (without leading dot), or use the fallback.
    """
    return (os.path.splitext(name)[1].lower().lstrip(".")) or fallback


def mime_for_ext(ext: str) -> str:
    """
    Minimal mapping for common Office/PDF types; falls back to octet-stream.
    """
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }.get(ext.lower(), "application/octet-stream")


def ensure_unique_path(dir_path: str, filename: str) -> str:
    """
    If a file already exists, append " (n)" before the extension until it's unique.
    """
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(dir_path, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(dir_path, f"{base} ({i}){ext}")
        i += 1
    return candidate


def file_md5(path: str, chunk_size: int = 1024 * 1024) -> str:
    """
    Streaming MD5 to avoid loading the whole file into memory.
    """
    md = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            md.update(chunk)
    return md.hexdigest()


def _shorten(s: str, keep: int = 4) -> str:
    """
    Redact helper: keep first/last `keep` chars if long; otherwise replace.
    """
    if not s:
        return ""
    if len(s) <= keep * 2 + 1:
        return "•••"
    return f"{s[:keep]}…{s[-keep:]}"


def redact_url(url: str, keep: int = 4) -> str:
    """
    Redacts sensitive query parameters in a URL (token, signature, etc) so we can
    safely include it in logs or API responses.
    """
    try:
        parts = urlsplit(url)
        q = parse_qsl(parts.query, keep_blank_values=True)
        redacted = []
        for k, v in q:
            if k in SENSITIVE_QS_KEYS:
                redacted.append((k, _shorten(v, keep)))
            else:
                redacted.append((k, v))
        new_qs = urlencode(redacted, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_qs, parts.fragment))
    except Exception:
        # If anything goes wrong, fall back to a coarse redaction
        return re.sub(r"(token|signature|access_token|sig)=([^&]+)", r"\1=[redacted]", url, flags=re.I)


def parse_ds_response(r) -> Tuple[bool, Dict[str, Any]]:
    """
    Parse OnlyOffice Document Server responses.

    Returns:
        (ok, data)
        ok == True  -> data is the parsed dict with success (error==0 or endConvert true)
        ok == False -> data is a dict with at least {"error": <code>} or a minimal {"status","body"}

    Notes:
      - /converter returns JSON like {"error":0, ...}
      - /ConvertService.ashx may return JSON or XML:
            <?xml version="1.0" encoding="utf-8"?>
            <FileResult><Error>-8</Error></FileResult>
    """
    ct = (r.headers.get("Content-Type") or "").lower()

    # Try JSON first if hinted by content-type
    if "application/json" in ct:
        try:
            data = r.json()
        except Exception:
            # Fall through to text parsing
            data = None
    else:
        data = None

    # If not JSON (or JSON failed), attempt minimal XML / text parsing
    if data is None:
        txt = r.text or ""
        # Very small XML shape: <FileResult><Error>-8</Error></FileResult>
        m = re.search(r"<\s*error\s*>\s*(-?\d+)\s*<\s*/\s*error\s*>", txt, flags=re.I)
        if m:
            try:
                code = int(m.group(1))
            except Exception:
                code = -1
            data = {"error": code}
        else:
            # Not recognizably XML error — return a minimal blob for diagnostics
            return False, {"status": r.status_code, "body": txt[:1000]}

    # Normalize common success case:
    # Some DS responses don't have explicit "error":0 but include endConvert or fileUrl.
    if isinstance(data, dict):
        # If explicit error and it's non-zero -> fail
        if "error" in data and data["error"] not in (0, "0", None):
            return False, data

        # Consider presence of endConvert/fileUrl as success signal
        if data.get("endConvert") or data.get("fileUrl"):
            # Ensure error=0 so callers don't have to check both styles
            data.setdefault("error", 0)
            return True, data

        # If neither explicit success nor explicit failure, pass through with status hint
        data.setdefault("status", r.status_code)
        return ("error" not in data or data["error"] in (0, "0", None)), data

    # Fallback: not a dict — treat as error with raw body excerpt
    return False, {"status": r.status_code, "body": (r.text or "")[:1000]}


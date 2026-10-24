"""Document intake: uploads, URL fetching (with SSRF guard), classification,
and LibreOffice-based conversion of DOCX/HTML to PDF for the Ollama engine.

A job always works on a local file in scratch space, whether it arrived as an
upload or was pulled from the web.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, UploadFile

from . import config

_log = logging.getLogger(__name__)

_CHUNK = 1024 * 256


class IntakeError(Exception):
    """User-facing intake failure (bad file, unreachable URL, ...)."""


def classify(path: Path) -> str:
    """Return 'pdf' | 'docx' | 'html' | 'image' for a stored file."""
    ext = path.suffix.lower()
    if ext in config.PDF_EXTENSIONS:
        return "pdf"
    if ext in config.DOCX_EXTENSIONS:
        return "docx"
    if ext in config.HTML_EXTENSIONS:
        return "html"
    if ext in config.IMAGE_EXTENSIONS:
        return "image"
    # No/unknown extension: sniff a few magic signatures.
    head = path.open("rb").read(8)
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:4] == b"PK\x03\x04":  # docx is a zip; xlsx/pptx too but unsupported
        return "docx"
    if head[:2] in (b"\xff\xd8", b"\x89P"):  # jpeg / png
        return "image"
    if head[:4] == b"RIFF" and path.open("rb").read(8)[8:12] == b"WEBP":
        return "image"
    text = path.open("r", errors="ignore").read(512).lower()
    if "<html" in text or "<!doctype html" in text:
        return "html"
    raise IntakeError(
        f"Unsupported file type {ext or '(no extension)'}. "
        f"Supported: PDF, DOCX, HTML, JPEG, WEBP, PNG."
    )


def _safe_name(name: str) -> str:
    base = Path(name).name if name else "document"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:120] or "document"
    return base


async def store_upload(upload: UploadFile, scratch: Path) -> tuple[Path, str]:
    """Stream an upload to scratch space. Returns (path, display name)."""
    dest = scratch / f"{uuid.uuid4().hex[:10]}_{_safe_name(upload.filename or 'upload')}"
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await upload.read(_CHUNK):
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise IntakeError(
                        f"Upload exceeds the {config.MAX_UPLOAD_BYTES // (1024*1024)} MB limit."
                    )
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return dest, upload.filename or dest.name


def _assert_public_host(hostname: str) -> None:
    if config.ALLOW_PRIVATE_URLS:
        return
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise IntakeError(f"Could not resolve host {hostname!r}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise IntakeError(
                f"Refusing to fetch a private/local address ({ip}). "
                "Set DATLENS_ALLOW_PRIVATE_URLS=1 to allow LAN sources."
            )


def fetch_url(url: str, scratch: Path) -> tuple[Path, str]:
    """Download url into scratch space."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise IntakeError("URL must start with http:// or https://")
    if not parsed.netloc:
        raise IntakeError(f"URL has no host: {url}")
    _assert_public_host(parsed.netloc.split(":")[0])

    name = Path(parsed.path).name or "document"
    dest = scratch / f"{uuid.uuid4().hex[:10]}_{_safe_name(name)}"
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=config.URL_FETCH_TIMEOUT,
            headers={"User-Agent": "DatLens_OCR/0.1 (+local document fetcher)"},
        ) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise IntakeError(f"URL fetch failed with HTTP {resp.status_code}.")
                size = 0
                with dest.open("wb") as out:
                    for chunk in resp.iter_bytes(_CHUNK):
                        size += len(chunk)
                        if size > config.MAX_URL_BYTES:
                            raise IntakeError(
                                f"URL content exceeds the "
                                f"{config.MAX_URL_BYTES // (1024*1024)} MB limit."
                            )
                        out.write(chunk)
    except IntakeError:
        dest.unlink(missing_ok=True)
        raise
    except httpx.HTTPError as exc:
        dest.unlink(missing_ok=True)
        raise IntakeError(f"Could not fetch {url}: {exc}") from exc

    # If the server didn't tell us a useful filename, classify will sniff.
    if name in ("", "index.html") and parsed.path.endswith(("/", "")):
        dest = dest.with_name(dest.name + ".html")
    return dest, url


def ensure_pdf(src: Path, scratch: Path) -> Path:
    """Convert DOCX/HTML to PDF via headless LibreOffice (for Ollama engine).

    PDFs and images are returned unchanged. Raises IntakeError when the
    conversion is impossible.
    """
    kind = classify(src)
    if kind in ("pdf", "image"):
        return src
    if shutil.which("soffice") is None:
        raise IntakeError(
            "This document type needs LibreOffice to be rendered to PDF for "
            "the Ollama engine, but 'soffice' was not found on this machine. "
            "Install LibreOffice, or upload the document as a PDF/image."
        )
    outdir = Path(tempfile.mkdtemp(prefix="datlens_lo_", dir=scratch))
    try:
        proc = subprocess.run(
            [
                "soffice",
                "--headless",
                "--norestore",
                f"-env:UserInstallation=file://{outdir}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(scratch),
                str(src),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise IntakeError("LibreOffice conversion timed out.") from exc
    pdf = scratch / f"{src.stem}.pdf"
    if proc.returncode != 0 or not pdf.exists():
        raise IntakeError(
            "Could not convert the document to PDF for image rendering: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return pdf

"""DatLens_OCR — FastAPI application.

Endpoints (task-queue flow, in-process):
  POST /api/jobs/upload   multipart files + options  -> {jobs: [...]}
  POST /api/jobs/url      JSON {url, ...}            -> {job_id}
  GET  /api/jobs          list job snapshots
  GET  /api/jobs/{id}     job snapshot (status/progress)
  GET  /api/jobs/{id}/events   SSE progress stream
  GET  /api/jobs/{id}/pages    per-page text + thumbnails (review UI)
  GET  /api/jobs/{id}/result?format=markdown|html|json|text
  POST /api/jobs/{id}/cancel   stop a queued/running job
  DELETE /api/jobs/{id}        (also cancels an active job)
  GET  /api/ollama/models?base_url=...   proxy to your Ollama server
  GET  /api/health, GET /api/config
"""

from __future__ import annotations

import asyncio
import logging
import queue
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .intake import IntakeError, classify, fetch_url, store_upload
from .outputs import available_formats
from .tasks import PageRecord, TaskRegistry

_log = logging.getLogger("datlens_ocr")

config.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
registry = TaskRegistry(retention=config.JOB_RETENTION, max_concurrent=config.MAX_CONCURRENT_JOBS)

app = FastAPI(title="DatLens_OCR", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local tool; reachable from other LAN devices
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- helpers

def normalize_ollama_url(value: str) -> str:
    """Validate a user-entered Ollama base URL (Local-OCR behavior)."""
    url = (value or "").strip().rstrip("/")
    if not url:
        raise ValueError("Ollama server URL is empty.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Ollama server URL must start with http:// or https://")
    if not parsed.netloc:
        raise ValueError(f"Ollama server URL has no host: {url!r}")
    return url


def _validate_options(engine: str, base_url: str, model: str, dpi: int) -> dict[str, Any]:
    if engine not in ("auto", "ollama"):
        raise HTTPException(422, f"Unknown engine {engine!r}; use auto|ollama.")
    if dpi not in config.DPI_OPTIONS:
        raise HTTPException(422, f"DPI must be one of {list(config.DPI_OPTIONS)}.")
    ollama_url = normalize_ollama_url(base_url)  # raises ValueError -> 422 below
    return {"engine": engine, "base_url": ollama_url, "model": model.strip(), "dpi": dpi}


@app.exception_handler(ValueError)
async def _value_error_handler(_: Request, exc: ValueError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def _run_job(job, src_path: Path, opts: dict[str, Any]) -> None:
    """Worker body: run the Ollama engine, store outputs on the job."""
    from .engines import ollama_engine
    from .engines.base import EngineOptions

    emit = job.emit
    engine_opts = EngineOptions(
        scratch=src_path.parent,
        ollama_url=opts["base_url"],
        model=opts["model"],
        dpi=opts["dpi"],
        source_desc=job.source_desc,
        is_cancelled=lambda: job.cancel_event.is_set(),
        cancel_event=job.cancel_event,
    )
    emit("status", {"phase": f"Connecting to Ollama at {opts['base_url']}"})
    result = ollama_engine.run(src_path, engine_opts, emit)

    job.pages = [
        PageRecord(page=p.page, text=p.text, image_b64=p.image_b64) for p in result.pages
    ]
    job.total_pages = len(result.pages) or None
    job.result = {**result.to_outputs(), "warnings": result.warnings}
    job.status = "done"
    job.finished_at = time.time()
    emit(
        "done",
        {
            "processing_time": round(job.finished_at - (job.started_at or job.created_at), 2),
            "warnings": result.warnings,
            "formats": list(available_formats()),
        },
    )


def _start_job(src_path: Path, name: str, opts: dict[str, Any]) -> "Any":
    try:
        kind = classify(src_path)
    except IntakeError as exc:
        raise HTTPException(422, str(exc)) from exc
    engine = "ollama"  # single-engine build; "auto" and "ollama" both map here
    if not opts["model"]:
        raise HTTPException(
            422,
            "The Ollama engine needs a model. Pick one in the Ollama panel.",
        )
    job = registry.create(
        source_desc=name,
        engine=engine,
        detail={
            "model": opts["model"] or None,
            "dpi": opts["dpi"],
            "base_url": opts["base_url"],
            "kind": kind,
        },
    )
    job.scratch_dir = src_path.parent  # type: ignore[attr-defined]
    registry.start_worker(job, lambda j: _run_job(j, src_path, opts))
    return job


# ------------------------------------------------------------------ routes

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "app": "DatLens_OCR",
        "version": "0.1.0",
        "engine": "ollama",
        "libreoffice": shutil.which("soffice") is not None,
        "formats": list(available_formats()),
    }


@app.get("/api/config")
async def api_config():
    return {
        "default_ollama_url": config.DEFAULT_OLLAMA_URL,
        "example_models": config.EXAMPLE_VISION_MODELS,
        "dpi_options": list(config.DPI_OPTIONS),
        "default_dpi": config.DEFAULT_DPI,
        "supported_extensions": sorted(config.SUPPORTED_EXTENSIONS),
        "formats": list(available_formats()),
    }


@app.get("/api/ollama/models")
async def ollama_models(base_url: Annotated[str, Query()] = config.DEFAULT_OLLAMA_URL):
    """Proxy to the Ollama server so the browser never needs CORS access."""
    normalized = normalize_ollama_url(base_url)
    import httpx

    try:
        async with httpx.AsyncClient(timeout=config.OLLAMA_LIST_TIMEOUT) as client:
            resp = await client.get(f"{normalized}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        models = sorted(
            {m.get("name") for m in data.get("models", []) if isinstance(m, dict) and m.get("name")}
        )
        return {"base_url": normalized, "models": models, "reachable": True}
    except Exception as exc:  # noqa: BLE001 - UI shows a friendly status chip
        return {
            "base_url": normalized,
            "models": [],
            "reachable": False,
            "error": str(exc),
        }


class UrlJobRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    engine: str = "auto"
    base_url: str = config.DEFAULT_OLLAMA_URL
    model: str = ""
    dpi: int = config.DEFAULT_DPI


@app.post("/api/jobs/upload", status_code=202)
async def create_upload_jobs(
    files: Annotated[list[UploadFile], File(description="PDF/DOCX/HTML/JPEG/WEBP/PNG files")],
    engine: Annotated[str, Form()] = "auto",
    base_url: Annotated[str, Form()] = config.DEFAULT_OLLAMA_URL,
    model: Annotated[str, Form()] = "",
    dpi: Annotated[int, Form()] = config.DEFAULT_DPI,
):
    if not files:
        raise HTTPException(422, "No files provided.")
    opts = _validate_options(engine, base_url, model, dpi)

    jobs = []
    for upload in files:
        job_dir = Path(tempfile.mkdtemp(prefix=f"job_{uuid.uuid4().hex[:8]}_", dir=config.SCRATCH_DIR))
        try:
            src_path, name = await store_upload(upload, job_dir)
            job = _start_job(src_path, name, opts)
        except Exception:
            # No job owns this directory once startup fails — clean it up.
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        jobs.append({"job_id": job.id, "name": name, "engine": job.engine})
    return {"jobs": jobs}


@app.post("/api/jobs/url", status_code=202)
async def create_url_job(req: UrlJobRequest):
    opts = _validate_options(req.engine, req.base_url, req.model, req.dpi)
    job_dir = Path(tempfile.mkdtemp(prefix=f"job_{uuid.uuid4().hex[:8]}_", dir=config.SCRATCH_DIR))
    try:
        src_path, name = await asyncio.to_thread(fetch_url, req.url, job_dir)
        job = _start_job(src_path, name, opts)
    except IntakeError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    return {"job_id": job.id, "name": name, "engine": job.engine}


@app.get("/api/jobs")
async def list_jobs():
    return {"jobs": registry.list_jobs()}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job.")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/pages")
async def job_pages(job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job.")
    return {
        "total": len(job.pages),
        "pages": [
            {"page": p.page, "text": p.text, "image_b64": p.image_b64} for p in job.pages
        ],
    }


@app.get("/api/jobs/{job_id}/result")
async def job_result(
    job_id: str,
    format: Annotated[str, Query()] = "markdown",
):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job.")
    if job.status == "error":
        raise HTTPException(502, f"Job failed: {job.error}")
    if job.status == "cancelled":
        raise HTTPException(410, "Job was cancelled — no result.")
    if not job.result:
        raise HTTPException(
            409, f"Result not ready yet (status: {job.status}). Poll /api/jobs/{job_id}."
        )
    if format not in available_formats():
        raise HTTPException(422, f"format must be one of {list(available_formats())}.")
    content = job.result[format]
    media = {
        "markdown": "text/markdown; charset=utf-8",
        "html": "text/html; charset=utf-8",
        "json": "application/json; charset=utf-8",
        "text": "text/plain; charset=utf-8",
    }[format]
    return Response(content=content, media_type=media)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Ask a queued/running job to stop. Engines check between pages; a
    mid-page Ollama stream is interrupted by closing its connection."""
    job = registry.cancel(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job.")
    return {"job_id": job.id, "status": job.status, "cancelling": not job.terminal}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job.")
    was_active = not job.terminal
    if was_active:
        # Stop the worker first so it releases its concurrency slot instead
        # of keeping running (and blocking other jobs) after deletion.
        job.cancel()
    registry.delete(job_id)
    return {"deleted": job_id, "cancelled": was_active}


# ---------------------------------------------------------------------- SSE

def _sse(name: str, data: dict[str, Any]) -> str:
    import json

    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request):
    """Server-Sent Events stream: replayed history, then live progress."""
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job.")

    async def gen():
        # Replay history first, then subscribe: late events reach the queue
        # exactly once (a tiny gap window exists but duplicates don't).
        history = job.history()
        q = job.subscribe()
        try:
            for name, data in history:
                yield _sse(name, data)
                if await request.is_disconnected():
                    return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.to_thread(q.get, timeout=15)
                except queue.Empty:
                    if job.terminal:
                        return
                    yield ": keepalive\n\n"
                    continue
                name, data = ev["event"], ev["data"]
                yield _sse(name, data)
                if job.terminal and name in ("status", "done"):
                    return
        finally:
            job.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------- static

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ------------------------------------------------------------------- entry

def run() -> None:
    """uvicorn entry point: `python -m datlens_ocr.main`."""
    import uvicorn

    uvicorn.run("datlens_ocr.main:app", host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    run()

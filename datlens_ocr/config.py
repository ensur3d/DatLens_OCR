"""Configuration for the DatLens_OCR web app (environment-overridable)."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- server -------------------------------------------------------------
HOST = os.environ.get("DATLENS_HOST", "0.0.0.0")
PORT = _env_int("DATLENS_PORT", 8901)

# Where uploaded / fetched documents are kept while a job runs.
SCRATCH_DIR = Path(os.environ.get("DATLENS_SCRATCH_DIR", str(BASE_DIR / "scratch")))

# --- limits --------------------------------------------------------------
# Uploads are streamed to disk; reject beyond this size (bytes).
MAX_UPLOAD_BYTES = _env_int("DATLENS_MAX_UPLOAD_MB", 200) * 1024 * 1024
# Same cap for documents fetched from a URL.
MAX_URL_BYTES = _env_int("DATLENS_MAX_URL_MB", 200) * 1024 * 1024
URL_FETCH_TIMEOUT = 60.0  # seconds

# By default the server refuses to fetch private/loopback addresses (SSRF).
# Set DATLENS_ALLOW_PRIVATE_URLS=1 when you intentionally want to pull
# documents from other machines on your LAN (e.g. a local file share).
ALLOW_PRIVATE_URLS = _env_bool("DATLENS_ALLOW_PRIVATE_URLS", False)

# Max jobs converted in parallel (Ollama workers).
MAX_CONCURRENT_JOBS = max(1, _env_int("DATLENS_MAX_CONCURRENT_JOBS", 2))
# Finished jobs kept in memory (results downloadable until pruned).
JOB_RETENTION = max(1, _env_int("DATLENS_JOB_RETENTION", 20))

# --- Ollama --------------------------------------------------------------
DEFAULT_OLLAMA_URL = "http://localhost:11434"
# Seconds of silence between stream chunks before a page request is stuck.
# (Same idea as Local-OCR: with stream=True the timeout applies to the pause
# between chunks, so long pages don't trip a single overall timeout.)
try:
    OLLAMA_STREAM_IDLE_TIMEOUT = float(os.environ.get("DATLENS_OLLAMA_TIMEOUT", "180"))
except ValueError:
    OLLAMA_STREAM_IDLE_TIMEOUT = 180.0
OLLAMA_LIST_TIMEOUT = 10.0

# Hard cap on output tokens per page. Some vision models loop forever on
# degenerate input (e.g. a blank page); this bounds the worst case.
MAX_PAGE_TOKENS = _env_int("DATLENS_MAX_PAGE_TOKENS", 8192)

# Absolute wall-clock cap for a single page's model request, regardless of
# whether chunks keep arriving (the idle timeout above only catches full
# silence). OCR-specialized models such as deepseek-ocr can stream slowly
# for minutes on dense pages, so the default is generous; lower it if your
# server is fast and you want stuck pages abandoned sooner.
try:
    OLLAMA_PAGE_WALL_TIMEOUT = float(os.environ.get("DATLENS_PAGE_TIMEOUT", "600"))
except ValueError:
    OLLAMA_PAGE_WALL_TIMEOUT = 600.0

# Repetition-loop detector for streamed model output. OCR-specialized models
# (glm-ocr, deepseek-ocr) sometimes lock into repeating a phrase endlessly on
# certain pages; with only the token cap in place that burns minutes per bad
# page. The guard watches sliding windows of the stream and aborts the page
# when one short n-gram dominates it (a pure loop scores ~1.0 coverage, real
# text stays well below) or the window is character-periodic at some lag up to
# REP_MAX_PERIOD (longer sentence/paragraph loops).
REP_WINDOW_CHARS = 1200      # sliding window size (chars) inspected
REP_NGRAM = 16               # substring length counted
REP_MIN_COUNT = 4            # occurrences of one n-gram needed to suspect a loop
REP_MIN_COVERAGE = 0.5       # ...and its span must cover >= this share of the window
REP_MAX_PERIOD = 600         # max loop period (chars) the periodicity check looks for
REP_MIN_PERIODICITY = 0.85   # char-match ratio at a candidate lag that counts as a loop

# Extra guard tiers, added after glm-ocr re-emitted an ENTIRE page ~20 times on
# blue_test.pdf p.26 (period ~1010 chars — beyond REP_MAX_PERIOD) and other
# pages locked into short unit loops ("```" x 37, "44" x 14, TOC entries x 8)
# that never fill the small window above.
REP_LINE_PERIOD_MAX = 6      # max line period checked (1 = identical consecutive lines)
REP_LINE_MIN_REPEATS = 8     # p=1: same non-blank line repeated this many times in a row
REP_LINE_MIN_CYCLES = 5      # p>=2: full cycles of the line period required before firing
REP_LINE_CONSISTENCY = 0.85  # fraction of tail positions matching their p-shifted copy (tolerates one corrupted/truncated line in the run)
REP_LONG_WINDOW = 8000       # chars retained for the long-loop scans below
REP_MAX_PERIOD_LONG = 3000   # max loop period (chars) for the trailing-window scan
REP_MIN_PERIODICITY_LONG = 0.9  # char-match ratio over the last two periods that counts
REP_COPY_NGRAM = 256         # substring length for the re-emission counter (shorter spans recur legitimately: shared rule headers / command prefixes in technical docs)
REP_COPY_MIN_COUNT = 4       # occurrences of one such chunk in REP_LONG_WINDOW to suspect a loop
REP_COPY_MAX_SPACING = 2048  # max gap (chars) between consecutive occurrences of it

# Suggestion only — never assumed to exist on the server, never pulled.
EXAMPLE_VISION_MODELS = ["llava:13b", "minicpm-v", "qwen2.5vl:7b"]

# --- conversion ----------------------------------------------------------
DPI_OPTIONS = (100, 150, 200, 300)
DEFAULT_DPI = 150

# A page whose dark-pixel share is below this fraction is treated as blank
# and skipped before the vision model (some models hang on blank pages).
BLANK_PAGE_INK_FRACTION = 0.001

PDF_EXTENSIONS = frozenset({".pdf"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
DOCX_EXTENSIONS = frozenset({".docx"})
HTML_EXTENSIONS = frozenset({".html", ".htm"})
SUPPORTED_EXTENSIONS = (
    PDF_EXTENSIONS | IMAGE_EXTENSIONS | DOCX_EXTENSIONS | HTML_EXTENSIONS
)

# Longest side (px) of the page preview thumbnail sent to the UI.
THUMBNAIL_MAX_SIDE = 900
# Images larger than this on the longest side are downscaled before being
# sent to the vision model (keeps token usage / memory sane).
MAX_MODEL_IMAGE_SIDE = 4096
MODEL_IMAGE_CAP_SIDE = 2048

SYSTEM_PROMPT = (
    "Convert this image into Markdown text format. Your task is to perform "
    "high-accuracy Optical Character Recognition (OCR). Preserve the "
    "document's structure as accurately as possible: headers, lists, and "
    "tables. Do not add any greetings, explanations, or "
    "introductory/concluding remarks. Output only the raw recognized text."
)

USER_PROMPT = "Recognize this document page."

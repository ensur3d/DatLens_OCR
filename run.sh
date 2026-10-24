#!/usr/bin/env bash
# DatLens_OCR (Ollama-only build) — start the web app.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

# Environment overrides (all optional — see README for details):
#   DATLENS_HOST / DATLENS_PORT        bind address (default 0.0.0.0:8901)
#   DATLENS_SCRATCH_DIR               temp storage for uploads/fetches
#   DATLENS_MAX_CONCURRENT_JOBS       parallel jobs (default 2)
#   DATLENS_ALLOW_PRIVATE_URLS=1      allow fetching private/LAN URLs
#   DATLENS_OLLAMA_TIMEOUT            seconds of stream silence tolerated per page (default 180)
#   DATLENS_PAGE_TIMEOUT              per-page wall-clock budget for Ollama OCR (default 600)
exec ./.venv/bin/python -m datlens_ocr

# DatLens_OCR (Ollama-only build) — production image
#
# Build:   docker build -t datlens-ocr-ollama .
# Run:     docker run -d --name datlens -p 8901:8901 datlens-ocr-ollama
#          → http://localhost:8901
#
# Notes:
#   * No docling / torch in this image — all conversion goes through your
#     Ollama vision LLM, so the image is small. Nothing to pre-download.
#   * Ollama is not included — point the UI at your existing (remote) Ollama
#     server; the container just needs network reachability to it.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Headless LibreOffice (writer only, no GUI) — needed to convert DOCX/HTML
# uploads to PDF before page rendering. Skip with:
#   docker build --build-arg WITH_LIBREOFFICE=0 -t datlens-ocr-ollama .
ARG WITH_LIBREOFFICE=1
RUN if [ "$WITH_LIBREOFFICE" = "1" ]; then \
        apt-get update && \
        apt-get install -y --no-install-recommends \
            libreoffice-writer-nogui \
            fonts-dejavu-core && \
        rm -rf /var/lib/apt/lists/*; \
    fi

# Python dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code (owned by the runtime user; modes normalized so the
# non-root user can read everything regardless of source permissions).
COPY --chown=1000:1000 datlens_ocr ./datlens_ocr
COPY --chown=1000:1000 static ./static
RUN chmod -R a+rX /app

# In-flight uploads/fetched documents live in /data/scratch (ephemeral; the
# app cleans up when jobs finish). Mount a volume here if you want to inspect
# leftovers after a crash.
ENV DATLENS_SCRATCH_DIR=/data/scratch
RUN mkdir -p /data/scratch && chown -R 1000:1000 /data

# Run as an unprivileged user; the app code itself is read-only.
USER 1000:1000

EXPOSE 8901

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8901/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "datlens_ocr"]

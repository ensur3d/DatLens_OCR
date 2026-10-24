# DatLens_OCR — Ollama Edition

<img width="1295" height="808" alt="Screenshot from 2026-10-23 21-48-26" src="https://github.com/user-attachments/assets/40737f81-2df9-4e02-a691-0fdbfb05923e" />

A modern web app for document OCR. Upload PDF / DOCX / HTML / images (or paste a
public URL) and get **Markdown, HTML, JSON or plain text** out — with live
per-page progress in the browser. Dark theme by default, light theme on toggle.

This build ships **one engine**:

| Engine  | What it does | Best for |
|---------|--------------|----------|
| **Ollama** | Renders pages to images and sends them to a **vision LLM on your Ollama server** (streaming, page by page — the Local-OCR approach). PDFs render via PyMuPDF, images are used directly, DOCX/HTML convert to PDF via headless LibreOffice first. | Scans, handwriting, structured docs — anything; remote model of your choice |

Because there is no local layout-model stack (no docling/torch), the Docker
image is small and there is **nothing to pre-download** — the only model that
matters lives on your Ollama server.

## Quick start



### Docker Compose (recommended)

Same steps on Windows, macOS and Linux — all you need is [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/macOS) or the [Docker Engine + Compose plugin](https://docs.docker.com/compose/install/) (Linux). Nothing else to install:

```bash
git clone https://github.com/ensur3d/DatLens_OCR.git
cd DatLens_OCR
docker compose up -d --build
# → open http://127.0.0.1:8901
```

Ollama is **not** part of this stack — in the UI, point the Ollama panel at your remote server (e.g. `http://192.168.x.x:11434`) and pick a vision-capable model.

### Docker Build (optional)

```bash
git clone https://github.com/ensur3d/DatLens_OCR.git
cd DatLens_OCR
docker build -t datlens-ocr-ollama .
docker run -d --name datlens -p 8901:8901 datlens-ocr-ollama
# → open http://127.0.0.1:8901
```

The container needs network reachability to your Ollama server (same LAN, or a
public endpoint) — enter it in the UI's **Ollama server** panel. No volumes are
required; uploads live in `/data/scratch` inside the container and are cleaned
up when jobs finish (mount a volume there if you want crash forensics).

Skip LibreOffice in the image if you never send DOCX/HTML to the app:
`docker build --build-arg WITH_LIBREOFFICE=0 -t datlens-ocr-ollama .`


| Task | Command |
|------|---------|
| Start (builds the image if needed) | `docker compose up -d --build` |
| Follow logs | `docker compose logs -f` |
| Stop | `docker compose down` |
| Update after code changes | `docker compose up -d --build` |

- No volumes are needed: uploads live in `/data/scratch` inside the container and are cleaned up when jobs finish.
- Port 8901 taken, or running both releases side by side? Override it: `DATLENS_PORT=8902 docker compose up -d --build`.

### Bare metal

```bash
git clone https://github.com/ensur3d/DatLens_OCR.git
cd DatLens_OCR
./run.sh                 # creates .venv + installs deps on first run
# → open http://localhost:8901  (or http://<this-machine>:8901 from other devices)
```

Requires Python 3.10+ and (for DOCX/HTML input) LibreOffice (`soffice` on PATH).
PDF/image inputs need no extra system packages.

## Connecting your Ollama server

Ollama is **not** installed or started by this app — it connects to an existing
instance over HTTP (e.g. on another machine on your LAN):

1. In the sidebar, set **Server URL** (e.g. `http://192.168.1.50:11434`) and hit refresh.
2. Pick a **vision-capable model** (`glm-ocr` works well with this has been tested. Others may work too...). The chip
   in the header shows whether the server is reachable.
3. Hit **Start OCR**.

The model list is fetched server-side, so no browser CORS issues. The URL you enter
is remembered between sessions.

## Inputs / outputs

- **Inputs:** `.pdf`, `.docx`, `.html`/`.htm`, `.png`, `.jpg`/`.jpeg`, `.webp` —
  multiple files at once (one job per file), or a public document URL
  (private/LAN URLs are blocked by default; set `DATLENS_ALLOW_PRIVATE_URLS=1` to allow).
- **Outputs:** Markdown, HTML (rendered preview + standalone document), JSON
  (structured page data: model, DPI, per-page text and timing), Text.
  Copy or download from the results panel; review page-by-page in the *Pages* tab.

## Configuration (environment variables)

| Variable | Default | Meaning |
|----------|---------|---------|
| `DATLENS_HOST` / `DATLENS_PORT` | `0.0.0.0` / `8901` | Bind address |
| `DATLENS_SCRATCH_DIR` | `./scratch` (container: `/data/scratch`) | Temp storage for uploads/fetched URLs (cleaned when jobs finish) |
| `DATLENS_MAX_CONCURRENT_JOBS` | `2` | Jobs running in parallel |
| `DATLENS_JOB_RETENTION` | `20` | Finished jobs kept in memory |
| `DATLENS_MAX_UPLOAD_MB` / `DATLENS_MAX_URL_MB` | `200` | Size caps |
| `DATLENS_ALLOW_PRIVATE_URLS` | off | Permit fetching private/LAN URLs |
| `DATLENS_OLLAMA_TIMEOUT` | `180` | Seconds of stream silence tolerated per page |
| `DATLENS_PAGE_TIMEOUT` | `600` | Per-page wall-clock budget for Ollama OCR (seconds); a page that exceeds it is aborted and the job continues |

## Robustness: looping models, long documents, cancellation

OCR-specialized vision models (glm-ocr, deepseek-ocr) can lock into a repetition
loop on certain pages — emitting one phrase thousands of times until the token cap.
The engine defends against this so a bad page never stalls a job:

- **Repetition guard** — layered checks over the streamed text detect degenerate
  loops within a few copies of the loop starting: one n-gram dominating a short
  window; a repeating line cycle (up to 6 lines, tolerating an occasional
  truncated copy); character-level periodicity up to ~3000 chars (whole-page
  re-emission, with per-copy drift); and a counter for the same ~256-char chunk
  being re-emitted several times back-to-back. The page is aborted, whatever text
  already arrived is kept as that page's result, and a warning is recorded.
- **Per-page wall-clock budget** — `DATLENS_PAGE_TIMEOUT` (default 10 min) bounds
  even a page that streams slowly but never finishes; same abort-and-continue
  behavior. On abort the model stream is closed immediately so the server stops
  generating.
- **Blank pages are skipped** before ever calling the model (a known hang trigger).
- **Fail-fast diagnostics** — if *page 1* comes back empty, the job fails with a
  clear "is this a vision-capable model?" error instead of producing N empty pages;
  empty later pages become warnings.

The job always ends `done` (or cancelled) — degenerate pages show up as per-page
warnings in the log and are visible in the results panel, not as infinite hangs.

Cancelling a running job stops it between pages: the model stream is closed, the
job slot is released immediately, and any text already received for the current
page is kept.

## Architecture

```
datlens_ocr/
  main.py            FastAPI app: jobs API, SSE progress stream, Ollama proxy, static UI
  config.py          env-overridable settings (ports, caps, prompts, DPI options)
  tasks.py           in-memory job registry + pub/sub
  intake.py          upload/URL handling, format sniffing, SSRF guard, LibreOffice bridge
  outputs.py         markdown → html/text rendering, result dispatch
  engines/
    base.py          EngineOptions / EngineResult contracts
    ollama_engine.py   page render (PyMuPDF/PIL) → streaming vision-LLM OCR
static/              vanilla-JS SPA (no build step): index.html, styles.css, app.js
run.sh               bare-metal launcher (venv + deps on first run)
Dockerfile           production image (python:3.12-slim, headless LibreOffice, non-root)
```

Job flow: `POST /api/jobs/upload|url` → worker thread runs the engine → progress
events stream over SSE (`status`, `log`, `page_image`, `stream_chunk`, `page_text`,
`done`) → results fetched on demand from `/api/jobs/{id}/result?format=...`.

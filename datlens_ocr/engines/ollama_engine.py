"""Ollama vision-LLM OCR engine.

Pipeline (the Local-OCR flow, adapted for the web):
  PDF            -> render every page to PNG with PyMuPDF at the chosen DPI
  JPEG/WEBP/PNG  -> used directly (downscaled only when extremely large)
  DOCX / HTML    -> converted to PDF via headless LibreOffice, then rendered

Each page image is sent to the configured Ollama model with a structured
OCR prompt; responses are streamed so the UI shows text live, page by page.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import threading
import time

import pymupdf
from PIL import Image

from .. import config
from ..intake import IntakeError, classify, ensure_pdf
from ..tasks import JobCancelled
from .base import EmitFn, EngineOptions, EngineResult, PageResult

_log = logging.getLogger(__name__)


class OllamaEngineError(Exception):
    """Raised when the Ollama engine cannot run (bad config, server down...)."""


class PageAborted(Exception):
    """One page's model call was cut short (repetition loop / wall timeout).

    Carries whatever text arrived before the abort so the job can keep the
    partial result and move on to the next page instead of hanging.
    """

    def __init__(self, reason: str, partial: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.partial = partial


class _RepetitionGuard:
    """Detects degenerate repetition loops in streamed model output.

    OCR-specialized models (glm-ocr, deepseek-ocr) can lock into repeating a
    phrase, a line, or even an entire page endlessly on certain pages; with
    only the token cap in place each such page burns minutes of generation and
    fills the result with garbage. The guard keeps sliding windows of the
    stream and reports degeneration with layered checks, cheapest first:

      1. one short n-gram dominates the small window (dense short loops);
      2. the recent non-blank lines form a short p-line cycle — catches unit
         loops that never fill the small window ("```" x N, "44" x N, a table
         of contents entry re-emitted over and over);
      3. the tail of the long window is character-periodic at a lag above
         max_period (whole-page loops: the model re-types the page with a
         period of ~1000+ chars);
      4. one REP_COPY_NGRAM-char chunk was re-emitted several times with small
         gaps in the long window (drift-tolerant backstop for loops whose
         period wobbles between copies).

    Checks 1-2 run on every feed because they are cheap and must fire as early
    as possible; checks 3-4 are throttled. All checks look at the *tail* of
    the stream, so a loop that has already ended (the model recovered) does
    not abort the page and lose the good text that followed it.
    """

    def __init__(
        self,
        window: int = config.REP_WINDOW_CHARS,
        ngram: int = config.REP_NGRAM,
        min_count: int = config.REP_MIN_COUNT,
        min_coverage: float = config.REP_MIN_COVERAGE,
        max_period: int = config.REP_MAX_PERIOD,
        min_periodicity: float = config.REP_MIN_PERIODICITY,
        line_period_max: int = config.REP_LINE_PERIOD_MAX,
        line_min_repeats: int = config.REP_LINE_MIN_REPEATS,
        line_min_cycles: int = config.REP_LINE_MIN_CYCLES,
        line_consistency: float = config.REP_LINE_CONSISTENCY,
        long_window: int = config.REP_LONG_WINDOW,
        max_period_long: int = config.REP_MAX_PERIOD_LONG,
        min_periodicity_long: float = config.REP_MIN_PERIODICITY_LONG,
        copy_ngram: int = config.REP_COPY_NGRAM,
        copy_min_count: int = config.REP_COPY_MIN_COUNT,
        copy_max_spacing: int = config.REP_COPY_MAX_SPACING,
    ):
        self._window = window
        self._ngram = ngram
        self._min_count = min_count
        self._min_coverage = min_coverage
        self._max_period = max_period
        self._min_periodicity = min_periodicity
        self._line_period_max = line_period_max
        self._line_min_repeats = line_min_repeats
        self._line_min_cycles = line_min_cycles
        self._line_consistency = line_consistency
        self._long_window = long_window
        self._max_period_long = max_period_long
        self._min_periodicity_long = min_periodicity_long
        self._copy_ngram = copy_ngram
        self._copy_min_count = copy_min_count
        self._copy_max_spacing = copy_max_spacing
        self._buf = ""
        self._long_buf = ""
        # Throttling for the (costlier) periodicity scans: re-run them only
        # after this many more characters have been consumed, not on every
        # chunk.
        self._rescan_after = 384
        self._total = 0
        self._last_scan_total = 0

    def feed(self, text: str) -> bool:
        """Feed a chunk of streamed output; True once a loop is detected."""
        if not text:
            return False
        self._total += len(text)
        self._buf += text
        if len(self._buf) > self._window:
            self._buf = self._buf[-self._window:]
        self._long_buf += text
        if len(self._long_buf) > self._long_window:
            self._long_buf = self._long_buf[-self._long_window:]

        # 1) Short loop: one n-gram dominates the small window.
        buf, n = self._buf, self._ngram
        if len(buf) >= n * self._min_count:
            counts: dict[str, int] = {}
            for i in range(len(buf) - n + 1):
                g = buf[i : i + n]
                counts[g] = counts.get(g, 0) + 1
            best_gram, best = max(counts.items(), key=lambda kv: kv[1])
            if best >= self._min_count and (best * n) / len(buf) >= self._min_coverage:
                return True

        # 2) Short line-cycle loop ("```" x N, "44" x N, TOC entry x N).
        if self._line_loop():
            return True

        # 3) + 4) Long-window scans, throttled so they do not re-run on every
        # streamed chunk.
        if self._total - self._last_scan_total < self._rescan_after:
            return False
        self._last_scan_total = self._total
        if self._long_periodic_loop() or self._copy_loop():
            return True
        return False

    def _line_loop(self) -> bool:
        """True when the recent non-blank lines cycle with a period of 1..p."""
        lines = [ln.strip() for ln in self._long_buf.split("\n") if ln.strip()]
        m = len(lines)
        if m < self._line_min_repeats:
            return False
        for p in range(1, self._line_period_max + 1):
            need = max(self._line_min_repeats, self._line_min_cycles * p)
            if m < need:
                continue
            tail = lines[-need:]
            matches = sum(1 for i in range(len(tail) - p) if tail[i] == tail[i + p])
            if matches / (len(tail) - p) >= self._line_consistency:
                return True
        return False

    def _long_periodic_loop(self) -> bool:
        """True when the tail of the long window is character-periodic at a lag
        above max_period — e.g. a whole page re-typed with a ~1000-char period,
        which the small-window scan can never see two full copies of."""
        s = self._long_buf
        limit = min(self._max_period_long, len(s) // 2)
        if limit <= self._max_period:
            return False
        head_len = 32
        need = head_len * 3 // 4
        for lag in range(self._max_period + 1, limit + 1):
            start = len(s) - 2 * lag  # measure the last two full periods only
            if start < 0:
                break
            pre = 0
            for k, (a, b) in enumerate(zip(s[start : start + head_len], s[start + lag : start + lag + head_len])):
                if a == b:
                    pre += 1
                if pre + (head_len - 1 - k) < need:
                    break  # cannot reach the threshold anymore
            if pre < need:
                continue
            matches = sum(1 for a, b in zip(s[start:], s[start + lag:]) if a == b)
            if matches / lag >= self._min_periodicity_long:
                return True
        return False

    def _copy_loop(self) -> bool:
        """True when one chunk of text was re-emitted several times with small
        gaps — the drift-tolerant backstop for loops whose period wobbles
        between copies and so defeats strict character periodicity."""
        s = self._long_buf
        n = self._copy_ngram
        if len(s) < n * self._copy_min_count:
            return False
        positions: dict[str, list[int]] = {}
        for i in range(len(s) - n + 1):
            g = s[i : i + n]
            lst = positions.get(g)
            if lst is None:
                positions[g] = [i]
            else:
                lst.append(i)
        best_pos = max((pos for pos in positions.values()), key=len, default=[])
        if len(best_pos) < self._copy_min_count:
            return False
        # A phrase that merely recurs a few times across the page is fine; a
        # loop re-emits it back-to-back. Require every consecutive gap to be
        # small.
        return all(
            b - a <= self._copy_max_spacing for a, b in zip(best_pos, best_pos[1:])
        )


# Chat-template tokens that some GGUF ports leak into the output stream
# (observed with deepseek-ocr via Ollama: "< |im_end|>assistant" at the tail).
_TEMPLATE_TOKEN = r"<\|(?:im_start|im_end|system|user|assistant|end_of_text|startoftext)\|>"
_ROLE_WORD = r"(?:assistant|user|system)"


def _clean_llm_output(text: str) -> str:
    """Strip leaked chat-template seams from a page's model output.

    Conservative on purpose: template tokens are removed at the text edges and
    where they directly prefix a role word (a template seam); a bare role word
    is only stripped when it stands alone on its line, so documents that
    merely *mention* such words or tokens in their body survive intact.
    """
    t = text.strip()
    # Normalize spaced variants ("< |im_end|>") to the canonical form.
    t = re.sub(r"<\s*\|\s*(\w+)\s*\|\s*>", r"<|\1|>", t)

    # Edges and seams: peel template tokens, standalone role words, and token
    # -> role-word seams until nothing more matches (handles "assistant\n
    # < |im_start|>\n..." and "...Body\n< |im_end|>assistant" in any order).
    prev = None
    while prev != t:
        prev = t
        t = re.sub(rf"{_TEMPLATE_TOKEN}\s*(?={_ROLE_WORD}\b)", "", t)  # seam
        t = re.sub(rf"^{_TEMPLATE_TOKEN}\s*", "", t)
        t = re.sub(rf"^({_ROLE_WORD})\s*(?:\n|$)", "", t)
        t = re.sub(rf"{_TEMPLATE_TOKEN}\s*$", "", t)
        t = re.sub(rf"(?:^|\n)({_ROLE_WORD})\s*$", "", t)

    return t.strip()


def _png_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _thumbnail(png_bytes: bytes, max_side: int = config.THUMBNAIL_MAX_SIDE) -> bytes:
    """Downscale a PNG for the preview/review UI (keeps it crisp but small)."""
    try:
        img = Image.open(io.BytesIO(png_bytes))
        if max(img.size) <= max_side:
            return png_bytes
        img.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - preview is best-effort
        return png_bytes


def _is_blank(png_bytes: bytes) -> bool:
    """True when a page has (almost) no ink.

    Some vision models hang or loop forever on blank pages, so we skip them
    before ever calling the model (Local-OCR-style robustness).
    """
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("L")
        hist = img.histogram()  # 256 buckets, values 0..255
        total = sum(hist)
        ink = sum(hist[:245])  # pixels clearly darker than paper white
        return total > 0 and (ink / total) < config.BLANK_PAGE_INK_FRACTION
    except Exception:  # noqa: BLE001 - when in doubt, send it to the model
        return False


def _page_pngs_from_pdf(pdf_path, dpi: int, emit: EmitFn) -> list[bytes]:
    """Render every PDF page to PNG at the given DPI (Local-OCR behavior)."""
    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:  # noqa: BLE001
        raise OllamaEngineError(f"Could not open PDF: {exc}") from exc
    with doc:
        if doc.needs_pass:
            raise OllamaEngineError("This PDF is password-protected.")
        total = doc.page_count
        emit("status", {"phase": "Rendering pages", "page": 0, "total": total})
        pngs: list[bytes] = []
        for i in range(total):
            page = doc.load_page(i)
            zoom = dpi / 72.0
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            pngs.append(pix.tobytes("png"))
            emit("status", {"phase": "Rendering pages", "page": i + 1, "total": total})
    return pngs


def _image_png(path, emit: EmitFn) -> bytes:
    """Load a raster image and return PNG bytes (downscaled if enormous)."""
    with Image.open(path) as img:
        if max(img.size) > config.MAX_MODEL_IMAGE_SIDE:
            img.thumbnail((config.MODEL_IMAGE_CAP_SIDE, config.MODEL_IMAGE_CAP_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        return buf.getvalue()


def _collect_page_images(src_path, opts: EngineOptions, emit: EmitFn) -> tuple[list[bytes], str]:
    """Return (page_pngs, source_kind) for any supported input file."""
    kind = classify(src_path)
    if kind == "pdf":
        return _page_pngs_from_pdf(src_path, opts.dpi, emit), "pdf"
    if kind == "image":
        return [_image_png(src_path, emit)], "image"
    # docx / html: render through LibreOffice -> PDF -> pages.
    emit("status", {"phase": "Converting document to PDF (LibreOffice)"})
    pdf_path = ensure_pdf(src_path, opts.scratch)
    return _page_pngs_from_pdf(pdf_path, opts.dpi, emit), kind


def _recognize_page(client, opts: EngineOptions, png: bytes, page: int, total: int, emit: EmitFn) -> str:
    """Stream one page through the Ollama vision model; return its markdown.

    Raises PageAborted (recoverable — keep partial text, move to next page)
    when the model degenerates into a repetition loop or exceeds the per-page
    wall-clock budget, and OllamaEngineError for job-level failures such as an
    unreachable server.
    """
    if opts.is_cancelled and opts.is_cancelled():
        raise JobCancelled("Job cancelled by user.")
    messages = [
        {"role": "system", "content": config.SYSTEM_PROMPT},
        {
            "role": "user",
            "content": config.USER_PROMPT,
            "images": [_png_b64(png)],
        },
    ]
    chunks: list[str] = []
    guard = _RepetitionGuard()
    deadline = time.monotonic() + config.OLLAMA_PAGE_WALL_TIMEOUT
    abort_reason: str | None = None
    stream = client.chat(
        model=opts.model,
        messages=messages,
        options={"num_predict": config.MAX_PAGE_TOKENS},
        stream=True,
    )
    try:
        for part in stream:
            if time.monotonic() > deadline:
                # The idle timeout only catches full silence; this bounds a
                # page that streams slowly but never finishes.
                abort_reason = f"wall-clock limit of {config.OLLAMA_PAGE_WALL_TIMEOUT:.0f}s reached"
                break
            if isinstance(part, dict):
                msg = part.get("message") or {}
                delta = msg.get("content") or ""
            else:  # pydantic ChatResponse (ollama python >= 0.5)
                msg = getattr(part, "message", None)
                delta = getattr(msg, "content", None) or ""
            if delta:
                chunks.append(delta)
                emit("stream_chunk", {"page": page, "text": delta})
                if guard.feed(delta):
                    abort_reason = "repetitive model output (repetition loop detected)"
                    break
    except Exception as exc:  # noqa: BLE001 - surface transport errors clearly
        if opts.is_cancelled and opts.is_cancelled():
            # The cancel watchdog closed the client to unblock this read.
            raise JobCancelled("Job cancelled by user.") from exc
        msg = str(exc).strip() or exc.__class__.__name__
        if "refused" in msg.lower() or "connect" in msg.lower() or "unreachable" in msg.lower():
            raise OllamaEngineError(
                f"Cannot reach the Ollama server at {opts.ollama_url}. "
                "Check the URL in the sidebar and that the server is running."
            ) from exc
        raise OllamaEngineError(f"Ollama request failed: {msg}") from exc
    finally:
        # Close the stream promptly on abort so the remote model stops
        # generating (frees its GPU/CPU for the next page).
        try:
            stream.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
    if abort_reason is not None:
        raise PageAborted(abort_reason, partial="".join(chunks))
    text = _clean_llm_output("".join(chunks))
    if not text:
        if page == 1:
            # Failing fast on the first page gives a clear diagnostic instead
            # of N identical warnings (usually means the model is not
            # vision-capable).
            raise OllamaEngineError(
                f"Page {page}/{total}: the model returned no text. Check that "
                f"'{opts.model}' is a vision-capable model on {opts.ollama_url}."
            )
        raise PageAborted("model returned no text")
    return text


def run(src_path, opts: EngineOptions, emit: EmitFn) -> EngineResult:
    """Run Ollama OCR over the source file. See module docstring."""
    if not opts.model:
        raise OllamaEngineError("No Ollama model selected.")

    import ollama

    try:
        client = ollama.Client(host=opts.ollama_url, timeout=config.OLLAMA_STREAM_IDLE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        raise OllamaEngineError(
            f"Could not create an Ollama client for {opts.ollama_url}: {exc}"
        ) from exc

    # Cancel watchdog: closing the underlying httpx client unblocks a
    # streaming read that is stuck mid-page (e.g. the model hanging).
    if opts.cancel_event is not None:
        def _watchdog() -> None:
            if opts.cancel_event.wait():
                try:
                    client.close()
                except Exception:  # noqa: BLE001 - best effort
                    pass

        threading.Thread(target=_watchdog, name=f"cancel-{src_path.name}", daemon=True).start()

    page_pngs, kind = _collect_page_images(src_path, opts, emit)
    total = len(page_pngs)
    if total == 0:
        raise OllamaEngineError("The document has no pages to process.")

    result = EngineResult(engine="ollama")
    started = time.time()

    for idx, png in enumerate(page_pngs, start=1):
        if opts.is_cancelled and opts.is_cancelled():
            raise JobCancelled("Job cancelled by user.")
        emit(
            "status",
            {"phase": f"OCR page {idx}/{total}", "page": idx, "total": total},
        )
        thumb = _thumbnail(png)
        emit("page_image", {"page": idx, "total": total, "image_b64": _png_b64(thumb)})

        if _is_blank(png):
            # Skip the model entirely — blank pages are a known hang trigger.
            result.pages.append(PageResult(page=idx, text="", image_b64=_png_b64(thumb)))
            emit("page_text", {"page": idx, "total": total, "text": ""})
            emit("log", {"message": f"Page {idx}/{total} appears blank — skipped"})
            continue

        aborted = False
        abort_reason = ""
        try:
            text = _recognize_page(client, opts, png, idx, total, emit)
        except PageAborted as pa:
            # A degenerate page must not stall the job: keep whatever text
            # arrived (may be empty), warn, and move on to the next page.
            # (Capture the reason now: Python deletes `pa` when the except
            # block exits.)
            aborted = True
            abort_reason = pa.reason
            text = _clean_llm_output(pa.partial)
            result.warnings.append(
                f"Page {idx}/{total} aborted: {abort_reason}"
                + (f" — kept {len(text)} chars of partial output" if text else "")
            )

        emit("page_text", {"page": idx, "total": total, "text": text})
        result.pages.append(PageResult(page=idx, text=text, image_b64=_png_b64(thumb)))
        if aborted:
            emit(
                "log",
                {"message": f"Page {idx}/{total} aborted ({abort_reason}) — continuing to next page"},
            )
        else:
            emit("log", {"message": f"Page {idx}/{total} done ({len(text)} chars)"})

    from ..outputs import md_to_html, md_to_text

    result.markdown = "\n\n".join(p.text for p in result.pages)
    result.html = md_to_html(result.markdown, title=opts.source_desc or "Document")
    result.text = md_to_text(result.markdown)
    result.json_doc = {
        "engine": "ollama",
        "source": opts.source_desc,
        "model": opts.model,
        "dpi": opts.dpi if kind == "pdf" else None,
        "source_kind": kind,
        "page_count": total,
        "processing_time_s": round(time.time() - started, 2),
        "pages": [{"page": p.page, "text": p.text} for p in result.pages],
    }
    if result.warnings:
        emit(
            "log",
            {"message": f"Finished with {len(result.warnings)} warning(s) — see job log"},
        )
    return result

"""Shared engine contracts.

An engine turns one local source file into an EngineResult while reporting
progress through `emit`. Events use the same names the SSE endpoint sends:

  status        {"status", "phase", "page"?, "total"?}
  log           {"message"}
  page_image    {"page", "total", "image_b64"}      (PNG thumbnail)
  stream_chunk  {"page", "text"}                    (live OCR text delta)
  page_text     {"page", "total", "text"}           (final page markdown)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class EngineOptions:
    """Everything an engine needs beyond the source file."""

    scratch: Path
    ollama_url: str = ""
    model: str = ""
    dpi: int = 150
    source_desc: str = ""  # original name / URL, for metadata
    # Cancellation hooks (set by main.py from the job's cancel flag).
    is_cancelled: Callable[[], bool] | None = None
    cancel_event: threading.Event | None = None


EmitFn = Callable[[str, dict[str, Any]], None]


@dataclass
class PageResult:
    page: int
    text: str = ""  # markdown
    image_b64: str = ""  # PNG thumbnail for the review UI


@dataclass
class EngineResult:
    engine: str
    pages: list[PageResult] = field(default_factory=list)
    markdown: str = ""
    html: str = ""
    text: str = ""
    json_doc: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_outputs(self) -> dict[str, str]:
        """Final rendered outputs keyed by format name."""
        return {
            "markdown": self.markdown,
            "html": self.html,
            "json": _dumps_json(self.json_doc),
            "text": self.text,
        }


def _dumps_json(doc: dict[str, Any]) -> str:
    import json

    return json.dumps(doc, indent=2, ensure_ascii=False)

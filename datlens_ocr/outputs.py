"""Output rendering: Markdown / HTML / JSON / Text from an EngineResult."""

from __future__ import annotations

import html as _html
import re

import markdown as md_lib

from .engines.base import EngineResult

_FORMATS = ("markdown", "html", "json", "text")

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      "Helvetica Neue", Arial, sans-serif;
    line-height: 1.6; max-width: 52rem; margin: 0 auto; padding: 2rem 1.25rem;
    color: #1f2430; background: #fff;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e6e9f2; background: #12141c; }}
    a {{ color: #8b93ff; }}
    pre, blockquote, table, th, td {{ border-color: #3a3f52 !important; }}
  }}
  h1, h2, h3 {{ line-height: 1.25; margin-top: 1.6em; }}
  img {{ max-width: 100%; height: auto; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  th, td {{ border: 1px solid #d4d7e2; padding: .45em .65em; text-align: left; }}
  th {{ background: rgba(120, 130, 255, .08); }}
  pre {{
    background: rgba(120, 130, 255, .07); padding: .9em 1em; border-radius: 8px;
    overflow-x: auto; font-size: .9em;
  }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  blockquote {{ border-left: 3px solid #8b93ff; margin-left: 0; padding-left: 1em; opacity: .85; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def md_to_html(markdown_text: str, title: str = "Document") -> str:
    """Render markdown to a standalone, theme-aware HTML document."""
    body = md_lib.markdown(
        markdown_text or "",
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
    )
    return _HTML_TEMPLATE.format(title=_html.escape(title), body=body)


def md_to_text(markdown_text: str) -> str:
    """Best-effort plain-text rendering of markdown (no external deps)."""
    text = markdown_text or ""
    # Fenced code blocks: keep the code, drop the fences.
    text = re.sub(r"```[a-zA-Z0-9_-]*\n?", "", text)
    # Inline code backticks.
    text = text.replace("`", "")
    # Images ![alt](url) -> alt ; links [text](url) -> text
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # Headings.
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    # Bold / italic markers.
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"(\*|_)([^*_]+)\1", r"\2", text)
    # Strikethrough.
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    # Horizontal rules.
    text = re.sub(r"^\s*([-*_])\s*(\1\s*){2,}$", "", text, flags=re.M)
    # Table pipes -> tabs (rows stay on their own lines).
    text = re.sub(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$", "", text, flags=re.M)
    text = text.replace("|", "  ")
    # Blockquote markers.
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.M)
    # List bullets -> "-".
    text = re.sub(r"^\s*[*+]\s+", "- ", text, flags=re.M)
    # Collapse 3+ blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def available_formats() -> tuple[str, ...]:
    return _FORMATS


def render(result: EngineResult, fmt: str, title: str = "Document") -> tuple[str, str]:
    """Return (content, media_type) for one output format."""
    if fmt == "markdown":
        return result.markdown, "text/markdown; charset=utf-8"
    if fmt == "html":
        return md_to_html(result.markdown, title), "text/html; charset=utf-8"
    if fmt == "json":
        import json

        return json.dumps(result.json_doc, indent=2, ensure_ascii=False), (
            "application/json; charset=utf-8"
        )
    if fmt == "text":
        return result.text, "text/plain; charset=utf-8"
    raise ValueError(f"Unknown format {fmt!r}; expected one of {_FORMATS}")

"""Load and extend ``prompts/categories.md`` (committed taxonomy for ledger LLM)."""

from __future__ import annotations

import re
from pathlib import Path

_SAMPLES_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SAMPLES_DIR.parent
_CATEGORIES_PATH = _REPO_ROOT / "prompts" / "categories.md"

_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


def categories_md_path() -> Path:
    return _CATEGORIES_PATH


def load_category_lines() -> list[str]:
    if not _CATEGORIES_PATH.is_file():
        return []
    lines: list[str] = []
    for raw in _CATEGORIES_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _BULLET_RE.match(raw)
        if m:
            s = m.group(1).strip()
            if s and not s.startswith("#"):
                lines.append(s)
    return lines


def category_match_set(lines: list[str] | None = None) -> set[str]:
    """Lowercase set for fuzzy “already in list” checks."""
    src = lines if lines is not None else load_category_lines()
    return {x.strip().lower() for x in src if x.strip()}


def _sanitize_new_category(name: str) -> str:
    s = " ".join((name or "").strip().split())
    if not s or len(s) > 80:
        return ""
    if not re.match(r"^[\w\s&(),./+\-':]+$", s, re.UNICODE):
        return ""
    return s[:80]


def append_category_if_new(name: str) -> tuple[bool, str]:
    """Append ``- {name}`` to categories.md if new. Returns (did_append, normalized)."""
    clean = _sanitize_new_category(name)
    if not clean:
        return False, ""
    existing = category_match_set()
    if clean.lower() in existing:
        return False, clean
    _CATEGORIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"- {clean}\n"
    if _CATEGORIES_PATH.is_file():
        body = _CATEGORIES_PATH.read_text(encoding="utf-8")
        if not body.endswith("\n"):
            body += "\n"
        body += line
        _CATEGORIES_PATH.write_text(body, encoding="utf-8")
    else:
        _CATEGORIES_PATH.write_text("# Personal spending categories\n\n" + line, encoding="utf-8")
    return True, clean


def format_categories_for_prompt(max_lines: int = 120) -> str:
    lines = load_category_lines()
    if not lines:
        return "(No categories.md — add prompts/categories.md)"
    lines = lines[:max_lines]
    return "\n".join(f"- {x}" for x in lines)

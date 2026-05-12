"""Load private spend context from prompts/financial-spend-knowledge.md (gitignored).

Copy ``prompts/financial-spend-knowledge.example.md`` to that name locally.
"""

from __future__ import annotations

from pathlib import Path

_SAMPLES_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SAMPLES_DIR.parent
_KB_PATH = _REPO_ROOT / "prompts" / "financial-spend-knowledge.md"


_MAX_KB_CHARS = 12_000


def load_spend_knowledge_for_prompt() -> str:
    """Return text to append to the system prompt, or empty if missing/blank."""
    if not _KB_PATH.is_file():
        return ""
    raw = _KB_PATH.read_text(encoding="utf-8").strip()
    if not raw:
        return ""
    if len(raw) > _MAX_KB_CHARS:
        raw = raw[: _MAX_KB_CHARS - 1].rstrip() + "…"
    return (
        "---\n\n"
        "## User spend knowledge (use when matching payees or amounts)\n\n"
        f"{raw}\n"
    )


def spend_knowledge_path() -> Path:
    return _KB_PATH

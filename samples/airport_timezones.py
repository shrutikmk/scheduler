"""Lookup IATA → IANA timezone from ``prompts/airport-timezones.csv`` (flight calendar hints)."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_IATA_WORD = re.compile(r"\b([A-Za-z]{3})\b")


@dataclass(frozen=True)
class AirportTzTurn:
    """IATA codes from the user turn that exist in the CSV, plus the formatted appendix."""

    matched_codes: tuple[str, ...]
    appendix: str


@lru_cache(maxsize=1)
def load_airport_rows(csv_path: str) -> dict[str, dict[str, str]]:
    """Map uppercased IATA → row dict (iata, name, city, country, timezone, …)."""
    out: dict[str, dict[str, str]] = {}
    path = Path(csv_path)
    if not path.is_file():
        return out
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = (row.get("iata") or "").strip().upper()
            if len(code) != 3:
                continue
            out[code] = {k: (v or "").strip() for k, v in row.items()}
    return out


def iata_codes_in_text(text: str, *, valid: dict[str, dict[str, str]]) -> list[str]:
    """IATA codes present in ``text`` that exist in the catalog (order first-seen)."""
    if not text or not valid:
        return []
    upper = text.upper()
    seen: set[str] = set()
    ordered: list[str] = []
    for m in _IATA_WORD.finditer(upper):
        code = m.group(1)
        if code not in valid or code in seen:
            continue
        seen.add(code)
        ordered.append(code)
    return ordered


def format_airport_tz_appendix(
    codes: list[str],
    *,
    catalog: dict[str, dict[str, str]],
    max_lines: int = 12,
) -> str:
    """Compact block for the LLM: IATA, IANA tz, name/city."""
    if not codes:
        return ""
    lines: list[str] = [
        "\n\n---\n[Airport timezones — from prompts/airport-timezones.csv]\n",
        "Use these **IANA** values for `timeZone` on flight-related events at each airport.\n",
    ]
    for code in codes[:max_lines]:
        row = catalog.get(code)
        if not row:
            continue
        tz = row.get("timezone") or "?"
        name = row.get("name") or code
        city = row.get("city") or ""
        country = row.get("country") or ""
        place = ", ".join(p for p in (city, country) if p)
        suffix = f" ({place})" if place else ""
        lines.append(f"- **{code}** → `{tz}` — {name}{suffix}\n")
    if len(codes) > max_lines:
        n = len(codes) - max_lines
        lines.append(f"- … ({n} more code(s) omitted; narrow or split request)\n")
    return "".join(lines)


def airport_tz_turn(
    user_text: str,
    *,
    csv_path: Path,
    max_airports: int = 12,
) -> AirportTzTurn:
    """Resolve IATA codes in ``user_text`` against the CSV; appendix empty if missing/no hits."""
    path = csv_path.expanduser().resolve()
    catalog = load_airport_rows(str(path))
    codes = iata_codes_in_text(user_text, valid=catalog)
    appendix = format_airport_tz_appendix(
        codes,
        catalog=catalog,
        max_lines=max_airports,
    )
    return AirportTzTurn(tuple(codes), appendix)


def airport_hints_for_user_message(
    user_text: str,
    *,
    csv_path: Path,
    max_airports: int = 12,
) -> str:
    """Back-compat: appendix string only."""
    return airport_tz_turn(
        user_text,
        csv_path=csv_path,
        max_airports=max_airports,
    ).appendix

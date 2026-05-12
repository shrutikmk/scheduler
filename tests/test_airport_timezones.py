"""Tests for ``airport_timezones`` CSV lookup."""

from __future__ import annotations

from pathlib import Path

import pytest
from airport_timezones import (
    airport_hints_for_user_message,
    airport_tz_turn,
    format_airport_tz_appendix,
    iata_codes_in_text,
    load_airport_rows,
)


@pytest.fixture(autouse=True)
def _fresh_airport_cache() -> None:
    load_airport_rows.cache_clear()
    yield
    load_airport_rows.cache_clear()


def _mini_csv(tmp: Path) -> Path:
    p = tmp / "airport-timezones.csv"
    p.write_text(
        "iata,icao,name,city,country,timezone,utc_offset_hours,dst\n"
        "JFK,KJFK,John F. Kennedy International Airport,New York,"
        "United States,America/New_York,-5,A\n"
        "LHR,EGLL,London Heathrow Airport,London,"
        "United Kingdom,Europe/London,0,E\n",
        encoding="utf-8",
    )
    return p


def test_iata_codes_in_text_order_unique(tmp_path: Path) -> None:
    cat = load_airport_rows(str(_mini_csv(tmp_path)))
    got = iata_codes_in_text("JFK-JFK nonsense LHR jfk repeat", valid=cat)
    assert got == ["JFK", "LHR"]


def test_unknown_three_letter_words_ignored(tmp_path: Path) -> None:
    cat = load_airport_rows(str(_mini_csv(tmp_path)))
    assert iata_codes_in_text("THE CAT SAT", valid=cat) == []


def test_airport_hints_for_user_message(tmp_path: Path) -> None:
    csv_p = _mini_csv(tmp_path)
    block = airport_hints_for_user_message(
        "Book JFK→LHR",
        csv_path=csv_p,
    )
    assert "America/New_York" in block and "Europe/London" in block
    assert "JFK" in block and "LHR" in block


def test_format_max_lines(tmp_path: Path) -> None:
    cat = load_airport_rows(str(_mini_csv(tmp_path)))
    long = format_airport_tz_appendix(
        ["JFK", "LHR", "JFK"],
        catalog=cat,
        max_lines=1,
    )
    assert "omitted" in long


def test_airport_tz_turn_matches_hints_appendix(tmp_path: Path) -> None:
    csv_p = _mini_csv(tmp_path)
    t = airport_tz_turn("JFK to LHR", csv_path=csv_p)
    assert t.matched_codes == ("JFK", "LHR")
    assert t.appendix == airport_hints_for_user_message("JFK to LHR", csv_path=csv_p)

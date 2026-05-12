"""Tests for prompts/categories.md helpers."""

from __future__ import annotations

from pathlib import Path

import spend_categories as sc


def test_append_category_if_new_writes_once(tmp_path: Path, monkeypatch) -> None:
    cat_file = tmp_path / "categories.md"
    cat_file.write_text("# T\n\n- Alpha\n", encoding="utf-8")
    monkeypatch.setattr(sc, "_CATEGORIES_PATH", cat_file)
    did, norm = sc.append_category_if_new("Beta Test")
    assert did is True
    assert norm == "Beta Test"
    did2, _ = sc.append_category_if_new("Beta Test")
    assert did2 is False
    body = cat_file.read_text(encoding="utf-8")
    assert body.count("Beta Test") == 1


def test_load_category_lines_reads_bullets(tmp_path: Path, monkeypatch) -> None:
    cat_file = tmp_path / "categories.md"
    cat_file.write_text("- One\n* Two\n\nignored\n", encoding="utf-8")
    monkeypatch.setattr(sc, "_CATEGORIES_PATH", cat_file)
    assert sc.load_category_lines() == ["One", "Two"]

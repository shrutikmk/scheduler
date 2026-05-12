"""Tests for LLM title/category JSON parsing used by ledger titling."""

from __future__ import annotations

from expense_display_titles import _parse_title_category_map


def test_parse_fp_to_object_dict():
    fps = {"a1", "b2"}
    text = (
        '{"a1": {"title": "Coffee shop", "category": "Food"}, '
        '"b2": {"title": "Gas", "category": "Auto"}}'
    )
    got = _parse_title_category_map(text, fps)
    assert set(got) == fps
    assert got["a1"]["title"] == "Coffee shop"
    assert got["a1"]["category"] == "Food"
    assert got["b2"]["is_new_category"] is False


def test_parse_array_of_objects():
    fps = {"fp_x", "fp_y"}
    text = (
        '[{"fp":"fp_x","title":"Rent payment","category":"Housing","is_new_category":false},'
        '{"fingerprint":"fp_y","title":"Netflix","category":"Subscriptions"}]'
    )
    got = _parse_title_category_map(text, fps)
    assert got["fp_x"]["category"] == "Housing"
    assert got["fp_y"]["title"] == "Netflix"
    assert got["fp_x"]["is_new_category"] is False


def test_parse_wrapper_results():
    fps = {"only"}
    text = '{"results": [{"id": "only", "title": "Grocery", "category": "Food"}]}'
    got = _parse_title_category_map(text, fps)
    assert got["only"]["title"] == "Grocery"


def test_parse_markdown_fence():
    fps = {"k"}
    text = "```json\n{\"k\": {\"display_title\": \"Payroll\", \"spend_category\": \"Income\"}}\n```"
    got = _parse_title_category_map(text, fps)
    assert got["k"]["title"] == "Payroll"
    assert got["k"]["category"] == "Income"


def test_parse_prose_then_json():
    fps = {"z9"}
    text = 'Here is the mapping:\n\n{"z9": {"title": "Whole Foods", "category": "Groceries"}}'
    got = _parse_title_category_map(text, fps)
    assert got["z9"]["category"] == "Groceries"


def test_ignores_unknown_fp():
    fps = {"want"}
    text = (
        '{"want": {"title": "Ok", "category": "Misc"}, '
        '"other": {"title": "No", "category": "X"}}'
    )
    got = _parse_title_category_map(text, fps)
    assert set(got.keys()) == {"want"}

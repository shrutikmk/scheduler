"""Tests for deduplicated financial ledger."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from bank_statement_csv import (
    StatementRow,
    clip_statement_rows,
    load_statement_rows_from_path,
    summarize_rows,
)
from financial_ledger_store import (
    build_insights_cache_key,
    connect,
    contributing_source_files,
    default_ledger_path,
    enrich_summary_with_ledger,
    enrich_summary_with_ledger_titles,
    fetch_statement_rows,
    ingest_bank_csv,
    init_schema,
    insights_cache_get,
    insights_cache_store,
    reindex_all_csvs,
    transaction_fingerprint,
    upsert_ledger_title_category,
)

MINI_CSV = """Description,,Summary Amt.
Beginning balance as of 01/01/2026,,"1,000.00"

Date,Description,Amount,Running Bal.
01/02/2026,"COFFEE SHOP TX","-5.50","994.50"
"""


def test_transaction_fingerprint_case_insensitive() -> None:
    r1 = StatementRow(date(2026, 1, 2), "COFFEE SHOP TX", -5.5, None)
    r2 = StatementRow(date(2026, 1, 2), "coffee  shop   tx", -5.5, 100.0)
    assert transaction_fingerprint(r1) == transaction_fingerprint(r2)


def test_transaction_fingerprint_differs_on_amount() -> None:
    r1 = StatementRow(date(2026, 1, 2), "COFFEE SHOP TX", -5.5, None)
    r2 = StatementRow(date(2026, 1, 2), "COFFEE SHOP TX", -5.51, None)
    assert transaction_fingerprint(r1) != transaction_fingerprint(r2)


def test_ingest_dedupes_across_two_files(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite"
    a = tmp_path / "week_a.csv"
    b = tmp_path / "week_b.csv"
    a.write_text(MINI_CSV, encoding="utf-8")
    b.write_text(MINI_CSV, encoding="utf-8")

    conn = connect(db)
    init_schema(conn)
    try:
        ingest_bank_csv(conn, a)
        ingest_bank_csv(conn, b)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM transactions")
        assert int(cur.fetchone()[0]) == 1
        cur.execute("SELECT sources_json FROM transactions LIMIT 1")
        raw = cur.fetchone()[0]
        assert "week_a.csv" in raw and "week_b.csv" in raw
    finally:
        conn.close()


def test_reindex_builds_from_directory(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite"
    fin = tmp_path / "fin"
    fin.mkdir()
    (fin / "one.csv").write_text(MINI_CSV, encoding="utf-8")

    conn = connect(db)
    init_schema(conn)
    try:
        out = reindex_all_csvs(conn, fin)
        assert out["files"] == 1
        rows = fetch_statement_rows(conn, None, None)
        assert len(rows) == 1
        assert rows[0].row_date == date(2026, 1, 2)
    finally:
        conn.close()


def test_fetch_and_summarize_aggregate(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite"
    fin = tmp_path / "fin"
    fin.mkdir()
    (fin / "one.csv").write_text(MINI_CSV, encoding="utf-8")

    conn = connect(db)
    init_schema(conn)
    try:
        reindex_all_csvs(conn, fin)
        rows = fetch_statement_rows(conn, date(2026, 1, 1), date(2026, 1, 3))
        assert len(rows) == 1
        names = contributing_source_files(conn, date(2026, 1, 1), date(2026, 1, 3))
        assert names == ["one.csv"]
        s = summarize_rows(
            rows,
            filename="aggregate",
            preamble={},
            include_running_balance=False,
        )
        assert s["series_balance"] == []
        assert s["totals"]["debits"] == pytest.approx(-5.5)
        assert s["kpi"]["expense_total"] == pytest.approx(5.5)
        assert len(s["series_cash_flow_daily"]) >= 1
        assert len(s["series_cumulative_net"]) == 1
    finally:
        conn.close()


def test_clip_reduces_transaction_count(tmp_path: Path) -> None:
    f = tmp_path / "x.csv"
    f.write_text(MINI_CSV, encoding="utf-8")
    preamble, rows = load_statement_rows_from_path(f)
    clipped = clip_statement_rows(rows, date(2026, 1, 3), date(2026, 1, 4))
    assert len(clipped) == 0
    s = summarize_rows(clipped, filename="x.csv", preamble=preamble, include_running_balance=True)
    assert s["transaction_count"] == 0


def test_debit_ingest_sets_retitle_pending(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite"
    f = tmp_path / "one.csv"
    f.write_text(MINI_CSV, encoding="utf-8")
    conn = connect(db)
    init_schema(conn)
    try:
        ingest_bank_csv(conn, f)
        cur = conn.cursor()
        cur.execute(
            "SELECT retitle_pending FROM transactions "
            "WHERE amount IS NOT NULL AND amount < 0 LIMIT 1"
        )
        assert int(cur.fetchone()[0]) == 1
    finally:
        conn.close()


def test_enrich_summary_sets_display_label_for_outflows(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite"
    fin = tmp_path / "fin"
    fin.mkdir()
    (fin / "one.csv").write_text(MINI_CSV, encoding="utf-8")

    conn = connect(db)
    init_schema(conn)
    try:
        reindex_all_csvs(conn, fin)
        cur = conn.cursor()
        cur.execute("SELECT fingerprint FROM transactions LIMIT 1")
        fp = str(cur.fetchone()[0])
        upsert_ledger_title_category(conn, {fp: ("Coffee shop", "Coffee & cafes")})
        rows = fetch_statement_rows(conn, None, None)
        s = summarize_rows(rows, filename="aggregate", preamble={}, include_running_balance=False)
        enrich_summary_with_ledger(conn, s)
    finally:
        conn.close()

    txs = s.get("transactions") or []
    assert any(t.get("display_title") == "Coffee shop" for t in txs)
    assert s.get("display_titles_pending") is False
    outs = s.get("top_outflows") or []
    assert outs and outs[0].get("display_label") == "Coffee shop"


def test_enrich_rebuilds_spend_mix_from_ledger_category(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite"
    fin = tmp_path / "fin"
    fin.mkdir()
    (fin / "one.csv").write_text(MINI_CSV, encoding="utf-8")

    conn = connect(db)
    init_schema(conn)
    try:
        reindex_all_csvs(conn, fin)
        cur = conn.cursor()
        cur.execute("SELECT fingerprint FROM transactions LIMIT 1")
        fp = str(cur.fetchone()[0])
        upsert_ledger_title_category(conn, {fp: ("Coffee shop", "Coffee & cafes")})
        rows = fetch_statement_rows(conn, None, None)
        s = summarize_rows(rows, filename="aggregate", preamble={}, include_running_balance=False)
        enrich_summary_with_ledger(conn, s)
        cats = [r.get("name") for r in (s.get("spending_by_category") or [])]
        assert "Coffee & cafes" in cats
        assert s.get("mix_chart_labels_pending") is False
        rp = s.get("retitle_progress") or {}
        assert rp.get("pending") == 0
        assert rp.get("eligible") == 1
    finally:
        conn.close()


def test_enrich_summary_display_titles_pending_without_upsert(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite"
    fin = tmp_path / "fin"
    fin.mkdir()
    (fin / "one.csv").write_text(MINI_CSV, encoding="utf-8")

    conn = connect(db)
    init_schema(conn)
    try:
        reindex_all_csvs(conn, fin)
        rows = fetch_statement_rows(conn, None, None)
        s = summarize_rows(rows, filename="aggregate", preamble={}, include_running_balance=False)
        enrich_summary_with_ledger_titles(conn, s)
    finally:
        conn.close()

    assert s.get("display_titles_pending") is True
    outs = s.get("top_outflows") or []
    assert outs and outs[0].get("display_title_loading") is True


def test_default_ledger_path_under_financial_data() -> None:
    assert default_ledger_path().name == "ledger.sqlite"
    assert default_ledger_path().parent.name == "financial-data"


def test_build_insights_cache_key_stable_for_reordered_fps() -> None:
    k1 = build_insights_cache_key(
        view_mode="aggregate",
        snapshot_file=None,
        date_start="2026-01-01",
        date_end="2026-01-31",
        exclude_sav_brk=False,
        transaction_fingerprints=["bbb", "aaa"],
        insights_context_hash="",
    )
    k2 = build_insights_cache_key(
        view_mode="aggregate",
        snapshot_file=None,
        date_start="2026-01-01",
        date_end="2026-01-31",
        exclude_sav_brk=False,
        transaction_fingerprints=["aaa", "bbb"],
        insights_context_hash="",
    )
    assert k1 == k2
    assert len(k1) == 64


def test_build_insights_cache_key_differs_when_insights_context_hash_differs() -> None:
    base_kw = dict(
        view_mode="aggregate",
        snapshot_file=None,
        date_start="2026-01-01",
        date_end="2026-01-31",
        exclude_sav_brk=False,
        transaction_fingerprints=["fp1"],
    )
    a = build_insights_cache_key(**base_kw, insights_context_hash="aaa")
    b = build_insights_cache_key(**base_kw, insights_context_hash="bbb")
    assert a != b


def test_insights_cache_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite"
    conn = connect(db)
    init_schema(conn)
    try:
        k = build_insights_cache_key(
            view_mode="aggregate",
            snapshot_file=None,
            date_start=None,
            date_end=None,
            exclude_sav_brk=True,
            transaction_fingerprints=["fp1"],
            insights_context_hash="",
        )
        iid = insights_cache_store(
            conn,
            cache_key=k,
            markdown="# Hello",
            insights_model="Qwen/Test",
            view_mode="aggregate",
            snapshot_file=None,
            date_start=None,
            date_end=None,
            exclude_sav_brk=True,
            transaction_count=1,
        )
        assert iid >= 1
        hit = insights_cache_get(conn, k)
        assert hit is not None
        assert hit["id"] == iid
        assert hit["markdown"] == "# Hello"
        assert hit["insights_model"] == "Qwen/Test"
    finally:
        conn.close()

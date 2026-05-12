"""Tests for ``bank_statement_csv`` — synthetic data only."""

from __future__ import annotations

from pathlib import Path

import pytest
from bank_statement_csv import (
    is_sav_brk_transfer_debit,
    llm_digest_payload,
    load_statement_rows_from_path,
    parse_bank_statement_csv,
    parse_money_cell,
    summarize_rows,
    summarize_rows_for_insights,
    transactions_matching_spend_mix_row,
)


def test_parse_money_cell() -> None:
    assert parse_money_cell("-1,014.04") == -1014.04
    assert parse_money_cell("4,412.82") == 4412.82
    assert parse_money_cell("") is None
    assert parse_money_cell(None) is None


MINI_CSV = """Description,,Summary Amt.
Beginning balance as of 01/01/2026,,"1,000.00"
Total credits,,"100.00"
Total debits,,"-5.50"

Date,Description,Amount,Running Bal.
01/01/2026,Beginning balance as of 01/01/2026,,"1,000.00"
01/02/2026,"COFFEE SHOP TX","-5.50","994.50"
01/03/2026,"EMPLOYER PAYROLL","100.00","1,094.50"
"""


def test_is_sav_brk_transfer_debit() -> None:
    assert is_sav_brk_transfer_debit("ONLINE TRANSFER TO BRK", -200.0) is True
    assert is_sav_brk_transfer_debit("xfer to sav ABC123", -50.0) is True
    assert is_sav_brk_transfer_debit("COFFEE SHOP TX", -5.5) is False
    assert is_sav_brk_transfer_debit("transfer to BRK", 100.0) is False


def test_transactions_matching_spend_mix_row_sub(tmp_path: Path) -> None:
    p = tmp_path / "t.csv"
    p.write_text(MINI_CSV, encoding="utf-8")
    preamble, rows = load_statement_rows_from_path(p)
    s = summarize_rows(rows, filename="t.csv", preamble=preamble, include_running_balance=True)
    mix = s.get("spending_mix_chart") or []
    sub = next(x for x in mix if x.get("drill") == "sub")
    hits = transactions_matching_spend_mix_row(s["transactions"], sub)
    assert len(hits) == 1
    assert "COFFEE" in hits[0].get("description", "").upper()


def test_summarize_rows_for_insights_excludes_sav_brk(tmp_path: Path) -> None:
    extended = (
        MINI_CSV.rstrip() + '\n01/04/2026,"ONLINE TRANSFER TO BRK","-200.00","894.50"\n'
    )
    p = tmp_path / "brk.csv"
    p.write_text(extended, encoding="utf-8")
    preamble, rows = load_statement_rows_from_path(p)
    merged = summarize_rows_for_insights(
        rows,
        filename="brk.csv",
        preamble=preamble,
        include_running_balance=True,
        exclude_sav_brk_transfer_debits=True,
    )
    assert merged.get("_insights_exclude_sav_brk") is True
    assert merged["totals"]["debits"] == pytest.approx(-205.5)
    assert merged["kpi"]["expense_total"] == pytest.approx(5.5)
    assert merged["kpi"]["full_expense_total"] == pytest.approx(205.5)
    assert merged["series_cumulative_net"][-1]["cumulative_net"] == pytest.approx(94.5)
    assert not any(x.get("date") == "2026-01-04" for x in merged["series_daily_net"])
    digest = llm_digest_payload(merged)
    assert "INSIGHTS LENS" in digest
    assert "full-ledger money out" in digest


def test_spending_mix_chart_splits_transfers(tmp_path: Path) -> None:
    csv = MINI_CSV.rstrip() + '\n01/04/2026,"ONLINE TRANSFER TO BRK",-50.00,"1044.50"\n'
    p = tmp_path / "t.csv"
    p.write_text(csv, encoding="utf-8")
    out = parse_bank_statement_csv(p)
    smx = out.get("spending_mix_chart") or []
    labels = [x.get("label") for x in smx]
    assert any("Transfers ·" in str(lab) for lab in labels)


def test_parse_bank_statement_preamble_and_transactions(tmp_path: Path) -> None:
    p = tmp_path / "stmt.csv"
    p.write_text(MINI_CSV, encoding="utf-8")
    out = parse_bank_statement_csv(p)
    assert out["transaction_count"] == 3
    assert out["totals"]["credits"] == 100.0
    assert pytest.approx(out["totals"]["debits"]) == -5.5
    assert "Beginning balance as of 01/01/2026" in (out.get("preamble") or {})
    series = out["series_balance"]
    assert any(pt["balance"] == 1094.50 for pt in series)
    cum = out["series_cumulative_net"]
    assert cum[-1]["cumulative_net"] == pytest.approx(out["totals"]["net_flow"])
    assert out["kpi"]["income_total"] == 100.0
    assert out["kpi"]["expense_total"] == pytest.approx(5.5)
    assert out["kpi"]["net_flow"] == pytest.approx(94.5)
    cf_jan2 = next(x for x in out["series_cash_flow_daily"] if x["date"] == "2026-01-02")
    assert cf_jan2["outflow"] == pytest.approx(5.5)
    assert cf_jan2["inflow"] == pytest.approx(0.0)
    assert out["top_outflows"] and out["top_outflows"][0]["spent"] == pytest.approx(5.5)
    assert out["spending_by_category"]
    assert out["spending_by_category"][0]["name"] == "Other spending"
    smx = out.get("spending_mix_chart") or []
    assert smx and any("Other ·" in str(x.get("label", "")) for x in smx)
    digest = llm_digest_payload(out)
    assert "COFFEE" in digest or "COFFEE SHOP" in digest
    assert "PAYROLL" in digest
    digest_agg = llm_digest_payload(
        out,
        digest_context={
            "mode": "aggregate",
            "date_range_start": "2026-01-01",
            "date_range_end": "2026-01-31",
            "contributing_files": "a.csv, b.csv",
        },
    )
    assert "aggregate" in digest_agg.lower()
    assert "a.csv" in digest_agg

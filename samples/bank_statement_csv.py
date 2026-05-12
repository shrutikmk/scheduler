"""Parse bank-export CSV statements with a preamble + transaction table.

Expected columns include Date, Description, Amount, Running Bal.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_SAV_BRK_XFER_RE = re.compile(
    r"(transfer|xfer|trnsfr|p2p).{0,56}\b(sav|brk)\b|\b(sav|brk)\b.{0,32}(transfer|xfer|trnsfr)",
    re.IGNORECASE,
)


def _normalize_header_cell(s: str) -> str:
    return s.strip().lower()


def _find_transaction_header_row(lines: list[str]) -> tuple[int, list[str]]:
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = next(csv.reader(StringIO(line)))
        except csv.Error:
            continue
        if len(row) < 2:
            continue
        cells = [_normalize_header_cell(c) for c in row if c.strip() != ""]
        joined = " ".join(cells)
        if "date" in cells and "description" in joined and (
            "amount" in joined or any("amt" in c for c in cells)
        ):
            return i, row
    raise ValueError("No transaction table header found (expected Date, Description, Amount, …).")


def parse_money_cell(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = raw.strip().strip('"').strip()
    if s == "" or s.lower() in ("n/a", "—", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    s = s.replace(",", "").replace("$", "").strip()
    if s == "" or s == "-":
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _parse_stmt_date(s: str) -> date | None:
    s = s.strip().strip('"').strip()
    if not _DATE_RE.match(s):
        return None
    try:
        m, d, y = s.split("/")
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def _is_beginning_balance_description(desc: str) -> bool:
    return "beginning balance" in desc.strip().lower()


@dataclass
class StatementRow:
    row_date: date
    description: str
    amount: float | None
    running_balance: float | None


def _fingerprint_norm_description(description: str) -> str:
    s = description.strip().lower()
    return re.sub(r"\s+", " ", s)


def transaction_fingerprint(row: StatementRow) -> str:
    """Stable hash for deduping the same bank posting across exports (canonical expense ID)."""
    amt = row.amount
    amt_key = "" if amt is None else f"{float(amt):.4f}"
    desc = _fingerprint_norm_description(row.description)
    key = f"{row.row_date.isoformat()}|{amt_key}|{desc}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _read_csv_transaction_rows(lines: list[str], header_idx: int) -> list[StatementRow]:
    block = "\n".join(lines[header_idx:])
    reader = csv.DictReader(StringIO(block))
    if not reader.fieldnames:
        return []

    fn = [_normalize_header_cell(h or "") for h in reader.fieldnames]
    key_map: dict[str, str] = {}
    for orig, norm in zip(reader.fieldnames, fn, strict=True):
        if norm:
            key_map[norm] = orig

    def col(*names: str) -> str | None:
        for n in names:
            if n in key_map:
                return key_map[n]
        return None

    k_date = col("date")
    k_desc = col("description")
    k_amt = col("amount", "summary amt.", "summary amt")
    k_run = None
    for cand in key_map:
        if "running" in cand and "bal" in cand.replace(".", ""):
            k_run = key_map[cand]
            break
    if not k_date or not k_desc:
        raise ValueError("Statement table missing Date or Description column.")

    out: list[StatementRow] = []
    for raw in reader:
        d_raw = (raw.get(k_date) or "").strip()
        ds = _parse_stmt_date(d_raw)
        if ds is None:
            continue
        desc = (raw.get(k_desc) or "").strip().strip('"')
        amt = parse_money_cell(raw.get(k_amt)) if k_amt else None
        rb = parse_money_cell(raw.get(k_run)) if k_run else None
        out.append(StatementRow(ds, desc, amt, rb))
    return out


def parse_preamble_summary(lines: list[str], header_idx: int) -> dict[str, str]:
    summary: dict[str, str] = {}
    for i in range(header_idx):
        line = lines[i].strip()
        if not line:
            continue
        try:
            row = next(csv.reader(StringIO(line)))
        except csv.Error:
            continue
        if len(row) < 2:
            continue
        label = (row[0] or "").strip().strip('"')
        if not label or label.lower() == "description":
            continue
        amt_cell = ""
        for cell in row[1:]:
            if cell and str(cell).strip():
                amt_cell = str(cell).strip().strip('"')
                break
        v = parse_money_cell(amt_cell)
        if v is not None:
            summary[label] = f"{v:.2f}"
        elif amt_cell:
            summary[label] = amt_cell
    return summary


def _dedupe_beginning_balance_rows(raw_rows: list[StatementRow]) -> list[StatementRow]:
    seen_begin = False
    rows: list[StatementRow] = []
    for r in raw_rows:
        if _is_beginning_balance_description(r.description):
            if seen_begin:
                continue
            seen_begin = True
        rows.append(r)
    return rows


def load_statement_rows_from_path(path: Path) -> tuple[dict[str, str], list[StatementRow]]:
    """Read CSV file; return preamble dict and ledger rows (beginning-balance rows collapsed)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    header_idx, _hdr = _find_transaction_header_row(lines)
    preamble = parse_preamble_summary(lines, header_idx)
    raw_rows = _read_csv_transaction_rows(lines, header_idx)
    rows = _dedupe_beginning_balance_rows(raw_rows)
    return preamble, rows


def is_sav_brk_transfer_debit(description: str, amount: float | None) -> bool:
    """True for outflows that look like transfers to savings or brokerage (SAV/BRK)."""
    if amount is None or amount >= -1e-9:
        return False
    d = (description or "").strip()
    if not d:
        return False
    if _SAV_BRK_XFER_RE.search(d):
        return True
    low = d.lower()
    if "brk" in low and ("transfer" in low or "xfer" in low or "trnsfr" in low):
        return True
    if re.search(r"\bsav\b", low) and ("transfer" in low or "xfer" in low or "trnsfr" in low):
        return True
    return False


def clip_statement_rows(
    rows: list[StatementRow],
    start: date | None,
    end: date | None,
) -> list[StatementRow]:
    """Filter rows to inclusive date range; no bounds means all rows."""
    if start is None and end is None:
        return list(rows)
    out: list[StatementRow] = []
    for r in rows:
        if start is not None and r.row_date < start:
            continue
        if end is not None and r.row_date > end:
            continue
        out.append(r)
    return out


def payee_key(description: str) -> str:
    key = description.strip()
    if len(key) > 48:
        key = key[:45] + "…"
    return key


def transactions_matching_spend_mix_row(
    transactions: list[dict[str, Any]],
    meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """Debit rows whose spend categorization matches a spending_mix_chart row (see drill rules)."""
    if not meta or not transactions:
        return []
    parent = meta.get("parent_category")
    drill = meta.get("drill")
    out: list[dict[str, Any]] = []
    for t in transactions:
        if t.get("amount") is None:
            continue
        try:
            if float(t["amount"]) >= 0:
                continue
        except (TypeError, ValueError):
            continue
        if t.get("spend_category") != parent:
            continue
        if drill == "other_rest":
            ex = meta.get("exclude_subcategories") or []
            ex_set = set(ex) if isinstance(ex, (list, tuple)) else set()
            sub = t.get("spend_subcategory")
            if sub and sub not in ex_set:
                out.append(t)
        elif drill == "sub":
            if t.get("spend_subcategory") == meta.get("subcategory"):
                out.append(t)
        else:
            if not t.get("spend_subcategory"):
                out.append(t)
    return out


def _debit_parent_category(
    desc_l: str,
    spend_rules: list[tuple[str, tuple[str, ...]]],
    uncategorized: str,
) -> str:
    for name, keys in spend_rules:
        if any(k in desc_l for k in keys):
            return name
    return uncategorized


def _transfer_investing_subcategory(desc_l: str) -> str:
    if any(
        k in desc_l
        for k in (
            "robinhood",
            "brk",
            "schwab",
            "fidelity",
            "etrade",
            "interactive brokers",
            "m1 finance",
            "webull",
        )
    ):
        return "Brokerage & investments"
    if " sav" in desc_l or "sav " in desc_l or re.search(r"\bsav\b", desc_l) or "savings" in desc_l:
        return "Savings (SAV)"
    if "zelle" in desc_l:
        return "Zelle"
    if "venmo" in desc_l:
        return "Venmo"
    if "apple cash" in desc_l or "google pay" in desc_l or "cash app" in desc_l:
        return "Wallet / P2P"
    if "paypal" in desc_l and ("transfer" in desc_l or "withdraw" in desc_l):
        return "PayPal / wallet"
    if "transfer from" in desc_l:
        return "Transfer in (external)"
    if "transfer to" in desc_l or "xfer" in desc_l or "trnsfr" in desc_l:
        return "Transfer out"
    return "Other transfers"


def _debit_spend_subcategory(
    parent: str,
    description: str,
    desc_l: str,
    uncategorized: str,
) -> str:
    if parent == "Transfers & investing":
        return _transfer_investing_subcategory(desc_l)
    if parent == uncategorized:
        return payee_key(description)
    return ""


_OTHER_PAYEE_TOP_N = 10


def _build_spending_mix_chart(
    spending_by_category: list[dict[str, Any]],
    sub_bucket: dict[str, dict[str, list[float, int]]],
    uncategorized: str,
    total_spend_cats: float,
) -> list[dict[str, Any]]:
    """Flatten category chart rows; split Transfers & Other spending into sub-rows."""
    mix: list[dict[str, Any]] = []
    tot_ref = total_spend_cats if total_spend_cats > 1e-9 else 0.0

    def pct_of_spend(t: float) -> float:
        return round(100.0 * t / tot_ref, 1) if tot_ref > 1e-9 else 0.0

    for cat_row in spending_by_category:
        name = str(cat_row["name"])
        if name == "Transfers & investing" and sub_bucket.get(name):
            subs = sorted(sub_bucket[name].items(), key=lambda x: -x[1][0])
            for sub_name, cell in subs:
                st, sc = cell[0], cell[1]
                st_r = round(st, 2)
                mix.append(
                    {
                        "label": f"Transfers · {sub_name}",
                        "total": st_r,
                        "pct_of_spend": pct_of_spend(st_r),
                        "count": int(sc),
                        "parent_category": name,
                        "subcategory": sub_name,
                        "drill": "sub",
                    }
                )
        elif name == uncategorized and sub_bucket.get(name):
            subs = sorted(sub_bucket[name].items(), key=lambda x: -x[1][0])
            top = subs[:_OTHER_PAYEE_TOP_N]
            rest = subs[_OTHER_PAYEE_TOP_N:]
            exclude_keys = [k for k, _ in top]
            for sub_name, cell in top:
                st, sc = cell[0], cell[1]
                st_r = round(st, 2)
                mix.append(
                    {
                        "label": f"Other · {sub_name}",
                        "total": st_r,
                        "pct_of_spend": pct_of_spend(st_r),
                        "count": int(sc),
                        "parent_category": name,
                        "subcategory": sub_name,
                        "drill": "sub",
                    }
                )
            if rest:
                rtot = round(sum(c[0] for _, c in rest), 2)
                rcnt = sum(int(c[1]) for _, c in rest)
                mix.append(
                    {
                        "label": f"Other · All other payees ({len(rest)} payees)",
                        "total": rtot,
                        "pct_of_spend": pct_of_spend(rtot),
                        "count": rcnt,
                        "parent_category": name,
                        "subcategory": None,
                        "drill": "other_rest",
                        "exclude_subcategories": exclude_keys,
                    }
                )
        else:
            mix.append(
                {
                    "label": name,
                    "total": cat_row["total"],
                    "pct_of_spend": cat_row["pct"],
                    "count": cat_row["count"],
                    "parent_category": name,
                    "subcategory": None,
                    "drill": "parent",
                }
            )
    mix.sort(key=lambda x: -float(x["total"]))
    return mix


def _median_float(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return float((s[mid - 1] + s[mid]) / 2)


def summarize_rows(
    rows: list[StatementRow],
    *,
    filename: str,
    preamble: dict[str, str] | None = None,
    include_running_balance: bool = True,
) -> dict[str, Any]:
    """Build summary dict (transactions, totals, series, categories) from in-memory rows."""
    preamble = preamble or {}
    txs: list[dict[str, Any]] = []
    daily_net = defaultdict(float)
    daily_inflow = defaultdict(float)
    daily_outflow = defaultdict(float)
    merchant_totals: dict[str, float] = defaultdict(float)
    merchant_counts: dict[str, int] = defaultdict(int)

    spend_rules: list[tuple[str, tuple[str, ...]]] = [
        ("Transfers & investing", ("transfer to", "transfer from", "robinhood", "brk ", "sav ")),
        ("Amazon & shopping", ("amazon", "amzn", "target", "trader joe", "h&m", "paypal *")),
        ("Food & dining", ("tst*", "cafe", "ramen", "taco", "jinya", "velvet taco")),
        ("Travel", ("delta air", "southwest", "united ", "frontier", "air x")),
        ("Subscriptions & software", ("apple.com/bill", "grok", "att des:payment")),
        ("Transport", ("uber", "lyft")),
    ]
    income_keys = ("payroll", "irs treas", "tax ref", "casttaxrfd", "venmo des:cashout")

    category_totals: dict[str, float] = defaultdict(float)
    category_counts: dict[str, int] = defaultdict(int)
    uncategorized = "Other spending"

    total_credits = 0.0
    total_debits = 0.0

    running_points: list[tuple[date, float]] = []
    largest_out: tuple[float, date, str] | None = None
    max_credit: tuple[float, str] | None = None

    sub_bucket: dict[str, dict[str, list[float, int]]] = {}

    for r in rows:
        d_iso = r.row_date.isoformat()
        spend_cat: str | None = None
        spend_sub: str | None = None

        if include_running_balance and r.running_balance is not None:
            running_points.append((r.row_date, r.running_balance))

        if r.amount is not None and abs(r.amount) > 1e-9:
            desc_l = r.description.lower()
            daily_net[r.row_date] += r.amount
            if r.amount > 0:
                total_credits += r.amount
                daily_inflow[r.row_date] += r.amount
                if max_credit is None or r.amount > max_credit[0]:
                    max_credit = (r.amount, r.description.strip())
                cat = (
                    "Income & refunds"
                    if any(k in desc_l for k in income_keys)
                    else "Other credits"
                )
                category_totals[cat] += r.amount
                category_counts[cat] += 1
            else:
                total_debits += r.amount
                daily_outflow[r.row_date] += abs(r.amount)
                if largest_out is None or r.amount < largest_out[0]:
                    largest_out = (r.amount, r.row_date, r.description.strip())
                cat = _debit_parent_category(desc_l, spend_rules, uncategorized)
                spend_cat = cat
                sub_raw = _debit_spend_subcategory(cat, r.description, desc_l, uncategorized)
                spend_sub = sub_raw if sub_raw else None
                category_totals[cat] += abs(r.amount)
                category_counts[cat] += 1
                if cat in ("Transfers & investing", uncategorized):
                    inner = sub_bucket.setdefault(cat, {})
                    cell = inner.setdefault(sub_raw, [0.0, 0])
                    cell[0] += abs(r.amount)
                    cell[1] += 1

            pk = payee_key(r.description)
            merchant_totals[pk] += r.amount
            merchant_counts[pk] += 1

        fp = transaction_fingerprint(
            StatementRow(r.row_date, r.description, r.amount, r.running_balance)
        )
        txs.append(
            {
                "fingerprint": fp,
                "date": d_iso,
                "description": r.description,
                "amount": r.amount,
                "running_balance": r.running_balance,
                "spend_category": spend_cat,
                "spend_subcategory": spend_sub,
            }
        )

    sorted_days = sorted(daily_net.keys())
    series_daily_net = [{"date": d.isoformat(), "net": round(daily_net[d], 2)} for d in sorted_days]

    cash_flow_days = sorted(set(daily_inflow) | set(daily_outflow))
    series_cash_flow_daily = [
        {
            "date": d.isoformat(),
            "inflow": round(daily_inflow[d], 2),
            "outflow": round(daily_outflow[d], 2),
        }
        for d in cash_flow_days
    ]

    cumulative = 0.0
    series_cumulative_net: list[dict[str, Any]] = []
    for d in sorted_days:
        cumulative += daily_net[d]
        series_cumulative_net.append(
            {"date": d.isoformat(), "cumulative_net": round(cumulative, 2)}
        )

    last_by_day: dict[date, float] = {}
    for d, bal in running_points:
        last_by_day[d] = bal
    series_balance = [
        {"date": d.isoformat(), "balance": round(last_by_day[d], 2)}
        for d in sorted(last_by_day.keys())
    ]

    weekly_expense: dict[date, float] = defaultdict(float)
    for r in rows:
        if r.amount is not None and r.amount < 0:
            ws = r.row_date - timedelta(days=r.row_date.weekday())
            weekly_expense[ws] += abs(r.amount)
    series_weekly_expenses = [
        {"week_start": ws.isoformat(), "spent": round(weekly_expense[ws], 2)}
        for ws in sorted(weekly_expense.keys())
    ]

    top_merchants = sorted(
        (
            {
                "label": label,
                "total": round(v, 2),
                "count": merchant_counts[label],
            }
            for label, v in merchant_totals.items()
        ),
        key=lambda x: abs(float(x["total"])),
        reverse=True,
    )[:15]

    top_outflows = sorted(
        (
            {
                "label": label,
                "total": round(v, 2),
                "count": merchant_counts[label],
                "spent": round(abs(v), 2),
            }
            for label, v in merchant_totals.items()
            if v < 0
        ),
        key=lambda x: float(x["total"]),
    )[:12]

    cats_out = [
        {"name": n, "total": round(category_totals[n], 2), "count": category_counts[n]}
        for n in sorted(category_totals.keys(), key=lambda k: category_totals[k], reverse=True)
        if category_totals[n] > 0
    ]

    _non_spend_cats = frozenset({"Income & refunds", "Other credits"})
    spend_pairs = [
        (n, category_totals[n])
        for n in category_totals
        if n not in _non_spend_cats and category_totals[n] > 0
    ]
    total_spend_cats = sum(v for _, v in spend_pairs)
    spending_by_category = [
        {
            "name": n,
            "total": round(v, 2),
            "pct": round(100.0 * v / total_spend_cats, 1) if total_spend_cats > 1e-9 else 0.0,
            "count": category_counts[n],
        }
        for n, v in sorted(spend_pairs, key=lambda x: -x[1])
    ]

    spending_mix_chart = _build_spending_mix_chart(
        spending_by_category,
        sub_bucket,
        uncategorized,
        total_spend_cats,
    )

    row_dates = [r.row_date for r in rows]
    calendar_days = (max(row_dates) - min(row_dates)).days + 1 if row_dates else 0
    expense_total = abs(total_debits)
    median_daily_net = round(_median_float([daily_net[d] for d in sorted_days]), 2)
    savings_rate: float | None = None
    if total_credits > 1e-6:
        savings_rate = round((total_credits - expense_total) / total_credits * 100.0, 1)
    avg_daily_expense = round(expense_total / calendar_days, 2) if calendar_days else 0.0
    income_concentration_pct: float | None = None
    if max_credit and total_credits > 1e-6:
        income_concentration_pct = round(100.0 * max_credit[0] / total_credits, 1)

    lo_dict: dict[str, Any] | None = None
    if largest_out is not None:
        amt, d_lo, desc_lo = largest_out
        lo_dict = {
            "date": d_lo.isoformat(),
            "amount": round(amt, 2),
            "description": (desc_lo[:120] + "…") if len(desc_lo) > 120 else desc_lo,
        }

    top_spend_cat = spending_by_category[0] if spending_by_category else None
    kpi: dict[str, Any] = {
        "income_total": round(total_credits, 2),
        "expense_total": round(expense_total, 2),
        "net_flow": round(total_credits + total_debits, 2),
        "savings_rate_pct": savings_rate,
        "calendar_days": calendar_days,
        "days_with_activity": len(sorted_days),
        "avg_daily_expense": avg_daily_expense,
        "median_daily_net": median_daily_net,
        "largest_outflow": lo_dict,
        "income_concentration_pct": income_concentration_pct,
        "top_spending_category": top_spend_cat["name"] if top_spend_cat else None,
        "top_spending_category_pct": top_spend_cat["pct"] if top_spend_cat else None,
    }

    out: dict[str, Any] = {
        "filename": filename,
        "preamble": preamble,
        "transaction_count": len(txs),
        "totals": {
            "credits": round(total_credits, 2),
            "debits": round(total_debits, 2),
            "net_flow": round(total_credits + total_debits, 2),
        },
        "kpi": kpi,
        "transactions": txs,
        "series_daily_net": series_daily_net,
        "series_cash_flow_daily": series_cash_flow_daily,
        "series_weekly_expenses": series_weekly_expenses,
        "series_cumulative_net": series_cumulative_net,
        "spending_by_category": spending_by_category,
        "spending_mix_chart": spending_mix_chart,
        "top_merchants": top_merchants,
        "top_outflows": top_outflows,
        "categories": cats_out,
    }
    if include_running_balance:
        out["series_balance"] = series_balance
    else:
        out["series_balance"] = []
    return out


def rebuild_summary_spend_from_transactions(summary: dict[str, Any]) -> None:
    """Recompute spending_by_category and spending_mix_chart from ``transactions`` rows.

    Uses each debit's ``spend_category`` (ledger + heuristic). Respects
    ``_insights_exclude_sav_brk`` when set on ``summary``.
    """
    unc = "Misc & uncategorized (only when nothing else fits)"
    txs = summary.get("transactions") or []
    excl = bool(summary.get("_insights_exclude_sav_brk"))
    debit_totals: dict[str, float] = defaultdict(float)
    debit_counts: dict[str, int] = defaultdict(int)
    for t in txs:
        if t.get("amount") is None:
            continue
        try:
            amt = float(t["amount"])
            if amt >= 0:
                continue
        except (TypeError, ValueError):
            continue
        desc = str(t.get("description") or "")
        if excl and is_sav_brk_transfer_debit(desc, amt):
            continue
        raw_cat = t.get("spend_category")
        c = str(raw_cat).strip() if raw_cat else ""
        if not c:
            c = unc
        debit_totals[c] += abs(amt)
        debit_counts[c] += 1
    spend_pairs = sorted(debit_totals.items(), key=lambda x: -x[1])
    total_spend = sum(v for _, v in spend_pairs)
    spending_by_category = [
        {
            "name": n,
            "total": round(v, 2),
            "pct": round(100.0 * v / total_spend, 1) if total_spend > 1e-9 else 0.0,
            "count": debit_counts[n],
        }
        for n, v in spend_pairs
    ]
    summary["spending_by_category"] = spending_by_category
    summary["spending_mix_chart"] = _build_spending_mix_chart(
        spending_by_category,
        {},
        unc,
        total_spend,
    )
    kpi = summary.setdefault("kpi", {})
    if spending_by_category:
        top = spending_by_category[0]
        kpi["top_spending_category"] = top["name"]
        kpi["top_spending_category_pct"] = top["pct"]
    else:
        kpi["top_spending_category"] = None
        kpi["top_spending_category_pct"] = None


def summarize_rows_for_insights(
    rows: list[StatementRow],
    *,
    filename: str,
    preamble: dict[str, str] | None = None,
    include_running_balance: bool = True,
    exclude_sav_brk_transfer_debits: bool = False,
) -> dict[str, Any]:
    """Re-summarize optionally without SAV/BRK-style transfer debits.

    When exclusion is on, ``totals``, running-balance series, full transaction list,
    and transaction count stay aligned to the full ledger; spending-oriented series,
    KPIs, cash-flow / weekly / daily-net / cumulative-net charts use the filtered
    rows (transfers omitted).
    ``_insights_exclude_sav_brk`` is set on the merged dict.
    """
    full = summarize_rows(
        rows,
        filename=filename,
        preamble=preamble,
        include_running_balance=include_running_balance,
    )
    if not exclude_sav_brk_transfer_debits:
        return full

    filtered = [r for r in rows if not is_sav_brk_transfer_debit(r.description, r.amount)]
    adj = summarize_rows(
        filtered,
        filename=filename,
        preamble=preamble,
        include_running_balance=include_running_balance,
    )
    merged: dict[str, Any] = {**adj}
    merged["totals"] = full["totals"]
    merged["transactions"] = full["transactions"]
    merged["series_balance"] = full["series_balance"]
    merged["series_cumulative_net"] = adj["series_cumulative_net"]
    merged["series_daily_net"] = adj["series_daily_net"]
    merged["transaction_count"] = full["transaction_count"]
    merged["_insights_exclude_sav_brk"] = True
    fk = full.get("kpi") or {}
    ak = adj.get("kpi") or {}
    merged["kpi"] = {
        **ak,
        "full_ledger_net_flow": full["totals"]["net_flow"],
        "full_expense_total": fk.get("expense_total"),
        "full_income_total": fk.get("income_total"),
    }
    return merged


def parse_bank_statement_csv(path: Path) -> dict[str, Any]:
    """Load statement path; return transactions, aggregates, and chart-ready series."""
    preamble, rows = load_statement_rows_from_path(path)
    return summarize_rows(
        rows,
        filename=path.name,
        preamble=preamble,
        include_running_balance=True,
    )


def llm_digest_payload(
    summary: dict[str, Any],
    *,
    digest_context: dict[str, Any] | None = None,
) -> str:
    """Compact text block for the insight model user turn."""
    ctx = digest_context or {}
    mode = ctx.get("mode")
    dr_start = ctx.get("date_range_start")
    dr_end = ctx.get("date_range_end")
    contrib = ctx.get("contributing_files")

    lines: list[str] = []
    if mode:
        lines.append(f"View mode: {mode}")
    if dr_start or dr_end:
        lines.append(f"Date filter (inclusive): {dr_start or '…'} — {dr_end or '…'}")
    if mode == "aggregate" and contrib:
        lines.append(f"Source CSVs (deduplicated ledger): {contrib}")
    lines.extend(
        [
            f"File label: {summary.get('filename', '')}",
            f"Preamble summary (from export): {summary.get('preamble', {})}",
            f"Totals — credits: {summary['totals']['credits']}, "
            f"debits: {summary['totals']['debits']}, "
            f"net: {summary['totals']['net_flow']}",
            f"Transaction rows in this view: {summary.get('transaction_count', 0)}",
        ]
    )
    if summary.get("_insights_exclude_sav_brk"):
        lines.append(
            "INSIGHTS LENS: Spending KPIs, weekly spend, category mix, top outflows, "
            "and daily in/out series below omit debit rows classified as savings/brokerage "
            "transfers (SAV/BRK heuristics). "
            "Export totals above and sample transactions are the full ledger."
        )
    kpi = summary.get("kpi") or {}
    if kpi:
        if summary.get("_insights_exclude_sav_brk"):
            lines.append("Derived KPIs (spending-focused; SAV/BRK transfer debits excluded):")
            lines.append(
                f"  - Money in: {kpi.get('income_total')}; "
                f"money out (spend lens): {kpi.get('expense_total')}; "
                f"full-ledger money out: {kpi.get('full_expense_total')}; "
                f"net cash flow (full ledger): {kpi.get('full_ledger_net_flow')}"
            )
        else:
            lines.append("Derived KPIs (use these explicitly):")
            lines.append(
                f"  - Money in: {kpi.get('income_total')}; "
                f"money out (sum of debits): {kpi.get('expense_total')}; "
                f"net cash flow: {kpi.get('net_flow')}"
            )
        if kpi.get("savings_rate_pct") is None:
            lines.append("  - Savings rate vs income: n/a (no or negligible income in period)")
        else:
            lines.append(f"  - Savings rate vs income: {kpi.get('savings_rate_pct')}%")
        lines.append(
            f"  - Period span: {kpi.get('calendar_days')} calendar days; "
            f"{kpi.get('days_with_activity')} days with ledger activity; "
            f"avg spend per calendar day: {kpi.get('avg_daily_expense')}"
        )
        lines.append(f"  - Median daily net: {kpi.get('median_daily_net')}")
        ic = kpi.get("income_concentration_pct")
        if ic is not None:
            lines.append(f"  - Largest single inflow is {ic}% of total credits")
        tcat = kpi.get("top_spending_category")
        tpct = kpi.get("top_spending_category_pct")
        if tcat:
            lines.append(
                f"  - Top spending bucket (heuristic): {tcat} "
                f"({tpct}% of categorized spend)"
            )
        lo = kpi.get("largest_outflow")
        if isinstance(lo, dict) and lo.get("amount") is not None:
            desc = str(lo.get("description", ""))[:100]
            lines.append(
                f"  - Largest single outflow: {lo.get('amount')} on {lo.get('date')} — {desc}"
            )
    weekly = summary.get("series_weekly_expenses") or []
    if weekly:
        lines.append("Weekly total spending (debits, week starts Monday):")
        for w in weekly[-8:]:
            lines.append(f"  - week of {w.get('week_start')}: spent {w.get('spent')}")
    sc = summary.get("spending_by_category") or []
    if sc:
        lines.append("Spending mix (heuristic categories, debit totals; % of spend excl. income):")
        for c in sc[:8]:
            lines.append(f"  - {c.get('name')}: {c.get('total')} ({c.get('pct')}%)")
    smx = summary.get("spending_mix_chart") or []
    if smx:
        lines.append(
            "Expanded mix (transfers by subtype; other = payee clusters; % of total spend):"
        )
        for row in smx[:14]:
            lines.append(
                f"  - {row.get('label')}: {row.get('total')} ({row.get('pct_of_spend')}%)"
            )
    oo = summary.get("top_outflows") or []
    if oo:
        lines.append("Top outflows by merchant/description (negative totals = cash leaving):")
        for m in oo[:8]:
            lines.append(f"  - {m.get('label', '')}: {m.get('total')} ({m.get('count')} tx)")
    tm = summary.get("top_merchants") or []
    lines.append("Top merchants by magnitude (signed; includes credits):")
    for m in tm[:6]:
        lines.append(f"  - {m.get('label', '')}: {m.get('total')} ({m.get('count')} tx)")
    cats = summary.get("categories") or []
    lines.append("All category buckets (credits + debits, heuristic):")
    for c in cats[:8]:
        lines.append(f"  - {c.get('name')}: {c.get('total')} ({c.get('count')} tx)")
    txs = summary.get("transactions") or []
    sample = [t for t in txs if t.get("amount") not in (None, 0)][:24]
    lines.append("Sample transactions:")
    for t in sample:
        lines.append(
            f"  {t.get('date')} | {t.get('amount')} | {str(t.get('description', ''))[:120]}"
        )
    return "\n".join(lines)

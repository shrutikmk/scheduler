"""SQLite ledger: deduplicated transactions from multiple bank CSV uploads."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from bank_statement_csv import (
    StatementRow,
    load_statement_rows_from_path,
    payee_key,
    transaction_fingerprint,
    transactions_matching_spend_mix_row,
)

_SAMPLES_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SAMPLES_DIR.parent
DEFAULT_LEDGER_DB = _REPO_ROOT / "financial-data" / "ledger.sqlite"


def default_ledger_path() -> Path:
    return DEFAULT_LEDGER_DB


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    p = db_path or default_ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            name TEXT PRIMARY KEY,
            imported_at TEXT NOT NULL,
            min_date TEXT,
            max_date TEXT,
            row_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS transactions (
            fingerprint TEXT PRIMARY KEY,
            post_date TEXT NOT NULL,
            amount REAL,
            description TEXT NOT NULL,
            sources_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_transactions_post_date
        ON transactions (post_date);
        """
    )
    conn.commit()
    _ensure_transaction_title_columns(conn)
    _ensure_mix_chart_labels_table(conn)
    _ensure_financial_insights_table(conn)


def _ensure_transaction_title_columns(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(transactions)")
    cols = {str(r[1]) for r in cur.fetchall()}
    if "display_title" not in cols:
        cur.execute("ALTER TABLE transactions ADD COLUMN display_title TEXT")
    if "display_title_at" not in cols:
        cur.execute("ALTER TABLE transactions ADD COLUMN display_title_at TEXT")
    if "spend_category" not in cols:
        cur.execute("ALTER TABLE transactions ADD COLUMN spend_category TEXT")
    if "spend_category_at" not in cols:
        cur.execute("ALTER TABLE transactions ADD COLUMN spend_category_at TEXT")
    if "retitle_pending" not in cols:
        cur.execute("ALTER TABLE transactions ADD COLUMN retitle_pending INTEGER")
        cur.execute(
            """
            UPDATE transactions SET retitle_pending = CASE
                WHEN amount IS NULL OR amount >= 0 THEN 0
                WHEN IFNULL(TRIM(display_title), '') = ''
                     OR IFNULL(TRIM(spend_category), '') = '' THEN 1
                ELSE 0
            END
            """
        )
    conn.commit()


def fetch_ledger_tx_annotations(
    conn: sqlite3.Connection, fingerprints: list[str]
) -> dict[str, dict[str, Any]]:
    """Return fingerprint -> display_title, spend_category, retitle_pending (if row exists)."""
    if not fingerprints:
        return {}
    init_schema(conn)
    uniq: list[str] = list(dict.fromkeys(str(f) for f in fingerprints if f))
    if not uniq:
        return {}
    qmarks = ",".join("?" * len(uniq))
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT fingerprint, display_title, spend_category, retitle_pending
        FROM transactions
        WHERE fingerprint IN ({qmarks})
        """,
        uniq,
    )
    out: dict[str, dict[str, Any]] = {}
    for fp, dt, sc, rp in cur.fetchall():
        out[str(fp)] = {
            "display_title": dt,
            "spend_category": sc,
            "retitle_pending": rp,
        }
    return out


def fetch_display_titles(
    conn: sqlite3.Connection, fingerprints: list[str]
) -> dict[str, str | None]:
    """Return fingerprint -> display_title (or None if unset)."""
    if not fingerprints:
        return {}
    init_schema(conn)
    cur = conn.cursor()
    qmarks = ",".join("?" * len(fingerprints))
    cur.execute(
        f"SELECT fingerprint, display_title FROM transactions WHERE fingerprint IN ({qmarks})",
        fingerprints,
    )
    return {str(r[0]): r[1] for r in cur.fetchall()}


def upsert_display_titles(conn: sqlite3.Connection, titles: dict[str, str]) -> int:
    """Set display_title for fingerprints (overwrite allowed). Returns rows updated."""
    if not titles:
        return 0
    init_schema(conn)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    cur = conn.cursor()
    n = 0
    for fp, title in titles.items():
        t = (title or "").strip()
        if not t:
            continue
        cur.execute(
            """
            UPDATE transactions
            SET display_title = ?, display_title_at = ?
            WHERE fingerprint = ?
            """,
            (t[:200], now, fp),
        )
        n += int(cur.rowcount)
    conn.commit()
    return n


def upsert_ledger_title_category(
    conn: sqlite3.Connection, items: dict[str, tuple[str, str]]
) -> int:
    """Set display_title + spend_category and clear ``retitle_pending`` for debit rows."""
    if not items:
        return 0
    init_schema(conn)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    cur = conn.cursor()
    n = 0
    for fp, pair in items.items():
        title, cat = pair[0], pair[1]
        t = (title or "").strip()
        c = (cat or "").strip()
        if not t or not c:
            continue
        cur.execute(
            """
            UPDATE transactions
            SET display_title = ?, display_title_at = ?,
                spend_category = ?, spend_category_at = ?,
                retitle_pending = 0
            WHERE fingerprint = ?
            """,
            (t[:200], now, c[:120], now, fp),
        )
        n += int(cur.rowcount)
    conn.commit()
    return n


def list_debits_pending_retitle(
    conn: sqlite3.Connection, limit: int
) -> list[tuple[str, str]]:
    """Up to ``limit`` debit rows that still need LLM title + category."""
    init_schema(conn)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT fingerprint, description FROM transactions
        WHERE (amount IS NOT NULL AND amount < 0)
          AND retitle_pending = 1
        ORDER BY post_date DESC
        LIMIT ?
        """,
        (max(1, min(limit, 500)),),
    )
    return [(str(r[0]), str(r[1])) for r in cur.fetchall()]


def list_debits_missing_display_title(
    conn: sqlite3.Connection, limit: int
) -> list[tuple[str, str]]:
    """Deprecated alias: use ``list_debits_pending_retitle``."""
    return list_debits_pending_retitle(conn, limit)


def debits_pending_retitle_fps(
    conn: sqlite3.Connection, fingerprints: list[str]
) -> set[str]:
    """Debit fingerprints in ``fingerprints`` with ``retitle_pending``."""
    if not fingerprints:
        return set()
    init_schema(conn)
    uniq: list[str] = list(dict.fromkeys(str(f) for f in fingerprints if f))
    if not uniq:
        return set()
    qmarks = ",".join("?" * len(uniq))
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT fingerprint FROM transactions
        WHERE fingerprint IN ({qmarks})
          AND (amount IS NOT NULL AND amount < 0)
          AND retitle_pending = 1
        """,
        uniq,
    )
    return {str(r[0]) for r in cur.fetchall()}


def debits_pending_display_title_fps(
    conn: sqlite3.Connection, fingerprints: list[str]
) -> set[str]:
    """Deprecated alias: same as ``debits_pending_retitle_fps``."""
    return debits_pending_retitle_fps(conn, fingerprints)


def _ensure_financial_insights_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS financial_insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT NOT NULL UNIQUE,
            view_mode TEXT NOT NULL,
            snapshot_file TEXT,
            date_start TEXT,
            date_end TEXT,
            exclude_sav_brk INTEGER NOT NULL DEFAULT 0,
            transaction_count INTEGER NOT NULL DEFAULT 0,
            markdown TEXT NOT NULL,
            insights_model TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def build_insights_cache_key(
    *,
    view_mode: str,
    snapshot_file: str | None,
    date_start: str | None,
    date_end: str | None,
    exclude_sav_brk: bool,
    transaction_fingerprints: list[str],
    insights_context_hash: str = "",
) -> str:
    """Stable key from view params + sorted ledger row fingerprints (dedupe identity).

    ``insights_context_hash`` should digest the insights system prompt (including any spend
    knowledge) so prompt or KB edits invalidate cached markdown without a full re-ledger.
    """
    payload = {
        "mode": view_mode,
        "file": snapshot_file or "",
        "start": date_start or "",
        "end": date_end or "",
        "ex": bool(exclude_sav_brk),
        "fps": sorted(transaction_fingerprints),
        "ictx": str(insights_context_hash or ""),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def insights_cache_get(conn: sqlite3.Connection, cache_key: str) -> dict[str, Any] | None:
    init_schema(conn)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, markdown, insights_model, created_at, updated_at
        FROM financial_insights
        WHERE cache_key = ?
        """,
        (cache_key,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "markdown": str(row[1]),
        "insights_model": row[2],
        "created_at": row[3],
        "updated_at": row[4],
    }


def insights_cache_store(
    conn: sqlite3.Connection,
    *,
    cache_key: str,
    markdown: str,
    insights_model: str | None,
    view_mode: str,
    snapshot_file: str | None,
    date_start: str | None,
    date_end: str | None,
    exclude_sav_brk: bool,
    transaction_count: int,
) -> int:
    """Insert or update cached insights; returns row id (stable for a given cache_key)."""
    init_schema(conn)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO financial_insights (
            cache_key, view_mode, snapshot_file, date_start, date_end,
            exclude_sav_brk, transaction_count, markdown, insights_model,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            view_mode = excluded.view_mode,
            snapshot_file = excluded.snapshot_file,
            date_start = excluded.date_start,
            date_end = excluded.date_end,
            exclude_sav_brk = excluded.exclude_sav_brk,
            transaction_count = excluded.transaction_count,
            markdown = excluded.markdown,
            insights_model = excluded.insights_model,
            updated_at = excluded.updated_at
        RETURNING id
        """,
        (
            cache_key,
            view_mode,
            snapshot_file,
            date_start,
            date_end,
            1 if exclude_sav_brk else 0,
            transaction_count,
            markdown,
            insights_model,
            now,
            now,
        ),
    )
    out = cur.fetchone()
    conn.commit()
    if not out:
        cur.execute("SELECT id FROM financial_insights WHERE cache_key = ?", (cache_key,))
        row2 = cur.fetchone()
        return int(row2[0]) if row2 else 0
    return int(out[0])


def _ensure_mix_chart_labels_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mix_chart_labels (
            row_key TEXT PRIMARY KEY,
            display_label TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def spend_mix_row_key(row: dict[str, Any]) -> str:
    """Stable id for a spending_mix_chart row (persisted LLM bar label)."""
    parent = str(row.get("parent_category") or "")
    sub = str(row.get("subcategory") or "")
    drill = str(row.get("drill") or "")
    label = str(row.get("label") or "")
    excl = row.get("exclude_subcategories")
    excl_s = json.dumps(excl, sort_keys=True) if isinstance(excl, list) else ""
    raw = f"{parent}\0{sub}\0{drill}\0{label}\0{excl_s}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mix_row_needs_llm_bar_label(row: dict[str, Any]) -> bool:
    """Rows that show raw payee strings — polish with LLM after transaction titles exist."""
    lab = str(row.get("label") or "")
    return lab.startswith("Other ·")


def fetch_mix_chart_labels(
    conn: sqlite3.Connection, row_keys: list[str]
) -> dict[str, str | None]:
    if not row_keys:
        return {}
    init_schema(conn)
    uniq = list(dict.fromkeys(row_keys))
    qmarks = ",".join("?" * len(uniq))
    cur = conn.cursor()
    cur.execute(
        f"SELECT row_key, display_label FROM mix_chart_labels WHERE row_key IN ({qmarks})",
        uniq,
    )
    return {str(r[0]): str(r[1]) for r in cur.fetchall()}


def upsert_mix_chart_labels(conn: sqlite3.Connection, labels: dict[str, str]) -> int:
    if not labels:
        return 0
    init_schema(conn)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    cur = conn.cursor()
    n = 0
    for key, label in labels.items():
        t = (label or "").strip()
        if not t or not key:
            continue
        cur.execute(
            """
            INSERT INTO mix_chart_labels (row_key, display_label, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(row_key) DO UPDATE SET
                display_label = excluded.display_label,
                updated_at = excluded.updated_at
            """,
            (str(key), t[:200], now),
        )
        n += 1
    conn.commit()
    return n


def _tx_debit_pending_retitle(t: dict[str, Any]) -> bool:
    if t.get("amount") is None:
        return False
    try:
        if float(t["amount"]) >= 0:
            return False
    except (TypeError, ValueError):
        return False
    rp = t.get("retitle_pending")
    if rp is not None:
        try:
            return int(rp) == 1
        except (TypeError, ValueError):
            return bool(rp)
    tv = t.get("display_title")
    sc = t.get("spend_category")
    return not (tv and str(tv).strip() and sc and str(sc).strip())


def _tx_debit_missing_display_title(t: dict[str, Any]) -> bool:
    return _tx_debit_pending_retitle(t)


def enrich_summary_mix_chart_labels(conn: sqlite3.Connection, summary: dict[str, Any]) -> None:
    """Attach chart_display_label / loading flags for spending mix; set mix_chart_labels_pending."""
    mix = summary.get("spending_mix_chart") or []
    txs = summary.get("transactions") or []
    pending_mix = False
    if not mix:
        summary["mix_chart_labels_pending"] = False
    else:
        keys = [spend_mix_row_key(r) for r in mix if mix_row_needs_llm_bar_label(r)]
        uniq_keys = list(dict.fromkeys(keys))
        dbm = fetch_mix_chart_labels(conn, uniq_keys) if uniq_keys else {}
        for row in mix:
            if not mix_row_needs_llm_bar_label(row):
                row["chart_display_label"] = str(row.get("label") or "")
                row["mix_label_loading"] = False
                continue
            key = spend_mix_row_key(row)
            stored = dbm.get(key)
            if stored and str(stored).strip():
                row["chart_display_label"] = str(stored).strip()
                row["mix_label_loading"] = False
                continue
            mt = transactions_matching_spend_mix_row(txs, row)
            base = str(row.get("label") or "")
            if not mt:
                row["chart_display_label"] = base
                row["mix_label_loading"] = False
                continue
            if any(_tx_debit_pending_retitle(t) for t in mt):
                row["chart_display_label"] = base
                row["mix_label_loading"] = False
                pending_mix = True
            else:
                row["chart_display_label"] = base
                row["mix_label_loading"] = True
                pending_mix = True
        summary["mix_chart_labels_pending"] = pending_mix
    summary["llm_labels_incomplete"] = bool(summary.get("display_titles_pending")) or bool(
        summary.get("mix_chart_labels_pending")
    )


def enrich_summary_with_ledger_titles(conn: sqlite3.Connection, summary: dict[str, Any]) -> None:
    """Attach ledger annotations to transactions and top_outflows display labels."""
    txs = summary.get("transactions") or []
    fps = [str(t["fingerprint"]) for t in txs if t.get("fingerprint")]
    if not fps:
        summary["display_titles_pending"] = False
    else:
        ann = fetch_ledger_tx_annotations(conn, fps)
        for t in txs:
            fp = str(t.get("fingerprint") or "")
            a = ann.get(fp) or {}
            tv = a.get("display_title")
            if tv and str(tv).strip():
                t["display_title"] = str(tv).strip()
            else:
                t["display_title"] = None
            dcat = a.get("spend_category")
            if dcat and str(dcat).strip():
                t["spend_category"] = str(dcat).strip()
                t["spend_subcategory"] = None
            rp_raw = a.get("retitle_pending")
            if t.get("amount") is None:
                t["retitle_pending"] = 0
            else:
                try:
                    cred = float(t["amount"]) >= 0
                except (TypeError, ValueError):
                    cred = True
                if cred:
                    t["retitle_pending"] = 0
                elif rp_raw is None:
                    t["retitle_pending"] = (
                        1
                        if (
                            not (t.get("display_title") or "").strip()
                            or not (t.get("spend_category") or "").strip()
                        )
                        else 0
                    )
                else:
                    try:
                        t["retitle_pending"] = 1 if int(rp_raw) == 1 else 0
                    except (TypeError, ValueError):
                        t["retitle_pending"] = 1 if rp_raw else 0

        debit_fps: list[str] = []
        for t in txs:
            fp = str(t.get("fingerprint") or "")
            if not fp:
                continue
            if t.get("amount") is None:
                continue
            try:
                if float(t["amount"]) >= 0:
                    continue
            except (TypeError, ValueError):
                continue
            debit_fps.append(fp)

        pending = debits_pending_retitle_fps(conn, debit_fps)
        summary["display_titles_pending"] = bool(pending)

        for row in summary.get("top_outflows") or []:
            label = str(row.get("label") or "")
            matching: list[dict[str, Any]] = []
            for t in txs:
                if t.get("amount") is None:
                    continue
                try:
                    if float(t["amount"]) >= 0:
                        continue
                except (TypeError, ValueError):
                    continue
                if payee_key(str(t.get("description") or "")) == label:
                    matching.append(t)
            row_loading = any(_tx_debit_pending_retitle(t) for t in matching)
            dlab = None
            for t in matching:
                tv = t.get("display_title")
                if tv and str(tv).strip():
                    dlab = tv
                    break
            row["display_title_loading"] = row_loading
            row["display_label"] = dlab if dlab else label


def attach_retitle_progress(summary: dict[str, Any], conn: sqlite3.Connection) -> None:
    """Counts debits in this summary still flagged ``retitle_pending`` in the ledger."""
    txs = summary.get("transactions") or []
    debit_fps: list[str] = []
    for t in txs:
        fp = str(t.get("fingerprint") or "")
        if not fp:
            continue
        if t.get("amount") is None:
            continue
        try:
            if float(t["amount"]) >= 0:
                continue
        except (TypeError, ValueError):
            continue
        debit_fps.append(fp)
    pend_db = debits_pending_retitle_fps(conn, debit_fps)
    el = len(debit_fps)
    summary["retitle_progress"] = {
        "pending": len(pend_db),
        "eligible": el,
        "complete": max(0, el - len(pend_db)),
    }


def enrich_summary_with_ledger(
    conn: sqlite3.Connection, summary: dict[str, Any]
) -> None:
    """Titles, categories, rebuilt spend mix, retitle progress, mix-chart polish flags."""
    from bank_statement_csv import rebuild_summary_spend_from_transactions

    enrich_summary_with_ledger_titles(conn, summary)
    rebuild_summary_spend_from_transactions(summary)
    attach_retitle_progress(summary, conn)
    enrich_summary_mix_chart_labels(conn, summary)


def _detach_source(conn: sqlite3.Connection, filename: str) -> None:
    """Remove ``filename`` from each row's sources; delete rows that have no sources left."""
    init_schema(conn)
    cur = conn.cursor()
    cur.execute("SELECT fingerprint, sources_json FROM transactions")
    to_delete: list[str] = []
    to_update: list[tuple[str, str]] = []
    for fp, sj in cur.fetchall():
        try:
            sources = json.loads(sj)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(sources, list):
            continue
        if filename not in sources:
            continue
        new_sources = [s for s in sources if s != filename]
        if not new_sources:
            to_delete.append(str(fp))
        else:
            to_update.append((str(fp), json.dumps(new_sources)))
    for fp in to_delete:
        cur.execute("DELETE FROM transactions WHERE fingerprint = ?", (fp,))
    for fp, payload in to_update:
        cur.execute(
            "UPDATE transactions SET sources_json = ? WHERE fingerprint = ?",
            (payload, fp),
        )
    conn.commit()


def ingest_bank_csv(conn: sqlite3.Connection, path: Path) -> dict[str, Any]:
    """Import one bank CSV; dedupe rows by fingerprint; track multiple source files per row."""
    init_schema(conn)
    name = path.name
    _detach_source(conn, name)
    preamble, rows = load_statement_rows_from_path(path)

    row_dates = [r.row_date for r in rows]
    min_d = min(row_dates).isoformat() if row_dates else None
    max_d = max(row_dates).isoformat() if row_dates else None
    now = datetime.now(UTC).replace(microsecond=0).isoformat()

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO files (name, imported_at, min_date, max_date, row_count)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            imported_at = excluded.imported_at,
            min_date = excluded.min_date,
            max_date = excluded.max_date,
            row_count = excluded.row_count
        """,
        (name, now, min_d, max_d, len(rows)),
    )

    new_fp = 0
    merged_sources = 0
    for r in rows:
        fp = transaction_fingerprint(r)
        cur.execute("SELECT sources_json FROM transactions WHERE fingerprint = ?", (fp,))
        found = cur.fetchone()
        desc = r.description
        amt = r.amount
        d_iso = r.row_date.isoformat()
        if found is None:
            rp = 1 if (amt is not None and amt < 0) else 0
            cur.execute(
                """
                INSERT INTO transactions (
                    fingerprint, post_date, amount, description, sources_json, retitle_pending
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (fp, d_iso, amt, desc, json.dumps([name]), rp),
            )
            new_fp += 1
        else:
            try:
                sources: list[str] = json.loads(found[0])
            except (json.JSONDecodeError, TypeError):
                sources = []
            if name not in sources:
                sources.append(name)
                cur.execute(
                    "UPDATE transactions SET sources_json = ? WHERE fingerprint = ?",
                    (json.dumps(sources), fp),
                )
                merged_sources += 1
    conn.commit()
    return {
        "file": name,
        "rows_read": len(rows),
        "new_fingerprints": new_fp,
        "source_reattached": merged_sources,
        "preamble_keys": len(preamble),
    }


def reindex_all_csvs(conn: sqlite3.Connection, financial_dir: Path) -> dict[str, Any]:
    init_schema(conn)
    cur = conn.cursor()
    cur.execute("DELETE FROM transactions")
    cur.execute("DELETE FROM files")
    conn.commit()
    paths = sorted(financial_dir.glob("*.csv"), key=lambda p: p.name.lower())
    per_file: list[dict[str, Any]] = []
    for p in paths:
        per_file.append(ingest_bank_csv(conn, p))
    return {"files": len(paths), "details": per_file}


def contributing_source_files(
    conn: sqlite3.Connection,
    start: date | None,
    end: date | None,
) -> list[str]:
    """Sorted unique CSV names that contributed any row in the optional inclusive date range."""
    init_schema(conn)
    cur = conn.cursor()
    if start is not None and end is not None:
        cur.execute(
            """
            SELECT sources_json FROM transactions
            WHERE post_date >= ? AND post_date <= ?
            """,
            (start.isoformat(), end.isoformat()),
        )
    elif start is not None:
        cur.execute(
            "SELECT sources_json FROM transactions WHERE post_date >= ?",
            (start.isoformat(),),
        )
    elif end is not None:
        cur.execute(
            "SELECT sources_json FROM transactions WHERE post_date <= ?",
            (end.isoformat(),),
        )
    else:
        cur.execute("SELECT sources_json FROM transactions")
    names: set[str] = set()
    for (sj,) in cur.fetchall():
        try:
            for n in json.loads(sj):
                if isinstance(n, str):
                    names.add(n)
        except (json.JSONDecodeError, TypeError):
            continue
    return sorted(names)


def fetch_statement_rows(
    conn: sqlite3.Connection,
    start: date | None,
    end: date | None,
) -> list[StatementRow]:
    init_schema(conn)
    cur = conn.cursor()
    if start is not None and end is not None:
        cur.execute(
            """
            SELECT post_date, amount, description
            FROM transactions
            WHERE post_date >= ? AND post_date <= ?
            ORDER BY post_date ASC, fingerprint ASC
            """,
            (start.isoformat(), end.isoformat()),
        )
    elif start is not None:
        cur.execute(
            """
            SELECT post_date, amount, description
            FROM transactions
            WHERE post_date >= ?
            ORDER BY post_date ASC, fingerprint ASC
            """,
            (start.isoformat(),),
        )
    elif end is not None:
        cur.execute(
            """
            SELECT post_date, amount, description
            FROM transactions
            WHERE post_date <= ?
            ORDER BY post_date ASC, fingerprint ASC
            """,
            (end.isoformat(),),
        )
    else:
        cur.execute(
            """
            SELECT post_date, amount, description
            FROM transactions
            ORDER BY post_date ASC, fingerprint ASC
            """
        )
    out: list[StatementRow] = []
    for post_date, amount, description in cur.fetchall():
        d = date.fromisoformat(str(post_date))
        out.append(StatementRow(d, str(description), amount, None))
    return out


def ledger_meta(conn: sqlite3.Connection) -> dict[str, Any]:
    init_schema(conn)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM transactions")
    tx_count = int(cur.fetchone()[0])
    cur.execute("SELECT MIN(post_date), MAX(post_date) FROM transactions")
    r = cur.fetchone()
    gmin, gmax = r[0], r[1]
    cur.execute(
        """
        SELECT name, imported_at, min_date, max_date, row_count
        FROM files
        ORDER BY name COLLATE NOCASE
        """
    )
    files: list[dict[str, Any]] = []
    for name, imported_at, min_date, max_date, row_count in cur.fetchall():
        files.append(
            {
                "name": name,
                "imported_at": imported_at,
                "min_date": min_date,
                "max_date": max_date,
                "row_count": int(row_count),
            }
        )
    cur.execute(
        """
        SELECT COUNT(*) FROM transactions
        WHERE amount IS NOT NULL AND amount < 0
        """
    )
    debit_total = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT COUNT(*) FROM transactions
        WHERE amount IS NOT NULL AND amount < 0 AND retitle_pending = 1
        """
    )
    retitle_pending_count = int(cur.fetchone()[0])
    return {
        "transaction_count": tx_count,
        "min_date": gmin,
        "max_date": gmax,
        "files": files,
        "ledger_debit_count": debit_total,
        "retitle_pending_count": retitle_pending_count,
        "retitle_complete_count": max(0, debit_total - retitle_pending_count),
    }

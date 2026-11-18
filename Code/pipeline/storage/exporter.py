"""Data Export Engine for the Twig Job Posting Pipeline.

Provides export capabilities for canonical job postings and change audit trails
to RFC 4180 compliant CSV and structured JSON formats with human-readable list
and salary range formatting.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

# ==============================================================================
# Constants & Default Schema Definitions
# ==============================================================================

# Core 17 schema fields defined in the assignment specification
SCHEMA_17_FIELDS: List[str] = [
    "posting_id",
    "company",
    "title",
    "seniority",
    "city",
    "country",
    "remote_policy",
    "salary_min",
    "salary_max",
    "currency",
    "employment_type",
    "skills",
    "visa_sponsorship",
    "posted_date",
    "status",
    "source_urls",
    "evidence",
]

# Standard canonical CSV headers including formatted helper fields and audit timestamps
DEFAULT_CANONICAL_CSV_HEADERS: List[str] = [
    "posting_id",
    "canonical_id",
    "company",
    "title",
    "seniority",
    "city",
    "country",
    "remote_policy",
    "salary_min",
    "salary_max",
    "salary_range",
    "currency",
    "employment_type",
    "skills",
    "visa_sponsorship",
    "posted_date",
    "status",
    "sources",
    "source_urls",
    "evidence",
    "first_seen",
    "last_seen",
]

# Standard change history audit trail CSV headers
DEFAULT_HISTORY_CSV_HEADERS: List[str] = [
    "history_id",
    "posting_id",
    "field_name",
    "old_value",
    "new_value",
    "detected_at",
    "reason",
]

# Currency symbols for human-readable salary range display
CURRENCY_SYMBOLS: Dict[str, str] = {
    "USD": "$",
    "CAD": "$",
    "AUD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "INR": "₹",
    "CHF": "CHF ",
}


# ==============================================================================
# Helper Formatting Utilities
# ==============================================================================


def format_skills_for_csv(skills: Any) -> str:
    """Formats skills list or JSON string into a clean comma-separated string.

    Args:
        skills: List of strings or JSON-encoded array string.

    Returns:
        Comma-separated string of skills (e.g. 'Python, SQL, Spark, dbt').
    """
    if not skills:
        return ""

    items: List[str] = []
    if isinstance(skills, str):
        skills_str = skills.strip()
        if skills_str.startswith("[") and skills_str.endswith("]"):
            try:
                parsed = json.loads(skills_str)
                if isinstance(parsed, list):
                    items = [str(x).strip() for x in parsed if str(x).strip()]
                else:
                    items = [skills_str]
            except Exception:
                items = [s.strip() for s in skills_str.split(",") if s.strip()]
        else:
            items = [s.strip() for s in skills_str.split(",") if s.strip()]
    elif isinstance(skills, (list, tuple, set)):
        items = [str(x).strip() for x in skills if str(x).strip()]
    else:
        items = [str(skills).strip()]

    return ", ".join(items)


def format_source_urls_for_csv(source_urls: Any) -> str:
    """Formats source URLs into a pipe-separated string.

    Args:
        source_urls: List of URL strings or JSON-encoded array string.

    Returns:
        Pipe-separated URLs string (e.g. 'https://... | https://...').
    """
    if not source_urls:
        return ""

    items: List[str] = []
    if isinstance(source_urls, str):
        urls_str = source_urls.strip()
        if urls_str.startswith("[") and urls_str.endswith("]"):
            try:
                parsed = json.loads(urls_str)
                if isinstance(parsed, list):
                    items = [str(x).strip() for x in parsed if str(x).strip()]
                else:
                    items = [urls_str]
            except Exception:
                items = [u.strip() for u in urls_str.split("|") if u.strip()]
        elif "|" in urls_str:
            items = [u.strip() for u in urls_str.split("|") if u.strip()]
        else:
            items = [urls_str]
    elif isinstance(source_urls, (list, tuple, set)):
        items = [str(x).strip() for x in source_urls if str(x).strip()]
    else:
        items = [str(source_urls).strip()]

    return " | ".join(items)


def format_sources_for_csv(sources: Any) -> str:
    """Formats scraper sources into a comma-separated string.

    Args:
        sources: List of source names or JSON-encoded array string.

    Returns:
        Comma-separated sources string (e.g. 'greenhouse, hackernews').
    """
    if not sources:
        return ""

    items: List[str] = []
    if isinstance(sources, str):
        sources_str = sources.strip()
        if sources_str.startswith("[") and sources_str.endswith("]"):
            try:
                parsed = json.loads(sources_str)
                if isinstance(parsed, list):
                    items = [str(x).strip() for x in parsed if str(x).strip()]
                else:
                    items = [sources_str]
            except Exception:
                items = [s.strip() for s in sources_str.split(",") if s.strip()]
        else:
            items = [s.strip() for s in sources_str.split(",") if s.strip()]
    elif isinstance(sources, (list, tuple, set)):
        items = [str(x).strip() for x in sources if str(x).strip()]
    else:
        items = [str(sources).strip()]

    return ", ".join(items)


def format_salary_range(
    salary_min: Optional[Union[float, int]],
    salary_max: Optional[Union[float, int]],
    currency: Optional[str] = None,
) -> str:
    """Formats minimum and maximum salary figures into a human-readable range.

    Examples:
        - (160000, 220000, 'USD') -> '$160,000 - $220,000 USD'
        - (150000, 150000, 'USD') -> '$150,000 USD'
        - (120000, None, 'USD')   -> '$120,000+ USD'
        - (None, 180000, 'USD')   -> 'Up to $180,000 USD'
        - (None, None, 'USD')     -> ''

    Args:
        salary_min: Numeric minimum annualized salary.
        salary_max: Numeric maximum annualized salary.
        currency: ISO-4217 currency code (e.g. 'USD').

    Returns:
        Formatted salary range string.
    """
    if salary_min is None and salary_max is None:
        return ""

    curr = (currency or "").strip().upper()
    symbol = CURRENCY_SYMBOLS.get(curr, "$")
    suffix = f" {curr}" if curr else ""

    def _fmt(val: Union[int, float]) -> str:
        f_val = float(val)
        if f_val.is_integer():
            return f"{int(f_val):,}"
        return f"{f_val:,.2f}"

    if salary_min is not None and salary_max is not None:
        min_fmt = _fmt(salary_min)
        max_fmt = _fmt(salary_max)
        if float(salary_min) == float(salary_max):
            return f"{symbol}{min_fmt}{suffix}"
        return f"{symbol}{min_fmt} - {symbol}{max_fmt}{suffix}"

    if salary_min is not None:
        return f"{symbol}{_fmt(salary_min)}+{suffix}"

    return f"Up to {symbol}{_fmt(salary_max)}{suffix}"


def _parse_json_field(val: Any, default: Any) -> Any:
    """Helper to safely decode a JSON or stringified value."""
    if val is None:
        return default
    if isinstance(val, (list, dict)):
        return val
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str:
            return default
        try:
            return json.loads(val_str)
        except Exception:
            return val
    return val


def format_file_size(num_bytes: int) -> str:
    """Formats byte count into human-readable size string (B, KB, MB, GB).

    Args:
        num_bytes: Total file size in bytes.

    Returns:
        Formatted string (e.g. '18.4 KB', '2.1 MB').
    """
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{num_bytes / (1024 * 1024 * 1024):.1f} GB"


# ==============================================================================
# Data Exporter Engine
# ==============================================================================


class DataExporter:
    """High-performance export engine for canonical job postings and audit trails.

    Supports export to RFC 4180 compliant CSV and structured JSON files,
    with flexible status filtering, custom schema column selection, and
    Slowly Changing Dimensions (SCD) audit history reporting.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        """Initialize DataExporter with an optional default database path."""
        self.db_path = str(db_path) if db_path else None

    @classmethod
    def _resolve_connection(
        cls,
        conn: Union[sqlite3.Connection, str, Path],
    ) -> tuple[sqlite3.Connection, bool]:
        """Resolves conn parameter to an open connection and owns_conn flag."""
        if isinstance(conn, sqlite3.Connection):
            return conn, False
        if isinstance(conn, (str, Path)):
            c = sqlite3.connect(str(conn), timeout=30.0)
            return c, True
        raise TypeError(f"Expected sqlite3.Connection or database path, got {type(conn)}")

    @classmethod
    def _table_exists(cls, conn: sqlite3.Connection, table_name: str) -> bool:
        """Checks if a specified table exists in the database."""
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        return cur.fetchone() is not None

    @classmethod
    def export_canonical_jobs_to_csv(
        cls,
        conn: Union[sqlite3.Connection, str, Path],
        output_path: Union[str, Path],
        status_filter: Optional[str] = None,
        columns: Optional[Sequence[str]] = None,
    ) -> int:
        """Queries canonical_job_postings and exports to RFC 4180 compliant CSV.

        Formats list and dictionary fields cleanly:
        - skills: comma-separated string (e.g. 'Python, SQL, Spark, dbt')
        - source_urls: pipe-separated URLs ('https://... | https://...')
        - sources: comma-separated sources ('greenhouse, hackernews')
        - salary_range: formatted string ('$160,000 - $220,000 USD') alongside
          numeric salary_min and salary_max

        Args:
            conn: Active sqlite3.Connection or path to SQLite database.
            output_path: Target filesystem path for the CSV output.
            status_filter: Optional filter ('open', 'closed', or 'all').
            columns: Optional list of headers to export (defaults to all canonical fields).

        Returns:
            Total row count exported (excluding header row).
        """
        active_conn, owns_conn = cls._resolve_connection(conn)
        target_path = Path(output_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        headers = list(columns) if columns is not None else list(DEFAULT_CANONICAL_CSV_HEADERS)

        try:
            if not cls._table_exists(active_conn, "canonical_job_postings"):
                logger.warning("Table 'canonical_job_postings' does not exist in database.")
                with open(target_path, mode="w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                    writer.writerow(headers)
                return 0

            # Construct query with optional status filtering
            query = "SELECT * FROM canonical_job_postings"
            params: List[Any] = []

            if status_filter:
                norm_status = status_filter.strip().lower()
                if norm_status in ("open", "closed"):
                    query += " WHERE LOWER(status) = ?"
                    params.append(norm_status)

            query += " ORDER BY company ASC, title ASC"

            cur = active_conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall()
            col_names = [d[0] for d in cur.description] if cur.description else []

            total_exported = 0
            with open(target_path, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                writer.writerow(headers)

                for raw_row in rows:
                    row_dict = dict(zip(col_names, raw_row))

                    # Resolve identifiers
                    raw_id = row_dict.get("canonical_id") or row_dict.get("posting_id") or ""
                    salary_min = row_dict.get("salary_min")
                    salary_max = row_dict.get("salary_max")
                    currency = row_dict.get("currency")
                    salary_range = format_salary_range(salary_min, salary_max, currency)

                    # Format collections
                    formatted_skills = format_skills_for_csv(row_dict.get("skills"))
                    formatted_sources = format_sources_for_csv(row_dict.get("sources"))
                    formatted_urls = format_source_urls_for_csv(row_dict.get("source_urls"))

                    # Format visa sponsorship
                    visa_raw = row_dict.get("visa_sponsorship")
                    if visa_raw is True or visa_raw == 1 or str(visa_raw).lower() in ("true", "1"):
                        visa_str = "true"
                    elif visa_raw is False or visa_raw == 0 or str(visa_raw).lower() in ("false", "0"):
                        visa_str = "false"
                    else:
                        visa_str = ""

                    # Format evidence JSON
                    evidence_val = row_dict.get("evidence")
                    if isinstance(evidence_val, dict):
                        evidence_str = json.dumps(evidence_val, ensure_ascii=False)
                    elif isinstance(evidence_val, str) and evidence_val.strip():
                        evidence_str = evidence_val.strip()
                    else:
                        evidence_str = "{}"

                    # Build row dictionary mapping column -> formatted string
                    row_data: Dict[str, Any] = {
                        "posting_id": raw_id,
                        "canonical_id": raw_id,
                        "company": row_dict.get("company") or "",
                        "title": row_dict.get("title") or "",
                        "seniority": row_dict.get("seniority") or "",
                        "city": row_dict.get("city") or "",
                        "country": row_dict.get("country") or "",
                        "remote_policy": row_dict.get("remote_policy") or "",
                        "salary_min": salary_min if salary_min is not None else "",
                        "salary_max": salary_max if salary_max is not None else "",
                        "salary_range": salary_range,
                        "currency": currency or "",
                        "employment_type": row_dict.get("employment_type") or "",
                        "skills": formatted_skills,
                        "visa_sponsorship": visa_str,
                        "posted_date": row_dict.get("posted_date") or "",
                        "status": row_dict.get("status") or "open",
                        "sources": formatted_sources,
                        "source_urls": formatted_urls,
                        "evidence": evidence_str,
                        "first_seen": str(row_dict.get("first_seen") or ""),
                        "last_seen": str(row_dict.get("last_seen") or ""),
                    }

                    # Extract row values matching selected headers
                    row_values = [row_data.get(h, row_dict.get(h, "")) for h in headers]
                    writer.writerow(row_values)
                    total_exported += 1

            logger.info("Exported %d canonical jobs to CSV: %s", total_exported, target_path)
            return total_exported
        finally:
            if owns_conn:
                active_conn.close()

    @classmethod
    def export_canonical_jobs_to_json(
        cls,
        conn: Union[sqlite3.Connection, str, Path],
        output_path: Union[str, Path],
        status_filter: Optional[str] = None,
        indent: int = 2,
    ) -> int:
        """Exports full JSON array of canonical records with typed collections.

        Decodes stringified JSON fields (skills, sources, source_urls, evidence)
        back into native Python lists and dictionaries.

        Args:
            conn: Active sqlite3.Connection or path to SQLite database.
            output_path: Target filesystem path for the JSON output.
            status_filter: Optional filter ('open', 'closed', or 'all').
            indent: JSON indentation spacing (default 2).

        Returns:
            Total records exported.
        """
        active_conn, owns_conn = cls._resolve_connection(conn)
        target_path = Path(output_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if not cls._table_exists(active_conn, "canonical_job_postings"):
                logger.warning("Table 'canonical_job_postings' does not exist in database.")
                with open(target_path, mode="w", encoding="utf-8") as f:
                    json.dump([], f, indent=indent, ensure_ascii=False)
                return 0

            query = "SELECT * FROM canonical_job_postings"
            params: List[Any] = []

            if status_filter:
                norm_status = status_filter.strip().lower()
                if norm_status in ("open", "closed"):
                    query += " WHERE LOWER(status) = ?"
                    params.append(norm_status)

            query += " ORDER BY company ASC, title ASC"

            cur = active_conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall()
            col_names = [d[0] for d in cur.description] if cur.description else []

            records: List[Dict[str, Any]] = []
            for raw_row in rows:
                row_dict = dict(zip(col_names, raw_row))

                raw_id = row_dict.get("canonical_id") or row_dict.get("posting_id") or ""
                salary_min = row_dict.get("salary_min")
                salary_max = row_dict.get("salary_max")
                currency = row_dict.get("currency")
                salary_range = format_salary_range(salary_min, salary_max, currency)

                # Decode JSON collections
                skills = _parse_json_field(row_dict.get("skills"), [])
                sources = _parse_json_field(row_dict.get("sources"), [])
                source_urls = _parse_json_field(row_dict.get("source_urls"), [])
                source_posting_ids = _parse_json_field(row_dict.get("source_posting_ids"), [])
                evidence = _parse_json_field(row_dict.get("evidence"), {})

                # Format visa sponsorship boolean
                visa_raw = row_dict.get("visa_sponsorship")
                if visa_raw is True or visa_raw == 1 or str(visa_raw).lower() in ("true", "1"):
                    visa_bool: Optional[bool] = True
                elif visa_raw is False or visa_raw == 0 or str(visa_raw).lower() in ("false", "0"):
                    visa_bool = False
                else:
                    visa_bool = None

                rec: Dict[str, Any] = {
                    "posting_id": raw_id,
                    "canonical_id": raw_id,
                    "company": row_dict.get("company"),
                    "title": row_dict.get("title"),
                    "seniority": row_dict.get("seniority"),
                    "city": row_dict.get("city"),
                    "country": row_dict.get("country"),
                    "remote_policy": row_dict.get("remote_policy"),
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                    "salary_range": salary_range,
                    "currency": currency,
                    "employment_type": row_dict.get("employment_type"),
                    "skills": skills if isinstance(skills, list) else [str(skills)],
                    "visa_sponsorship": visa_bool,
                    "posted_date": row_dict.get("posted_date"),
                    "status": row_dict.get("status", "open"),
                    "sources": sources if isinstance(sources, list) else [str(sources)],
                    "source_urls": source_urls if isinstance(source_urls, list) else [str(source_urls)],
                    "source_posting_ids": source_posting_ids if isinstance(source_posting_ids, list) else [],
                    "evidence": evidence if isinstance(evidence, dict) else {},
                    "first_seen": str(row_dict.get("first_seen") or ""),
                    "last_seen": str(row_dict.get("last_seen") or ""),
                    "duplicate_count": int(row_dict.get("duplicate_count", 1)),
                }
                records.append(rec)

            with open(target_path, mode="w", encoding="utf-8") as f:
                json.dump(records, f, indent=indent, ensure_ascii=False)

            logger.info("Exported %d canonical jobs to JSON: %s", len(records), target_path)
            return len(records)
        finally:
            if owns_conn:
                active_conn.close()

    @classmethod
    def export_change_history(
        cls,
        conn: Union[sqlite3.Connection, str, Path],
        output_path: Union[str, Path],
    ) -> int:
        """Exports complete audit trail from posting_change_history to RFC 4180 CSV.

        Args:
            conn: Active sqlite3.Connection or path to SQLite database.
            output_path: Target filesystem path for the CSV output.

        Returns:
            Total audit log rows exported.
        """
        active_conn, owns_conn = cls._resolve_connection(conn)
        target_path = Path(output_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        headers = list(DEFAULT_HISTORY_CSV_HEADERS)

        try:
            # Check for table existence
            table_name: Optional[str] = None
            if cls._table_exists(active_conn, "posting_change_history"):
                table_name = "posting_change_history"
            elif cls._table_exists(active_conn, "change_history"):
                table_name = "change_history"

            if table_name is None:
                logger.warning("No change history table found in database.")
                with open(target_path, mode="w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                    writer.writerow(headers)
                return 0

            cur = active_conn.cursor()
            cur.execute(f"SELECT * FROM {table_name} ORDER BY 1 ASC")
            rows = cur.fetchall()
            col_names = [d[0] for d in cur.description] if cur.description else []

            total_exported = 0
            with open(target_path, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                writer.writerow(headers)

                for raw_row in rows:
                    row_dict = dict(zip(col_names, raw_row))

                    # Normalize id / history_id
                    hist_id = row_dict.get("history_id")
                    if hist_id is None:
                        hist_id = row_dict.get("id", "")

                    row_values = [
                        hist_id,
                        row_dict.get("posting_id") or row_dict.get("canonical_id") or "",
                        row_dict.get("field_name") or "",
                        row_dict.get("old_value") or "",
                        row_dict.get("new_value") or "",
                        str(row_dict.get("detected_at") or ""),
                        row_dict.get("reason") or "",
                    ]
                    writer.writerow(row_values)
                    total_exported += 1

            logger.info("Exported %d change history records to CSV: %s", total_exported, target_path)
            return total_exported
        finally:
            if owns_conn:
                active_conn.close()

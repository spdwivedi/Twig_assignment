"""Idempotent Upsert and Storage Engine for Canonical Job Postings.

Coordinates database persistence, field-level change detection, audit logging,
and lifecycle management to ensure strict idempotency and zero duplicate data
across repeated pipeline execution passes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from pipeline.models.canonical import CanonicalJobPosting, generate_canonical_id
from pipeline.models.schema import (
    EmploymentTypeEnum,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)
from pipeline.storage.change_tracker import ChangeTracker, FieldChange
from pipeline.storage.database import (
    DatabaseManager,
    configure_connection,
    create_connection,
    init_db,
)
from pipeline.storage.lifecycle_manager import LifecycleManager

logger = logging.getLogger(__name__)


# ==============================================================================
# Helper Serialization Utilities
# ==============================================================================


def _clean_enum_val(val: Any) -> Optional[str]:
    """Extract string value from enum or string input."""
    if val is None:
        return None
    if hasattr(val, "value"):
        return str(val.value)
    s = str(val).strip()
    return s if s else None


def _clean_visa_int(val: Any) -> Optional[int]:
    """Coerce boolean visa sponsorship to SQLite integer (1, 0, or None)."""
    if val is True:
        return 1
    if val is False:
        return 0
    if val in (1, "1"):
        return 1
    if val in (0, "0"):
        return 0
    return None


def _clean_json_list(val: Any) -> str:
    """Serializes collection or string to a canonical JSON array string."""
    if not val:
        return json.dumps([])
    if isinstance(val, str):
        s = val.strip()
        if (s.startswith("[") and s.endswith("]")) or (s.startswith("(") and s.endswith(")")):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    clean = [str(x).strip() for x in parsed if str(x).strip()]
                    return json.dumps(clean)
            except Exception:
                pass
        parts = [p.strip() for p in s.split(",") if p.strip()]
        return json.dumps(parts)
    if isinstance(val, (list, tuple, set)):
        clean = [str(x).strip() for x in val if str(x).strip()]
        return json.dumps(clean)
    return json.dumps([str(val).strip()])


def _clean_json_dict(val: Any) -> str:
    """Serializes dict or string to a canonical JSON object string."""
    if not val:
        return json.dumps({})
    if isinstance(val, dict):
        return json.dumps(val, sort_keys=True)
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return json.dumps(parsed, sort_keys=True)
            except Exception:
                pass
        return json.dumps({"raw": s})
    return json.dumps({})


def _parse_list(val: Any) -> List[str]:
    """Parses JSON string or collection into a python list of strings."""
    if not val:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, (set, tuple)):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                pass
        return [part.strip() for part in s.split(",") if part.strip()]
    return [str(val).strip()]


def _parse_dict(val: Any) -> Dict[str, Any]:
    """Parses JSON string or dict into a python dictionary."""
    if not val:
        return {}
    if isinstance(val, dict):
        return dict(val)
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return {}


# ==============================================================================
# IdempotentStorageEngine Class
# ==============================================================================


class IdempotentStorageEngine:
    """Transactional, idempotent storage engine for canonical job postings.

    Coordinates database persistence, field-level change detection, audit logging,
    and lifecycle management to ensure zero duplicate records, zero false change
    logs, and strictly idempotent re-runs.
    """

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        database_manager: Optional[DatabaseManager] = None,
        change_tracker: Optional[ChangeTracker] = None,
        lifecycle_manager: Optional[LifecycleManager] = None,
        table_name: str = "canonical_job_postings",
        history_table: str = "posting_change_history",
        float_tolerance: float = 1.0,
    ) -> None:
        """Initialize the IdempotentStorageEngine.

        Args:
            db_path: Optional database path or ':memory:'.
            database_manager: Optional DatabaseManager connection pool instance.
            change_tracker: Optional ChangeTracker instance for field diffing.
            lifecycle_manager: Optional LifecycleManager instance for closure/reopen handling.
            table_name: Canonical table name (default 'canonical_job_postings').
            history_table: Change audit history table name (default 'posting_change_history').
            float_tolerance: Numeric tolerance for salary diff detection.
        """
        self.db_path = str(db_path) if db_path is not None else None
        self.table_name = table_name
        self.history_table = history_table
        self.float_tolerance = float_tolerance

        # 1. Database Manager Integration
        if database_manager is not None:
            self.database_manager = database_manager
        elif self.db_path is not None:
            self.database_manager = DatabaseManager(db_path=self.db_path)
        else:
            self.database_manager = None

        # 2. Change Tracker Integration
        if change_tracker is not None:
            self.change_tracker = change_tracker
        else:
            self.change_tracker = ChangeTracker(float_tolerance=self.float_tolerance)

        # 3. Lifecycle Manager Integration
        if lifecycle_manager is not None:
            self.lifecycle_manager = lifecycle_manager
        else:
            self.lifecycle_manager = LifecycleManager(
                table_name=self.table_name,
                history_table=self.history_table,
            )

    # --------------------------------------------------------------------------
    # Database Introspection & Schema Helpers
    # --------------------------------------------------------------------------

    def ensure_tables(self, conn: sqlite3.Connection) -> None:
        """Ensures that all tables, columns, and performance indices exist."""
        init_db(conn=conn)
        self.change_tracker.init_table(conn)
        self.lifecycle_manager.ensure_tables(conn)

    def _get_table_columns(self, conn: sqlite3.Connection, table_name: str) -> List[str]:
        """Returns list of column names for the specified table."""
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name});")
        return [row[1] for row in cursor.fetchall()]

    def _get_id_column(self, conn: sqlite3.Connection, table_name: str) -> str:
        """Determines whether posting_id or canonical_id is the primary key column."""
        cols = self._get_table_columns(conn, table_name)
        if "posting_id" in cols:
            return "posting_id"
        if "canonical_id" in cols:
            return "canonical_id"
        return "posting_id"

    # --------------------------------------------------------------------------
    # Core Idempotent Upsert Logic
    # --------------------------------------------------------------------------

    def upsert_canonical_records(
        self,
        conn: sqlite3.Connection,
        records: List[Union[CanonicalJobPosting, Dict[str, Any]]],
        current_timestamp: Optional[datetime] = None,
    ) -> Dict[str, int]:
        """Executes a transactional batch upsert of canonical records with strict idempotency.

        Behavior:
        - Case 1: Brand new canonical posting ID:
          - Inserts record into `canonical_job_postings`.
          - Sets status = 'open', first_seen = now, last_seen = now, duplicate_count = len(cluster).
          - Increments `inserted` count.
        - Case 2: Existing canonical posting ID:
          - Fetches existing row from `canonical_job_postings`.
          - Compares tracked fields via `ChangeTracker.detect_changes(existing, incoming)`.
          - If changes detected:
            - Logs each changed field to `posting_change_history`.
            - Updates changed fields, merges sources/evidence, updates last_seen = now.
            - Updates duplicate_count = max(existing.duplicate_count, incoming.duplicate_count).
            - Increments `updated` count and `changes_logged` count.
          - If NO changes detected:
            - Updates ONLY `last_seen = now` (and duplicate_count).
            - Inserts 0 rows into `posting_change_history`.
            - Increments `unchanged` count.

        Args:
            conn: Active SQLite connection (foreign keys ON, configured).
            records: List of CanonicalJobPosting instances or equivalent dictionaries.
            current_timestamp: Optional explicit datetime for observation timestamps.

        Returns:
            Dictionary with counts:
            {"inserted": int, "updated": int, "unchanged": int, "changes_logged": int}
        """
        if not records:
            return {
                "inserted": 0,
                "updated": 0,
                "unchanged": 0,
                "changes_logged": 0,
            }

        self.ensure_tables(conn)

        # Standardize observation timestamp
        if current_timestamp is not None:
            now_dt = current_timestamp
            now_iso = (
                current_timestamp.isoformat()
                if hasattr(current_timestamp, "isoformat")
                else str(current_timestamp)
            )
        else:
            now_dt = datetime.now(timezone.utc)
            now_iso = now_dt.isoformat()

        id_col = self._get_id_column(conn, self.table_name)
        table_cols = self._get_table_columns(conn, self.table_name)
        table_cols_set = set(table_cols)

        inserted_count = 0
        updated_count = 0
        unchanged_count = 0
        changes_logged_count = 0

        # Entire batch executes within a single atomic database transaction
        with conn:
            cursor = conn.cursor()

            for rec in records:
                # 1. Normalize record data
                if isinstance(rec, dict):
                    data = dict(rec)
                elif hasattr(rec, "model_dump"):
                    data = rec.model_dump()
                else:
                    data = getattr(rec, "__dict__", {})

                # Extract or generate stable deterministic canonical ID
                rec_id = (
                    data.get("canonical_id")
                    or data.get("posting_id")
                    or generate_canonical_id(
                        company=data.get("company", ""),
                        title=data.get("title", ""),
                        location=data.get("city", "") or data.get("location", "") or "",
                    )
                )

                # 2. Check if posting already exists in target table
                cursor.execute(
                    f"SELECT * FROM {self.table_name} WHERE {id_col} = ?",
                    (rec_id,),
                )
                existing_row = cursor.fetchone()

                # Clean fields for database persistence
                company = str(data.get("company", "")).strip()
                title = str(data.get("title", "")).strip()
                seniority = _clean_enum_val(data.get("seniority"))
                city = str(data.get("city")).strip() if data.get("city") else None
                country = str(data.get("country")).strip() if data.get("country") else None
                remote_policy = _clean_enum_val(data.get("remote_policy"))
                currency = str(data.get("currency")).strip().upper() if data.get("currency") else None
                employment_type = _clean_enum_val(data.get("employment_type"))
                visa_int = _clean_visa_int(data.get("visa_sponsorship"))

                sal_min = data.get("salary_min")
                sal_min_f = float(sal_min) if sal_min is not None else None

                sal_max = data.get("salary_max")
                sal_max_f = float(sal_max) if sal_max is not None else None

                posted_date = data.get("posted_date")
                if hasattr(posted_date, "isoformat"):
                    posted_date_str = posted_date.isoformat()
                elif posted_date:
                    posted_date_str = str(posted_date)
                else:
                    posted_date_str = None

                skills_json = _clean_json_list(data.get("skills"))
                sources_list = _parse_list(data.get("sources"))
                sources_json = _clean_json_list(sources_list)
                source_urls_list = _parse_list(data.get("source_urls"))
                source_urls_json = _clean_json_list(source_urls_list)
                source_posting_ids_list = _parse_list(data.get("source_posting_ids"))
                source_posting_ids_json = _clean_json_list(source_posting_ids_list)
                evidence_dict = _parse_dict(data.get("evidence"))
                evidence_json = _clean_json_dict(evidence_dict)

                status_val = _clean_enum_val(data.get("status")) or "open"
                incoming_dup = int(data.get("duplicate_count") or len(source_posting_ids_list) or 1)
                incoming_dup = max(1, incoming_dup)

                if existing_row is None:
                    # ==========================================================
                    # CASE 1: Brand New Canonical Posting ID
                    # ==========================================================
                    insert_dict: Dict[str, Any] = {
                        id_col: rec_id,
                        "company": company,
                        "title": title,
                        "seniority": seniority,
                        "city": city,
                        "country": country,
                        "remote_policy": remote_policy,
                        "salary_min": sal_min_f,
                        "salary_max": sal_max_f,
                        "currency": currency,
                        "employment_type": employment_type,
                        "skills": skills_json,
                        "visa_sponsorship": visa_int,
                        "posted_date": posted_date_str,
                        "status": status_val,
                        "sources": sources_json,
                        "source_urls": source_urls_json,
                        "source_posting_ids": source_posting_ids_json,
                        "evidence": evidence_json,
                        "first_seen": now_iso,
                        "last_seen": now_iso,
                        "duplicate_count": incoming_dup,
                    }

                    if "created_at" in table_cols_set:
                        insert_dict["created_at"] = now_iso
                    if "updated_at" in table_cols_set:
                        insert_dict["updated_at"] = now_iso

                    # Build dynamic insert query adhering to active table columns
                    valid_cols = [c for c in table_cols if c in insert_dict]
                    placeholders = ", ".join(["?"] * len(valid_cols))
                    col_str = ", ".join(valid_cols)
                    cursor.execute(
                        f"INSERT INTO {self.table_name} ({col_str}) VALUES ({placeholders})",
                        [insert_dict[c] for c in valid_cols],
                    )

                    inserted_count += 1
                    logger.debug("Inserted new canonical job posting: %s", rec_id)

                else:
                    # ==========================================================
                    # CASE 2: Existing Canonical Posting ID
                    # ==========================================================
                    existing_dict = dict(existing_row)

                    # Invoke ChangeTracker to detect field modifications
                    changes = self.change_tracker.detect_changes(existing_dict, data)

                    if changes:
                        # ------------------------------------------------------
                        # Subcase 2A: Changes Detected
                        # ------------------------------------------------------
                        # 1. Log each changed field to posting_change_history
                        logged = self.change_tracker.log_changes(
                            conn=conn,
                            posting_id=rec_id,
                            changes=changes,
                            timestamp=now_dt,
                        )
                        changes_logged_count += logged

                        # 2. Merge multi-source collections and update canonical posting
                        existing_sources = _parse_list(existing_dict.get("sources"))
                        merged_sources = sorted(list(dict.fromkeys(existing_sources + sources_list)))

                        existing_urls = _parse_list(existing_dict.get("source_urls"))
                        merged_urls = sorted(list(dict.fromkeys(existing_urls + source_urls_list)))

                        existing_pids = _parse_list(existing_dict.get("source_posting_ids"))
                        merged_pids = sorted(list(dict.fromkeys(existing_pids + source_posting_ids_list)))

                        existing_ev = _parse_dict(existing_dict.get("evidence"))
                        merged_ev = {**existing_ev, **evidence_dict}

                        existing_dup = int(existing_dict.get("duplicate_count") or 1)
                        merged_dup = max(existing_dup, incoming_dup)

                        update_dict: Dict[str, Any] = {
                            "company": company,
                            "title": title,
                            "seniority": seniority,
                            "city": city,
                            "country": country,
                            "remote_policy": remote_policy,
                            "salary_min": sal_min_f,
                            "salary_max": sal_max_f,
                            "currency": currency,
                            "employment_type": employment_type,
                            "skills": skills_json,
                            "visa_sponsorship": visa_int,
                            "posted_date": posted_date_str,
                            "status": status_val,
                            "sources": _clean_json_list(merged_sources),
                            "source_urls": _clean_json_list(merged_urls),
                            "source_posting_ids": _clean_json_list(merged_pids),
                            "evidence": _clean_json_dict(merged_ev),
                            "last_seen": now_iso,
                            "duplicate_count": merged_dup,
                        }

                        if "updated_at" in table_cols_set:
                            update_dict["updated_at"] = now_iso

                        # Execute UPDATE query
                        update_cols = [
                            c for c in table_cols
                            if c in update_dict and c not in (id_col, "first_seen", "created_at")
                        ]
                        set_clause = ", ".join([f"{c} = ?" for c in update_cols])
                        cursor.execute(
                            f"UPDATE {self.table_name} SET {set_clause} WHERE {id_col} = ?",
                            [update_dict[c] for c in update_cols] + [rec_id],
                        )

                        updated_count += 1
                        logger.debug("Updated canonical posting with %d change(s): %s", len(changes), rec_id)

                    else:
                        # ------------------------------------------------------
                        # Subcase 2B: NO Changes Detected (Strict Idempotency)
                        # ------------------------------------------------------
                        # Update ONLY last_seen (and duplicate_count if larger)
                        existing_dup = int(existing_dict.get("duplicate_count") or 1)
                        merged_dup = max(existing_dup, incoming_dup)

                        if "duplicate_count" in table_cols_set:
                            cursor.execute(
                                f"""
                                UPDATE {self.table_name}
                                SET last_seen = ?,
                                    duplicate_count = ?
                                WHERE {id_col} = ?
                                """,
                                (now_iso, merged_dup, rec_id),
                            )
                        else:
                            cursor.execute(
                                f"UPDATE {self.table_name} SET last_seen = ? WHERE {id_col} = ?",
                                (now_iso, rec_id),
                            )

                        unchanged_count += 1
                        logger.debug("Posting unchanged, refreshed last_seen: %s", rec_id)

        summary = {
            "inserted": inserted_count,
            "updated": updated_count,
            "unchanged": unchanged_count,
            "changes_logged": changes_logged_count,
        }

        logger.info(
            "Completed upsert: %d inserted, %d updated, %d unchanged, %d changes logged",
            inserted_count,
            updated_count,
            unchanged_count,
            changes_logged_count,
        )
        return summary

    # --------------------------------------------------------------------------
    # Idempotency Verification Suite
    # --------------------------------------------------------------------------

    def verify_idempotency(
        self,
        conn: sqlite3.Connection,
        records: List[Union[CanonicalJobPosting, Dict[str, Any]]],
    ) -> bool:
        """Runs batch upsert twice consecutively to strictly verify pipeline idempotency.

        Asserts that on Run 2:
        - `inserted == 0`
        - `updated == 0` (no false field diffs)
        - `unchanged == len(records)`
        - `changes_logged == 0`
        - Total row count in `canonical_job_postings` remains identical.

        Args:
            conn: Active SQLite connection.
            records: List of CanonicalJobPosting instances.

        Returns:
            True if all idempotency assertions pass.

        Raises:
            AssertionError: If any idempotency invariant is violated.
        """
        # Run 1: Primary Upsert Execution
        self.upsert_canonical_records(conn=conn, records=records)

        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {self.table_name}")
        row_count_after_run1 = cursor.fetchone()[0]

        # Run 2: Consecutive Re-run with Identical Data
        res2 = self.upsert_canonical_records(conn=conn, records=records)

        cursor.execute(f"SELECT COUNT(*) FROM {self.table_name}")
        row_count_after_run2 = cursor.fetchone()[0]

        # Assert strict idempotency guarantees
        assert res2["inserted"] == 0, (
            f"Idempotency violation: Expected 0 inserts on Run 2, got {res2['inserted']}"
        )
        assert res2["updated"] == 0, (
            f"Idempotency violation: Expected 0 updates on Run 2, got {res2['updated']}"
        )
        assert res2["unchanged"] == len(records), (
            f"Idempotency violation: Expected {len(records)} unchanged on Run 2, got {res2['unchanged']}"
        )
        assert res2["changes_logged"] == 0, (
            f"Idempotency violation: Expected 0 changes logged on Run 2, got {res2['changes_logged']}"
        )
        assert row_count_after_run1 == row_count_after_run2, (
            f"Idempotency violation: Table row count mutated between runs: "
            f"{row_count_after_run1} -> {row_count_after_run2}"
        )

        return True

    # --------------------------------------------------------------------------
    # Inspection & Retrieval Convenience Helpers
    # --------------------------------------------------------------------------

    def get_canonical_record(
        self,
        conn: sqlite3.Connection,
        posting_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Retrieves a single canonical job posting record by ID."""
        id_col = self._get_id_column(conn, self.table_name)
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM {self.table_name} WHERE {id_col} = ?",
            (posting_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_all_canonical_records(
        self,
        conn: sqlite3.Connection,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieves all canonical job postings from target table."""
        query = f"SELECT * FROM {self.table_name} ORDER BY company, title"
        params: List[Any] = []
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]

    def get_change_history(
        self,
        conn: sqlite3.Connection,
        posting_id: Optional[str] = None,
        field_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieves audit change logs from posting_change_history."""
        return self.change_tracker.get_history(
            conn=conn,
            posting_id=posting_id,
            field_name=field_name,
        )

    def get_total_records_count(self, conn: sqlite3.Connection) -> int:
        """Returns total row count in the canonical postings table."""
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {self.table_name}")
        row = cursor.fetchone()
        return row[0] if row else 0

    def upsert(
        self,
        records: List[Union[CanonicalJobPosting, Dict[str, Any]]],
        current_timestamp: Optional[datetime] = None,
    ) -> Dict[str, int]:
        """Convenience wrapper acquiring connection from database_manager if available."""
        if self.database_manager is None:
            raise RuntimeError(
                "IdempotentStorageEngine was not initialized with a DatabaseManager or db_path."
            )
        with self.database_manager.get_connection(auto_commit=True) as conn:
            return self.upsert_canonical_records(
                conn=conn,
                records=records,
                current_timestamp=current_timestamp,
            )

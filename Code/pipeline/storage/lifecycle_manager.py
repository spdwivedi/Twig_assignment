"""Lifecycle and Closure Detection Manager for Canonical Job Postings.

Tracks job posting lifecycle transitions ('open' vs 'closed'), manages source-level
deprecation for multi-source canonical records, records comprehensive audit trails
in posting_change_history, and handles posting reopening events upon re-crawl.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Union

logger = logging.getLogger(__name__)


def _parse_sources(val: Any) -> List[str]:
    """Parses a sources field from SQLite (JSON string, CSV, or raw string) into a lowercase list."""
    if not val:
        return []
    if isinstance(val, (list, tuple, set)):
        return [str(s).strip().lower() for s in val if str(s).strip()]
    if isinstance(val, str):
        val_str = val.strip()
        if val_str.startswith("[") and val_str.endswith("]"):
            try:
                parsed = json.loads(val_str)
                if isinstance(parsed, list):
                    return [str(s).strip().lower() for s in parsed if str(s).strip()]
            except Exception:
                pass
        # Fallback for comma or semicolon delimited strings
        clean_s = val_str.replace(";", ",")
        return [s.strip().lower() for s in clean_s.split(",") if s.strip()]
    return [str(val).strip().lower()]


def _parse_ids(val: Any) -> List[str]:
    """Parses a list of source posting IDs from SQLite into a list of strings."""
    if not val:
        return []
    if isinstance(val, (list, tuple, set)):
        return [str(s).strip() for s in val if str(s).strip()]
    if isinstance(val, str):
        val_str = val.strip()
        if val_str.startswith("[") and val_str.endswith("]"):
            try:
                parsed = json.loads(val_str)
                if isinstance(parsed, list):
                    return [str(s).strip() for s in parsed if str(s).strip()]
            except Exception:
                pass
        clean_s = val_str.replace(";", ",")
        return [s.strip() for s in clean_s.split(",") if s.strip()]
    return [str(val).strip()]


def _to_iso_timestamp(ts: Optional[Union[datetime, str]]) -> str:
    """Converts a datetime or string to a standard ISO-8601 string."""
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


class LifecycleManager:
    """Manages lifecycle states, closure detection, and reopening for job postings.

    Features:
    - Identifies disappeared jobs after fresh scraper crawls and transitions status from 'open' to 'closed'.
    - Handles multi-source canonical records: if a job was found on multiple sources (e.g. Greenhouse
      and Hacker News), verifies whether any active linked source remains before closing the canonical record.
    - Records immutable audit history entries in `posting_change_history` for status changes and source deprecations.
    - Detects reappearing postings and transitions status from 'closed' back to 'open' with 'posting_reopened' audit entries.
    - Idempotent and resilient across database schemas (supports both 'canonical_id' and 'posting_id' columns).
    """

    def __init__(
        self,
        table_name: str = "canonical_job_postings",
        history_table: str = "posting_change_history",
        source_lifecycle_table: str = "canonical_source_lifecycle",
    ) -> None:
        self.table_name = table_name
        self.history_table = history_table
        self.source_lifecycle_table = source_lifecycle_table

    # --------------------------------------------------------------------------
    # Schema Helpers
    # --------------------------------------------------------------------------

    def ensure_tables(self, conn: sqlite3.Connection) -> None:
        """Ensures that posting_change_history and canonical_source_lifecycle tables exist."""
        with conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.history_table} (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    posting_id TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reason TEXT
                )
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.history_table}_posting_id ON {self.history_table} (posting_id)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.history_table}_detected_at ON {self.history_table} (detected_at)"
            )

            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.source_lifecycle_table} (
                    posting_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    last_seen TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (posting_id, source)
                )
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.source_lifecycle_table}_posting_id ON {self.source_lifecycle_table} (posting_id)"
            )

    def _get_columns(self, conn: sqlite3.Connection, table_name: str) -> List[str]:
        """Returns column names for a given table, or an empty list if table does not exist."""
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        return [row[1] for row in cursor.fetchall()]

    def _get_id_column(self, conn: sqlite3.Connection, table_name: str) -> str:
        """Determines whether canonical_id or posting_id is the primary identifier."""
        cols = self._get_columns(conn, table_name)
        if "canonical_id" in cols:
            return "canonical_id"
        if "posting_id" in cols:
            return "posting_id"
        return "canonical_id"

    def _get_history_id_col(self, conn: sqlite3.Connection) -> str:
        """Determines the ID column name for posting_change_history."""
        cols = self._get_columns(conn, self.history_table)
        if "posting_id" in cols:
            return "posting_id"
        if "canonical_id" in cols:
            return "canonical_id"
        return "posting_id"

    # --------------------------------------------------------------------------
    # Closure Detection
    # --------------------------------------------------------------------------

    def detect_closed_postings(
        self_or_conn: Union["LifecycleManager", sqlite3.Connection],
        conn_or_source: Any = None,
        source_name_or_active_ids: Any = None,
        active_ids_or_ts: Any = None,
        current_timestamp: Optional[datetime] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Identifies job postings that have disappeared from fresh source crawls.

        Can be called as an instance method or class method:
            manager.detect_closed_postings(conn, source_name, active_source_posting_ids, current_timestamp)
            LifecycleManager.detect_closed_postings(conn, source_name, active_source_posting_ids, current_timestamp)

        Rules:
        1. Selects postings in canonical_job_postings where status = 'open'.
        2. Filters for postings that belong to `source_name` (in sources list or source_posting_ids).
        3. Identifies records NOT present in `active_source_posting_ids`.
        4. For multi-source canonical records:
           - Marks source-level deprecation for `source_name`.
           - If other linked sources are still active in the current window, the canonical record remains 'open'.
           - If ALL linked sources are now inactive, transitions canonical status to 'closed'.
        5. Updates canonical_job_postings SET status = 'closed', last_seen = ? WHERE posting_id = ?.
        6. Writes audit entry to posting_change_history:
           field_name = 'status', old_value = 'open', new_value = 'closed', reason = 'posting_closed'.
        7. Returns list of closed postings with summary metrics.
        """
        # Resolve self vs class call and arguments
        if isinstance(self_or_conn, LifecycleManager):
            self = self_or_conn
            conn = conn_or_source if conn_or_source is not None else kwargs.get("conn")
            source_name = (
                source_name_or_active_ids
                if source_name_or_active_ids is not None
                else kwargs.get("source_name")
            )
            active_source_posting_ids = (
                active_ids_or_ts
                if active_ids_or_ts is not None
                else kwargs.get("active_source_posting_ids")
            )
            ts = current_timestamp if current_timestamp is not None else kwargs.get("current_timestamp")
        else:
            self = LifecycleManager()
            conn = self_or_conn
            source_name = conn_or_source if conn_or_source is not None else kwargs.get("source_name")
            active_source_posting_ids = (
                source_name_or_active_ids
                if source_name_or_active_ids is not None
                else kwargs.get("active_source_posting_ids")
            )
            ts = active_ids_or_ts if active_ids_or_ts is not None else kwargs.get("current_timestamp")

        if conn is None:
            raise ValueError("sqlite3.Connection must be provided to detect_closed_postings.")

        # Edge case: brand new database (table canonical_job_postings does not exist)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (self.table_name,))
        if not cursor.fetchone():
            logger.info("Table '%s' does not exist. 0 closures detected.", self.table_name)
            return []

        # Ensure history and source lifecycle tables exist
        self.ensure_tables(conn)

        # Normalize parameters
        source_clean = str(source_name).strip().lower()
        active_ids_set: Set[str] = {str(x).strip() for x in (active_source_posting_ids or set())}
        ts_str = _to_iso_timestamp(ts)

        id_col = self._get_id_column(conn, self.table_name)
        hist_id_col = self._get_history_id_col(conn)
        cols = self._get_columns(conn, self.table_name)
        has_updated_at = "updated_at" in cols
        has_active_sources_col = "active_sources" in cols

        # Query open postings
        query = f"SELECT * FROM {self.table_name} WHERE status = 'open'"
        cursor.execute(query)
        desc = cursor.description or []
        col_names = [d[0] for d in desc]
        raw_rows = cursor.fetchall()

        closed_postings: List[Dict[str, Any]] = []

        with conn:
            for raw_row in raw_rows:
                if isinstance(raw_row, sqlite3.Row) or isinstance(raw_row, dict):
                    row = dict(raw_row)
                else:
                    row = dict(zip(col_names, raw_row))

                pid = str(row.get(id_col) or "").strip()
                if not pid:
                    continue

                row_sources = _parse_sources(row.get("sources"))
                row_source_ids = _parse_ids(row.get("source_posting_ids"))

                # 1. Check if posting belongs to this source
                belongs_to_source = False
                if row_sources:
                    if source_clean in row_sources:
                        belongs_to_source = True
                else:
                    # If sources is empty, check if source_posting_ids match
                    if row_source_ids:
                        belongs_to_source = True
                    else:
                        belongs_to_source = True

                if not belongs_to_source:
                    continue

                # 2. Check if posting is present in active crawl IDs
                is_active = (pid in active_ids_set) or any(sid in active_ids_set for sid in row_source_ids)

                if is_active:
                    # Posting is active in current crawl for this source
                    conn.execute(
                        f"""
                        INSERT INTO {self.source_lifecycle_table} (posting_id, source, status, last_seen, updated_at)
                        VALUES (?, ?, 'active', ?, ?)
                        ON CONFLICT(posting_id, source) DO UPDATE SET
                            status = 'active',
                            last_seen = excluded.last_seen,
                            updated_at = excluded.updated_at
                        """,
                        (pid, source_clean, ts_str, ts_str),
                    )
                    continue

                # 3. Posting has disappeared from this source
                # Initialize source lifecycle for all linked sources if not yet tracked
                existing_src_rows = conn.execute(
                    f"SELECT source, status FROM {self.source_lifecycle_table} WHERE posting_id = ?",
                    (pid,),
                ).fetchall()
                tracked_sources = {r[0]: r[1] for r in existing_src_rows}

                all_linked_sources = set(row_sources) if row_sources else {source_clean}
                for s in all_linked_sources:
                    if s not in tracked_sources:
                        # Initialize as active prior to this check
                        conn.execute(
                            f"""
                            INSERT INTO {self.source_lifecycle_table} (posting_id, source, status, last_seen, updated_at)
                            VALUES (?, ?, 'active', ?, ?)
                            """,
                            (pid, s, row.get("last_seen", ts_str), ts_str),
                        )
                        tracked_sources[s] = "active"

                # Mark source_clean as inactive
                was_active = tracked_sources.get(source_clean) == "active"
                conn.execute(
                    f"""
                    UPDATE {self.source_lifecycle_table}
                    SET status = 'inactive', updated_at = ?
                    WHERE posting_id = ? AND source = ?
                    """,
                    (ts_str, pid, source_clean),
                )
                tracked_sources[source_clean] = "inactive"

                if was_active:
                    # Write source-level deprecation audit log
                    conn.execute(
                        f"""
                        INSERT INTO {self.history_table} ({hist_id_col}, field_name, old_value, new_value, detected_at, reason)
                        VALUES (?, ?, 'active', 'inactive', ?, 'source_closed')
                        """,
                        (pid, f"source:{source_clean}", ts_str),
                    )

                # 4. Multi-source evaluation: Check if any other linked sources remain active
                remaining_active_sources = [
                    s for s, st in tracked_sources.items() if st == "active" and s != source_clean
                ]

                if has_active_sources_col:
                    conn.execute(
                        f"UPDATE {self.table_name} SET active_sources = ? WHERE {id_col} = ?",
                        (json.dumps(remaining_active_sources), pid),
                    )

                if remaining_active_sources:
                    # Multi-source resilience: At least one source is still active!
                    logger.info(
                        "Posting %s remains 'open' due to active remaining sources: %s",
                        pid,
                        remaining_active_sources,
                    )
                    continue

                # 5. All linked sources are now inactive -> Transition canonical record to 'closed'
                if has_updated_at:
                    conn.execute(
                        f"""
                        UPDATE {self.table_name}
                        SET status = 'closed', last_seen = ?, updated_at = ?
                        WHERE {id_col} = ?
                        """,
                        (ts_str, ts_str, pid),
                    )
                else:
                    conn.execute(
                        f"""
                        UPDATE {self.table_name}
                        SET status = 'closed', last_seen = ?
                        WHERE {id_col} = ?
                        """,
                        (ts_str, pid),
                    )

                # 6. Write status change audit entry
                conn.execute(
                    f"""
                    INSERT INTO {self.history_table} ({hist_id_col}, field_name, old_value, new_value, detected_at, reason)
                    VALUES (?, 'status', 'open', 'closed', ?, 'posting_closed')
                    """,
                    (pid, ts_str),
                )

                closed_postings.append(
                    {
                        "posting_id": pid,
                        "canonical_id": pid,
                        "company": row.get("company", ""),
                        "title": row.get("title", ""),
                        "status": "closed",
                        "previous_status": "open",
                        "sources": row_sources,
                        "deprecated_source": source_clean,
                        "closed_at": ts_str,
                        "reason": "posting_closed",
                    }
                )

        logger.info(
            "detect_closed_postings completed for source '%s'. Closed %d postings.",
            source_clean,
            len(closed_postings),
        )
        return closed_postings

    # --------------------------------------------------------------------------
    # Reopening Detection
    # --------------------------------------------------------------------------

    def detect_reopened_postings(
        self_or_conn: Union["LifecycleManager", sqlite3.Connection],
        conn_or_id: Any = None,
        incoming_id_or_ts: Any = None,
        current_timestamp: Optional[datetime] = None,
        **kwargs: Any,
    ) -> bool:
        """Detects whether an incoming posting matches an existing closed record and reopens it.

        Can be called as an instance method or class method:
            manager.detect_reopened_postings(conn, incoming_posting_id, current_timestamp)
            LifecycleManager.detect_reopened_postings(conn, incoming_posting_id, current_timestamp)

        Rules:
        1. Queries canonical_job_postings for record matching incoming_posting_id (by primary ID
           or linked raw source_posting_ids).
        2. If matched record has status = 'closed':
           - Updates canonical_job_postings SET status = 'open', last_seen = ? WHERE posting_id = ?.
           - Writes audit entry to posting_change_history:
             field_name = 'status', old_value = 'closed', new_value = 'open', reason = 'posting_reopened'.
           - Returns True.
        3. If record is already open or not found, returns False.
        """
        # Resolve self vs class call and arguments
        if isinstance(self_or_conn, LifecycleManager):
            self = self_or_conn
            conn = conn_or_id if conn_or_id is not None else kwargs.get("conn")
            incoming_posting_id = (
                incoming_id_or_ts
                if incoming_id_or_ts is not None
                else kwargs.get("incoming_posting_id")
            )
            ts = current_timestamp if current_timestamp is not None else kwargs.get("current_timestamp")
        else:
            self = LifecycleManager()
            conn = self_or_conn
            incoming_posting_id = conn_or_id if conn_or_id is not None else kwargs.get("incoming_posting_id")
            ts = incoming_id_or_ts if incoming_id_or_ts is not None else kwargs.get("current_timestamp")

        if conn is None:
            raise ValueError("sqlite3.Connection must be provided to detect_reopened_postings.")

        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (self.table_name,))
        if not cursor.fetchone():
            return False

        self.ensure_tables(conn)

        target_id = str(incoming_posting_id).strip()
        ts_str = _to_iso_timestamp(ts)

        id_col = self._get_id_column(conn, self.table_name)
        hist_id_col = self._get_history_id_col(conn)
        cols = self._get_columns(conn, self.table_name)
        has_updated_at = "updated_at" in cols

        # 1. Search by primary key
        cursor.execute(f"SELECT * FROM {self.table_name} WHERE {id_col} = ?", (target_id,))
        raw_row = cursor.fetchone()

        pid: Optional[str] = None
        current_status: Optional[str] = None

        if raw_row:
            desc = cursor.description or []
            col_names = [d[0] for d in desc]
            row = dict(raw_row) if isinstance(raw_row, (sqlite3.Row, dict)) else dict(zip(col_names, raw_row))
            pid = str(row.get(id_col) or target_id)
            current_status = str(row.get("status") or "").strip().lower()
        else:
            # 2. Search across closed records by source_posting_ids
            cursor.execute(f"SELECT * FROM {self.table_name} WHERE status = 'closed'")
            desc = cursor.description or []
            col_names = [d[0] for d in desc]
            for cand in cursor.fetchall():
                c_row = dict(cand) if isinstance(cand, (sqlite3.Row, dict)) else dict(zip(col_names, cand))
                s_ids = _parse_ids(c_row.get("source_posting_ids"))
                if target_id in s_ids:
                    pid = str(c_row.get(id_col))
                    current_status = "closed"
                    break

        if not pid or current_status != "closed":
            return False

        # Reopen the record
        with conn:
            if has_updated_at:
                conn.execute(
                    f"""
                    UPDATE {self.table_name}
                    SET status = 'open', last_seen = ?, updated_at = ?
                    WHERE {id_col} = ?
                    """,
                    (ts_str, ts_str, pid),
                )
            else:
                conn.execute(
                    f"""
                    UPDATE {self.table_name}
                    SET status = 'open', last_seen = ?
                    WHERE {id_col} = ?
                    """,
                    (ts_str, pid),
                )

            # Re-activate in source lifecycle table
            conn.execute(
                f"""
                UPDATE {self.source_lifecycle_table}
                SET status = 'active', last_seen = ?, updated_at = ?
                WHERE posting_id = ?
                """,
                (ts_str, ts_str, pid),
            )

            # Record audit entry in posting_change_history
            conn.execute(
                f"""
                INSERT INTO {self.history_table} ({hist_id_col}, field_name, old_value, new_value, detected_at, reason)
                VALUES (?, 'status', 'closed', 'open', ?, 'posting_reopened')
                """,
                (pid, ts_str),
            )

        logger.info("Reopened posting %s (status transitioned closed -> open).", pid)
        return True

    # --------------------------------------------------------------------------
    # Audit & Inspection Utilities
    # --------------------------------------------------------------------------

    def get_change_history(
        self,
        conn: sqlite3.Connection,
        posting_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieves audit change history records, optionally filtered by posting_id."""
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (self.history_table,))
        if not cursor.fetchone():
            return []

        hist_id_col = self._get_history_id_col(conn)
        if posting_id:
            query = f"SELECT * FROM {self.history_table} WHERE {hist_id_col} = ? ORDER BY history_id ASC"
            cursor.execute(query, (str(posting_id).strip(),))
        else:
            query = f"SELECT * FROM {self.history_table} ORDER BY history_id ASC"
            cursor.execute(query)

        desc = cursor.description or []
        col_names = [d[0] for d in desc]
        rows = cursor.fetchall()
        return [
            dict(r) if isinstance(r, (sqlite3.Row, dict)) else dict(zip(col_names, r))
            for r in rows
        ]

    def get_active_sources_for_posting(
        self,
        conn: sqlite3.Connection,
        posting_id: str,
    ) -> Set[str]:
        """Returns the set of currently active sources for a given canonical posting."""
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (self.source_lifecycle_table,))
        if not cursor.fetchone():
            return set()
        cursor.execute(
            f"SELECT source FROM {self.source_lifecycle_table} WHERE posting_id = ? AND status = 'active'",
            (str(posting_id).strip(),),
        )
        return {r[0] for r in cursor.fetchall()}

    def get_lifecycle_metrics(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """Returns summary metrics of open vs closed postings and change history counts."""
        cursor = conn.cursor()
        metrics = {
            "total_canonical": 0,
            "open_postings": 0,
            "closed_postings": 0,
            "total_audit_events": 0,
            "closure_events": 0,
            "reopened_events": 0,
        }

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (self.table_name,))
        if cursor.fetchone():
            cursor.execute(f"SELECT status, COUNT(*) FROM {self.table_name} GROUP BY status")
            for status, count in cursor.fetchall():
                st = str(status).lower()
                if st == "open":
                    metrics["open_postings"] = count
                elif st == "closed":
                    metrics["closed_postings"] = count
                metrics["total_canonical"] += count

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (self.history_table,))
        if cursor.fetchone():
            cursor.execute(f"SELECT COUNT(*) FROM {self.history_table}")
            metrics["total_audit_events"] = cursor.fetchone()[0]

            cursor.execute(
                f"SELECT reason, COUNT(*) FROM {self.history_table} GROUP BY reason"
            )
            for reason, count in cursor.fetchall():
                r = str(reason).lower()
                if r == "posting_closed":
                    metrics["closure_events"] = count
                elif r == "posting_reopened":
                    metrics["reopened_events"] = count

        return metrics

"""Change Detection and Audit Logging Engine for Job Postings.

Tracks, detects, and records field-level changes (e.g. compensation updates,
remote policy transitions, location shifts, skill modifications, title updates,
and lifecycle status changes) across pipeline runs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)


# ==============================================================================
# FieldChange Model
# ==============================================================================


class FieldChange(BaseModel):
    """Represents a detected modification to a single tracked field."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=False,
    )

    field_name: str = Field(..., description="Name of the tracked field that changed")
    old_value: Optional[str] = Field(default=None, description="Previous value serialized as string or null")
    new_value: Optional[str] = Field(default=None, description="New value serialized as string or null")
    reason: str = Field(..., description="Reason code or explanation for the change")

    @field_validator("old_value", "new_value", mode="before")
    @classmethod
    def _coerce_to_str(cls, v: Any) -> Optional[str]:
        """Ensures old_value and new_value are stringified or None."""
        if v is None:
            return None
        if hasattr(v, "value"):  # Enum support (e.g. RemotePolicyEnum.remote)
            return str(v.value)
        if isinstance(v, (list, set, tuple)):
            cleaned = [str(x).strip() for x in v if str(x).strip()]
            return json.dumps(sorted(cleaned))
        if isinstance(v, dict):
            return json.dumps(v, sort_keys=True)
        return str(v)

    def __getitem__(self, item: str) -> Any:
        """Allow dict-like subscripting for compatibility."""
        return getattr(self, item)

    def to_dict(self) -> Dict[str, Any]:
        """Convert model to dictionary."""
        return self.model_dump()


# ==============================================================================
# Hybrid Method Decorator
# ==============================================================================


class _hybrid_method:
    """Descriptor enabling methods to be invoked on either class or instance."""

    def __init__(self, func):
        self.func = func

    def __get__(self, instance, owner):
        if instance is None:
            def class_wrapper(*args, **kwargs):
                return self.func(owner, *args, **kwargs)
            return class_wrapper
        else:
            def instance_wrapper(*args, **kwargs):
                return self.func(instance, *args, **kwargs)
            return instance_wrapper


# ==============================================================================
# ChangeTracker Engine
# ==============================================================================


class ChangeTracker:
    """Change detection and audit logger engine for job postings across pipeline runs."""

    DEFAULT_FLOAT_TOLERANCE: float = 1.0

    def __init__(
        self,
        float_tolerance: float = DEFAULT_FLOAT_TOLERANCE,
        db_path: Optional[Union[str, Path]] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Initialize the ChangeTracker.

        Args:
            float_tolerance: Numeric threshold for floating point salary comparisons.
                Absolute differences <= float_tolerance are treated as insignificant noise.
            db_path: Optional SQLite database file path.
            conn: Optional existing sqlite3.Connection.
        """
        self.float_tolerance = float_tolerance
        self.db_path = str(db_path) if db_path else None
        self.conn = conn

    @classmethod
    def init_table(cls, conn: sqlite3.Connection) -> None:
        """Ensure the posting_change_history table and necessary indexes exist.

        Args:
            conn: Active SQLite database connection.
        """
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS posting_change_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    posting_id TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reason TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_posting_change_history_posting "
                "ON posting_change_history (posting_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_posting_change_history_field "
                "ON posting_change_history (field_name)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_posting_change_history_detected "
                "ON posting_change_history (detected_at)"
            )

    # --------------------------------------------------------------------------
    # Helper Parsing Methods
    # --------------------------------------------------------------------------

    @staticmethod
    def _to_dict(rec: Any) -> Dict[str, Any]:
        """Convert input record (dict, Pydantic model, or Row) to standard dict."""
        if rec is None:
            return {}
        if isinstance(rec, dict):
            return rec
        if hasattr(rec, "model_dump"):
            return rec.model_dump()
        if hasattr(rec, "dict"):
            return rec.dict()
        if hasattr(rec, "keys"):
            return dict(rec)
        return getattr(rec, "__dict__", {})

    @staticmethod
    def _parse_float(val: Any) -> Optional[float]:
        """Safely parse float value or return None."""
        if val is None:
            return None
        try:
            s = str(val).strip()
            if not s:
                return None
            return float(s)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _normalize_skills(skills_raw: Any) -> Tuple[Set[str], List[str]]:
        """Normalize skills into a lowercase set for comparison and a sorted list for display."""
        if not skills_raw:
            return set(), []

        raw_list: List[str] = []
        if isinstance(skills_raw, str):
            s = skills_raw.strip()
            if not s:
                return set(), []
            if (s.startswith("[") and s.endswith("]")) or (s.startswith("(") and s.endswith(")")):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        raw_list = [str(x).strip() for x in parsed if x and str(x).strip()]
                except Exception:
                    raw_list = [part.strip() for part in s.split(",") if part.strip()]
            else:
                raw_list = [part.strip() for part in s.split(",") if part.strip()]
        elif isinstance(skills_raw, (list, set, tuple)):
            raw_list = [str(x).strip() for x in skills_raw if x and str(x).strip()]

        norm_set = {x.lower() for x in raw_list}
        display_list = sorted(list(set(raw_list)))
        return norm_set, display_list

    # --------------------------------------------------------------------------
    # Core Change Detection Logic
    # --------------------------------------------------------------------------

    @_hybrid_method
    def detect_changes(
        target,
        existing_record: Union[Dict[str, Any], BaseModel, Any],
        incoming_record: Union[Dict[str, Any], BaseModel, Any],
        float_tolerance: Optional[float] = None,
    ) -> List[FieldChange]:
        """Compares existing and incoming job posting records and returns detected changes.

        Tracked fields and change reasons:
        - `salary_min` & `salary_max`: Numeric comparison with tolerance. Reason: 'compensation_updated'.
        - `currency`: ISO code / currency change. Reason: 'currency_updated'.
        - `remote_policy`: Workplace policy transition. Reason: 'remote_status_changed'.
        - `city` / `country` / `location`: Geographic location updates. Reason: 'location_updated'.
        - `skills`: Case-normalized, order-invariant set comparison. Reason: 'skills_updated'.
        - `title`: Canonical job title change. Reason: 'title_updated'.
        - `status`: Lifecycle status transition (e.g. open -> closed). Reason: 'status_changed'.

        Args:
            existing_record: Baseline record from database or previous run.
            incoming_record: Fresh extracted or merged record.
            float_tolerance: Optional override for salary numeric tolerance.

        Returns:
            List of FieldChange instances (empty if records are identical across tracked fields).
        """
        existing = ChangeTracker._to_dict(existing_record)
        incoming = ChangeTracker._to_dict(incoming_record)

        if float_tolerance is not None:
            tol = float_tolerance
        elif isinstance(target, ChangeTracker):
            tol = target.float_tolerance
        else:
            tol = ChangeTracker.DEFAULT_FLOAT_TOLERANCE

        changes: List[FieldChange] = []

        # 1. Salary Min & Max (compensation_updated)
        for sal_field in ("salary_min", "salary_max"):
            old_raw = existing.get(sal_field)
            new_raw = incoming.get(sal_field)

            old_f = ChangeTracker._parse_float(old_raw)
            new_f = ChangeTracker._parse_float(new_raw)

            if old_f is None and new_f is None:
                pass  # Both unset or null
            elif old_f is None or new_f is None:
                changes.append(
                    FieldChange(
                        field_name=sal_field,
                        old_value=str(old_raw) if old_raw is not None else None,
                        new_value=str(new_raw) if new_raw is not None else None,
                        reason="compensation_updated",
                    )
                )
            else:
                if abs(old_f - new_f) > tol:
                    changes.append(
                        FieldChange(
                            field_name=sal_field,
                            old_value=str(old_raw),
                            new_value=str(new_raw),
                            reason="compensation_updated",
                        )
                    )

        # 2. Currency (currency_updated)
        old_curr = existing.get("currency")
        new_curr = incoming.get("currency")
        old_curr_norm = str(old_curr).strip().upper() if old_curr and str(old_curr).strip() else None
        new_curr_norm = str(new_curr).strip().upper() if new_curr and str(new_curr).strip() else None

        if old_curr_norm != new_curr_norm:
            changes.append(
                FieldChange(
                    field_name="currency",
                    old_value=str(old_curr).strip() if old_curr is not None else None,
                    new_value=str(new_curr).strip() if new_curr is not None else None,
                    reason="currency_updated",
                )
            )

        # 3. Remote Policy (remote_status_changed)
        old_rp = existing.get("remote_policy")
        new_rp = incoming.get("remote_policy")
        old_rp_str = old_rp.value if hasattr(old_rp, "value") else str(old_rp).strip() if old_rp is not None else None
        new_rp_str = new_rp.value if hasattr(new_rp, "value") else str(new_rp).strip() if new_rp is not None else None

        old_rp_norm = old_rp_str.lower() if old_rp_str else None
        new_rp_norm = new_rp_str.lower() if new_rp_str else None

        if old_rp_norm != new_rp_norm:
            changes.append(
                FieldChange(
                    field_name="remote_policy",
                    old_value=old_rp_norm,
                    new_value=new_rp_norm,
                    reason="remote_status_changed",
                )
            )

        # 4. Location: City & Country (location_updated)
        has_city_or_country = False
        for loc_field in ("city", "country"):
            if loc_field in existing or loc_field in incoming:
                has_city_or_country = True
                old_val = existing.get(loc_field)
                new_val = incoming.get(loc_field)
                old_norm = str(old_val).strip() if old_val and str(old_val).strip() else None
                new_norm = str(new_val).strip() if new_val and str(new_val).strip() else None

                if (old_norm is None and new_norm is not None) or \
                   (old_norm is not None and new_norm is None) or \
                   (old_norm and new_norm and old_norm.lower() != new_norm.lower()):
                    changes.append(
                        FieldChange(
                            field_name=loc_field,
                            old_value=old_norm,
                            new_value=new_norm,
                            reason="location_updated",
                        )
                    )

        # Fallback to single 'location' field if neither city nor country is tracked
        if not has_city_or_country and ("location" in existing or "location" in incoming):
            old_loc = existing.get("location")
            new_loc = incoming.get("location")
            old_norm = str(old_loc).strip() if old_loc and str(old_loc).strip() else None
            new_norm = str(new_loc).strip() if new_loc and str(new_loc).strip() else None

            if (old_norm is None and new_norm is not None) or \
               (old_norm is not None and new_norm is None) or \
               (old_norm and new_norm and old_norm.lower() != new_norm.lower()):
                changes.append(
                    FieldChange(
                        field_name="location",
                        old_value=old_norm,
                        new_value=new_norm,
                        reason="location_updated",
                    )
                )

        # 5. Skills (skills_updated)
        old_skills_raw = existing.get("skills")
        new_skills_raw = incoming.get("skills")

        old_set, old_disp = ChangeTracker._normalize_skills(old_skills_raw)
        new_set, new_disp = ChangeTracker._normalize_skills(new_skills_raw)

        if old_set != new_set:
            old_val_str = json.dumps(old_disp) if old_skills_raw is not None else None
            new_val_str = json.dumps(new_disp) if new_skills_raw is not None else None
            changes.append(
                FieldChange(
                    field_name="skills",
                    old_value=old_val_str,
                    new_value=new_val_str,
                    reason="skills_updated",
                )
            )

        # 6. Title (title_updated)
        old_title = existing.get("title")
        new_title = incoming.get("title")
        old_t_norm = str(old_title).strip() if old_title and str(old_title).strip() else None
        new_t_norm = str(new_title).strip() if new_title and str(new_title).strip() else None

        if (old_t_norm is None and new_t_norm is not None) or \
           (old_t_norm is not None and new_t_norm is None) or \
           (old_t_norm and new_t_norm and old_t_norm.lower() != new_t_norm.lower()):
            changes.append(
                FieldChange(
                    field_name="title",
                    old_value=old_t_norm,
                    new_value=new_t_norm,
                    reason="title_updated",
                )
            )

        # 7. Status (status_changed)
        old_status = existing.get("status")
        new_status = incoming.get("status")
        old_s_str = old_status.value if hasattr(old_status, "value") else str(old_status).strip() if old_status is not None else None
        new_s_str = new_status.value if hasattr(new_status, "value") else str(new_status).strip() if new_status is not None else None

        old_s_norm = old_s_str.lower() if old_s_str else None
        new_s_norm = new_s_str.lower() if new_s_str else None

        if old_s_norm != new_s_norm:
            changes.append(
                FieldChange(
                    field_name="status",
                    old_value=old_s_norm,
                    new_value=new_s_norm,
                    reason="status_changed",
                )
            )

        return changes

    # --------------------------------------------------------------------------
    # Database Persistence & Querying
    # --------------------------------------------------------------------------

    @_hybrid_method
    def log_changes(
        target,
        conn: sqlite3.Connection,
        posting_id: str,
        changes: List[Union[FieldChange, Dict[str, Any]]],
        timestamp: Optional[datetime] = None,
    ) -> int:
        """Inserts detected changes into the posting_change_history table.

        Args:
            conn: Active SQLite connection.
            posting_id: Canonical posting ID associated with the changes.
            changes: List of FieldChange objects or dicts.
            timestamp: Optional explicit datetime for detected_at (defaults to UTC now).

        Returns:
            Count of logged changes inserted into the database.
        """
        if not changes:
            return 0

        ChangeTracker.init_table(conn)

        if timestamp is not None:
            detected_at_str = (
                timestamp.isoformat()
                if hasattr(timestamp, "isoformat")
                else str(timestamp)
            )
        else:
            detected_at_str = datetime.now(timezone.utc).isoformat()

        insert_rows: List[Tuple[str, str, Optional[str], Optional[str], str, str]] = []
        for chg in changes:
            if isinstance(chg, FieldChange):
                insert_rows.append(
                    (
                        posting_id,
                        chg.field_name,
                        chg.old_value,
                        chg.new_value,
                        detected_at_str,
                        chg.reason,
                    )
                )
            elif isinstance(chg, dict):
                insert_rows.append(
                    (
                        posting_id,
                        str(chg["field_name"]),
                        str(chg["old_value"]) if chg.get("old_value") is not None else None,
                        str(chg["new_value"]) if chg.get("new_value") is not None else None,
                        detected_at_str,
                        str(chg["reason"]),
                    )
                )
            else:
                insert_rows.append(
                    (
                        posting_id,
                        getattr(chg, "field_name", str(chg[0])),
                        getattr(chg, "old_value", str(chg[1]) if chg[1] is not None else None),
                        getattr(chg, "new_value", str(chg[2]) if chg[2] is not None else None),
                        detected_at_str,
                        getattr(chg, "reason", str(chg[3])),
                    )
                )

        with conn:
            conn.executemany(
                """
                INSERT INTO posting_change_history (
                    posting_id, field_name, old_value, new_value, detected_at, reason
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                insert_rows,
            )

        logger.debug(
            "Logged %d change(s) for posting_id %s into posting_change_history.",
            len(insert_rows),
            posting_id,
        )
        return len(insert_rows)

    @_hybrid_method
    def get_history(
        target,
        conn: sqlite3.Connection,
        posting_id: Optional[str] = None,
        field_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Queries the posting_change_history table sorted by detected_at DESC.

        Args:
            conn: Active SQLite connection.
            posting_id: Optional posting ID filter.
            field_name: Optional field name filter.

        Returns:
            List of dictionaries containing history records ordered by detected_at DESC.
        """
        ChangeTracker.init_table(conn)

        query = (
            "SELECT history_id, posting_id, field_name, old_value, new_value, "
            "detected_at, reason FROM posting_change_history"
        )
        clauses: List[str] = []
        params: List[Any] = []

        if posting_id is not None:
            clauses.append("posting_id = ?")
            params.append(str(posting_id))

        if field_name is not None:
            clauses.append("field_name = ?")
            params.append(str(field_name))

        if clauses:
            query += " WHERE " + " AND ".join(clauses)

        query += " ORDER BY detected_at DESC, history_id DESC"

        cursor = conn.cursor()
        cursor.execute(query, params)
        cols = [col[0] for col in cursor.description] if cursor.description else []
        rows = cursor.fetchall()

        results: List[Dict[str, Any]] = []
        for r in rows:
            if isinstance(r, dict):
                results.append(r)
            else:
                results.append(dict(zip(cols, r)))

        return results

"""SQLite Database Engine and Schema Management for the Twig Ingestion Pipeline.

Provides thread-safe connection pooling, PRAGMA optimizations (WAL mode,
foreign key enforcement, synchronous normal, 5s busy timeout), complete
DDL schema initialization, and transactional context managers.
"""

from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from pipeline.config import DB_PATH

logger = logging.getLogger(__name__)

# ==============================================================================
# Database Schema DDL Definition
# ==============================================================================

SCHEMA_DDL = """
-- Table 1: Raw Ingestion Cache
CREATE TABLE IF NOT EXISTS raw_ingestion_cache (
    cache_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Table 2: Canonical Job Postings
CREATE TABLE IF NOT EXISTS canonical_job_postings (
    posting_id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    seniority TEXT CHECK(seniority IN ('junior', 'mid', 'senior', 'staff_plus') OR seniority IS NULL),
    city TEXT,
    country TEXT,
    remote_policy TEXT CHECK(remote_policy IN ('onsite', 'hybrid', 'remote') OR remote_policy IS NULL),
    salary_min REAL,
    salary_max REAL,
    currency TEXT,
    employment_type TEXT CHECK(employment_type IN ('full_time', 'contract', 'intern') OR employment_type IS NULL),
    skills TEXT,
    visa_sponsorship BOOLEAN,
    posted_date DATE,
    status TEXT DEFAULT 'open' CHECK(status IN ('open', 'closed')),
    sources TEXT NOT NULL,
    source_urls TEXT NOT NULL,
    source_posting_ids TEXT NOT NULL,
    evidence TEXT NOT NULL,
    first_seen TIMESTAMP NOT NULL,
    last_seen TIMESTAMP NOT NULL,
    duplicate_count INTEGER DEFAULT 1
);

-- Table 3: Posting Change History
CREATE TABLE IF NOT EXISTS posting_change_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_id TEXT NOT NULL REFERENCES canonical_job_postings(posting_id),
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason TEXT
);

-- Required Performance & Filter Indices
CREATE INDEX IF NOT EXISTS idx_canonical_company ON canonical_job_postings(company);
CREATE INDEX IF NOT EXISTS idx_canonical_status ON canonical_job_postings(status);
CREATE INDEX IF NOT EXISTS idx_canonical_last_seen ON canonical_job_postings(last_seen);
CREATE INDEX IF NOT EXISTS idx_history_posting_id ON posting_change_history(posting_id);
CREATE INDEX IF NOT EXISTS idx_history_field ON posting_change_history(field_name);

-- Ingestion Cache Performance Indices
CREATE INDEX IF NOT EXISTS idx_raw_ingestion_source ON raw_ingestion_cache(source);
CREATE INDEX IF NOT EXISTS idx_raw_ingestion_url ON raw_ingestion_cache(source_url);
"""

EXPECTED_TABLES = [
    "raw_ingestion_cache",
    "canonical_job_postings",
    "posting_change_history",
]

EXPECTED_INDICES = [
    "idx_canonical_company",
    "idx_canonical_status",
    "idx_canonical_last_seen",
    "idx_history_posting_id",
    "idx_history_field",
]


# ==============================================================================
# Connection & PRAGMA Optimization Helpers
# ==============================================================================


def configure_connection(
    conn: sqlite3.Connection,
    is_memory: bool = False,
) -> sqlite3.Connection:
    """Configures performance and integrity PRAGMA settings on an open SQLite connection.

    Enforces:
    - PRAGMA foreign_keys = ON;
    - PRAGMA busy_timeout = 5000;
    - PRAGMA synchronous = NORMAL;
    - PRAGMA journal_mode = WAL; (for persistent disk databases; handled gracefully for :memory:)
    - Row factory configured to sqlite3.Row for dictionary-like column access.

    Args:
        conn: Open SQLite connection.
        is_memory: True if connection is an in-memory database.

    Returns:
        Configured sqlite3.Connection instance.
    """
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("PRAGMA busy_timeout = 5000;")
    cursor.execute("PRAGMA synchronous = NORMAL;")
    # WAL journal mode: persistent disk databases switch to WAL mode.
    # In-memory databases return 'memory' or 'off' without raising errors.
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.close()
    return conn


def create_connection(
    db_path: Optional[Union[str, Path]] = None,
    timeout: float = 30.0,
) -> sqlite3.Connection:
    """Creates a newly configured SQLite connection with all PRAGMAs applied.

    Args:
        db_path: Database path string or Path object. Defaults to pipeline.config.DB_PATH.
        timeout: Timeout in seconds for acquiring database locks.

    Returns:
        Configured sqlite3.Connection instance.
    """
    if db_path is None:
        target_path = str(DB_PATH)
    else:
        target_path = str(db_path)

    is_memory = target_path == ":memory:"
    if not is_memory:
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        target_path,
        timeout=timeout,
        check_same_thread=False,
    )
    return configure_connection(conn, is_memory=is_memory)


# Alias get_connection to create_connection for API flexibility
get_connection = create_connection


def init_db(
    db_path: Optional[Union[str, Path]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> sqlite3.Connection:
    """Initializes the SQLite database schema DDL across all tables and indices.

    If an active connection is provided, the DDL is executed on that connection.
    Otherwise, a new connection is established, configured, and returned.

    Args:
        db_path: Target SQLite database path or ':memory:'. Defaults to pipeline.config.DB_PATH.
        conn: Optional existing open connection to initialize.

    Returns:
        The active, configured sqlite3.Connection with tables and indices created.
    """
    if conn is None:
        conn = create_connection(db_path=db_path)

    with conn:
        conn.executescript(SCHEMA_DDL)

    logger.debug(
        "Initialized database schema with tables: %s",
        ", ".join(EXPECTED_TABLES),
    )
    return conn


# ==============================================================================
# Thread-Safe Context Manager & Connection Pool
# ==============================================================================


@contextmanager
def get_db_connection(
    db_path: Optional[Union[str, Path]] = None,
    auto_commit: bool = True,
) -> Iterator[sqlite3.Connection]:
    """Context manager yielding a standalone SQLite connection with automatic PRAGMA settings.

    Automatically commits transaction on normal exit, rolls back on unhandled exceptions,
    and closes the connection upon completion (unless operating on a persistent pool).

    Args:
        db_path: Target database path or ':memory:'.
        auto_commit: If True, commits transaction upon successful exit.

    Yields:
        Configured sqlite3.Connection instance.
    """
    conn = create_connection(db_path=db_path)
    try:
        if auto_commit:
            with conn:
                yield conn
        else:
            yield conn
    finally:
        conn.close()


class DatabaseManager:
    """Thread-safe SQLite connection manager and pool with PRAGMA optimizations.

    Manages connection lifecycle, thread safety, connection pooling for disk databases,
    and persistent shared connection handling for in-memory databases.
    """

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        max_connections: int = 10,
        timeout: float = 30.0,
    ) -> None:
        """Initialize DatabaseManager with target path and connection pool capacity.

        Args:
            db_path: Path to SQLite file or ':memory:'. Defaults to DB_PATH.
            max_connections: Maximum pool capacity for concurrent connections.
            timeout: Busy timeout in seconds.
        """
        self.db_path = str(db_path) if db_path is not None else str(DB_PATH)
        self._is_memory = self.db_path == ":memory:"
        self.max_connections = max(1, max_connections)
        self.timeout = timeout

        self._lock = threading.RLock()
        self._pool: queue.Queue[sqlite3.Connection] = queue.Queue(maxsize=self.max_connections)
        self._created_connections = 0
        self._all_connections: List[sqlite3.Connection] = []
        self._closed = False

        # In-memory databases must maintain a single persistent connection
        # so that schema and data persist across queries.
        self._memory_conn: Optional[sqlite3.Connection] = None
        if self._is_memory:
            self._memory_conn = create_connection(":memory:", timeout=self.timeout)
            init_db(conn=self._memory_conn)
        else:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            # Initialize schema on disk database
            with self.get_connection() as conn:
                init_db(conn=conn)

    def init_db(self) -> None:
        """Initializes or verifies schema tables and indices."""
        with self.get_connection() as conn:
            init_db(conn=conn)

    def _acquire_connection(self) -> sqlite3.Connection:
        """Acquires a connection from pool or creates a new one under capacity limits."""
        if self._closed:
            raise RuntimeError("DatabaseManager has been closed")

        if self._is_memory and self._memory_conn is not None:
            return self._memory_conn

        # Check if an idle connection is available in pool
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass

        with self._lock:
            if self._created_connections < self.max_connections:
                conn = create_connection(self.db_path, timeout=self.timeout)
                self._created_connections += 1
                self._all_connections.append(conn)
                return conn

        # Wait for a connection to be returned to pool
        try:
            return self._pool.get(timeout=self.timeout)
        except queue.Empty:
            raise TimeoutError(
                f"Timed out after {self.timeout}s waiting for available database connection"
            )

    def _release_connection(self, conn: sqlite3.Connection) -> None:
        """Returns connection back to pool."""
        if self._is_memory:
            return

        if self._closed:
            try:
                conn.close()
            except Exception:
                pass
            return

        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            # Capacity exceeded, close excess connection
            try:
                conn.close()
            except Exception:
                pass

    @contextmanager
    def get_connection(self, auto_commit: bool = True) -> Iterator[sqlite3.Connection]:
        """Context manager for acquiring a thread-safe connection from the pool.

        Args:
            auto_commit: If True, commits the transaction on normal block exit.

        Yields:
            sqlite3.Connection ready for querying.
        """
        if self._is_memory:
            # Re-entrant lock guards the single in-memory connection across threads
            with self._lock:
                conn = self._memory_conn
                assert conn is not None
                # Re-apply foreign keys pragma in case
                conn.execute("PRAGMA foreign_keys = ON;")
                try:
                    if auto_commit:
                        with conn:
                            yield conn
                    else:
                        yield conn
                except Exception:
                    conn.rollback()
                    raise
            return

        conn = self._acquire_connection()
        try:
            # Ensure foreign keys are ON for the checkout
            conn.execute("PRAGMA foreign_keys = ON;")
            if auto_commit:
                with conn:
                    yield conn
            else:
                yield conn
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._release_connection(conn)

    def execute(
        self,
        query: str,
        params: Union[Tuple[Any, ...], Dict[str, Any], List[Any]] = (),
    ) -> sqlite3.Cursor:
        """Executes a single query and returns the cursor."""
        with self.get_connection(auto_commit=True) as conn:
            return conn.execute(query, params)

    def executemany(
        self,
        query: str,
        seq_of_params: List[Union[Tuple[Any, ...], Dict[str, Any]]],
    ) -> sqlite3.Cursor:
        """Executes query against sequence of parameter sets."""
        with self.get_connection(auto_commit=True) as conn:
            return conn.executemany(query, seq_of_params)

    def fetchone(
        self,
        query: str,
        params: Union[Tuple[Any, ...], Dict[str, Any], List[Any]] = (),
    ) -> Optional[sqlite3.Row]:
        """Executes query and returns first row or None."""
        with self.get_connection(auto_commit=False) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchone()

    def fetchall(
        self,
        query: str,
        params: Union[Tuple[Any, ...], Dict[str, Any], List[Any]] = (),
    ) -> List[sqlite3.Row]:
        """Executes query and returns all matching rows."""
        with self.get_connection(auto_commit=False) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()

    def close(self) -> None:
        """Closes all connections in the pool and shuts down manager."""
        with self._lock:
            self._closed = True
            if self._memory_conn is not None:
                try:
                    self._memory_conn.close()
                except Exception:
                    pass
                self._memory_conn = None

            while not self._pool.empty():
                try:
                    conn = self._pool.get_nowait()
                    conn.close()
                except Exception:
                    pass

            for conn in self._all_connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_connections.clear()
            self._created_connections = 0

    def __enter__(self) -> "DatabaseManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# ==============================================================================
# Verification & Introspection Helpers
# ==============================================================================


def get_table_names(conn: sqlite3.Connection) -> List[str]:
    """Retrieves list of user table names in the database."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
    )
    return [row["name"] if isinstance(row, sqlite3.Row) else row[0] for row in cursor.fetchall()]


def get_index_names(conn: sqlite3.Connection) -> List[str]:
    """Retrieves list of non-automatic user index names in the database."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
    )
    return [row["name"] if isinstance(row, sqlite3.Row) else row[0] for row in cursor.fetchall()]


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> List[Dict[str, Any]]:
    """Retrieves PRAGMA table_info schema definition for a given table."""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name});")
    columns: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
        if isinstance(row, sqlite3.Row):
            columns.append(dict(row))
        else:
            columns.append({
                "cid": row[0],
                "name": row[1],
                "type": row[2],
                "notnull": row[3],
                "dflt_value": row[4],
                "pk": row[5],
            })
    return columns


def verify_schema(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Verifies that all expected tables and indices exist in the database.

    Returns:
        Dictionary containing 'valid', 'missing_tables', and 'missing_indices'.
    """
    tables = set(get_table_names(conn))
    indices = set(get_index_names(conn))

    missing_tables = [t for t in EXPECTED_TABLES if t not in tables]
    missing_indices = [idx for idx in EXPECTED_INDICES if idx not in indices]

    return {
        "valid": len(missing_tables) == 0 and len(missing_indices) == 0,
        "tables": list(tables),
        "indices": list(indices),
        "missing_tables": missing_tables,
        "missing_indices": missing_indices,
    }

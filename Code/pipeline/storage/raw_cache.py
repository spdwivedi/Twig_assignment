"""SQLite-backed raw cache storage for the Twig Ingestion Pipeline.

Caches raw HTTP responses and payload text to avoid redundant network requests,
facilitate offline debugging, and ensure idempotent scraping runs.
"""

import hashlib
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple, Union

from pipeline.config import DB_PATH


class RawCache:
    """Persistent SQLite-backed cache for raw job postings and web responses."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        """Initialize the RawCache with a specific database path or fallback to pipeline.config.DB_PATH."""
        if db_path is not None:
            self.db_path = str(db_path)
        else:
            self.db_path = str(DB_PATH)

        self._is_memory = self.db_path == ":memory:"
        self._memory_conn: Optional[sqlite3.Connection] = None
        if self._is_memory:
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row
        else:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection, reusing connection for in-memory databases."""
        if self._is_memory and self._memory_conn is not None:
            return self._memory_conn
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create raw_cache table and necessary indexes if they do not exist."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_cache (
                    cache_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    url TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_raw_cache_source ON raw_cache (source)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_raw_cache_url ON raw_cache (url)"
            )

    @staticmethod
    def generate_cache_id(url: str, content: str) -> str:
        """Generate SHA-256 hash of URL + first 500 characters of content.

        Args:
            url: The canonical request or job posting URL.
            content: The raw body, HTML, or JSON payload string.

        Returns:
            64-character hexadecimal SHA-256 hash.
        """
        snippet = (content or "")[:500]
        data = f"{url}{snippet}".encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def save(self, source: str, url: str, payload: str) -> str:
        """Insert or replace a raw response payload into the cache.

        Args:
            source: Source identifier (e.g. 'greenhouse', 'lever', 'hn').
            url: The fetch URL or posting identifier.
            payload: Raw text/JSON payload.

        Returns:
            The generated cache_id for this entry.
        """
        cache_id = self.generate_cache_id(url, payload)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO raw_cache (cache_id, source, url, payload, fetched_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (cache_id, source, url, payload),
            )
        return cache_id

    def save_batch(self, items: List[Tuple[str, str, str]]) -> List[str]:
        """Insert or replace a batch of raw records in a single database transaction.

        Args:
            items: List of (source, url, payload) tuples.

        Returns:
            List of generated cache_ids in matching order.
        """
        if not items:
            return []

        cache_ids: List[str] = []
        rows: List[Tuple[str, str, str, str]] = []
        for source, url, payload in items:
            cid = self.generate_cache_id(url, payload)
            cache_ids.append(cid)
            rows.append((cid, source, url, payload))

        with self._get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO raw_cache (cache_id, source, url, payload, fetched_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                rows,
            )
        return cache_ids

    def get(self, cache_id: str) -> Optional[str]:
        """Retrieve cached payload by cache_id.

        Args:
            cache_id: The SHA-256 cache identifier.

        Returns:
            The raw payload string if found, otherwise None.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT payload FROM raw_cache WHERE cache_id = ?",
                (cache_id,),
            )
            row = cursor.fetchone()
            return row["payload"] if row else None

    def get_by_url(self, url: str) -> Optional[str]:
        """Retrieve most recently cached payload for a given URL.

        Args:
            url: The request URL.

        Returns:
            The payload string if cached, otherwise None.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT payload FROM raw_cache WHERE url = ? ORDER BY fetched_at DESC LIMIT 1",
                (url,),
            )
            row = cursor.fetchone()
            return row["payload"] if row else None

    def has(self, cache_id: str) -> bool:
        """Check if a cache_id exists in the cache.

        Args:
            cache_id: The SHA-256 cache identifier.

        Returns:
            True if present, False otherwise.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM raw_cache WHERE cache_id = ? LIMIT 1",
                (cache_id,),
            )
            return cursor.fetchone() is not None

    def count(self, source: Optional[str] = None) -> int:
        """Return total number of cached entries, optionally filtered by source.

        Args:
            source: Optional source identifier to filter by.

        Returns:
            Integer count of cached records.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if source:
                cursor.execute(
                    "SELECT COUNT(*) FROM raw_cache WHERE source = ?",
                    (source,),
                )
            else:
                cursor.execute("SELECT COUNT(*) FROM raw_cache")
            row = cursor.fetchone()
            return row[0] if row else 0

    def clear(self, source: Optional[str] = None) -> int:
        """Clear cache entries, optionally by source.

        Args:
            source: If provided, only delete entries from this source.

        Returns:
            Number of rows deleted.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if source:
                cursor.execute(
                    "DELETE FROM raw_cache WHERE source = ?",
                    (source,),
                )
            else:
                cursor.execute("DELETE FROM raw_cache")
            return cursor.rowcount

    def close(self) -> None:
        """Close database connection if in memory."""
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None

    def __enter__(self) -> "RawCache":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

"""
queue_manager.py — SQLite-backed persistent queue

Features:
  - No data loss on container restart or crash
  - Exponential backoff retry: 10s → 20s → 40s → ... (max 10 retries)
  - WAL mode for concurrent read/write
  - Completed items retained for audit trail

Schema:
  sync_queue(id, event_type, payload, retry_count,
             next_retry_at, created_at, status,
             firestore_doc_id, storage_urls, error_message)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

DB_PATH_DEFAULT = "/app/data/queue.db"
MAX_RETRIES = 10
BASE_BACKOFF_SEC = 10.0


@dataclass
class QueueItem:
    id: int
    event_type: str
    payload: dict
    retry_count: int
    next_retry_at: float
    created_at: float
    status: str  # pending / processing / done / failed
    firestore_doc_id: Optional[str]
    storage_urls: list[str]
    error_message: Optional[str]


class QueueManager:
    """
    SQLite-backed persistent local queue.
    Inserts: API thread
    Consumption: background worker thread
    Concurrent writes serialized via threading.Lock
    """

    def __init__(self, db_path: str = DB_PATH_DEFAULT) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_queue (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type       TEXT    NOT NULL,
                    payload          TEXT    NOT NULL,
                    retry_count      INTEGER NOT NULL DEFAULT 0,
                    next_retry_at    REAL    NOT NULL,
                    created_at       REAL    NOT NULL,
                    status           TEXT    NOT NULL DEFAULT 'pending',
                    firestore_doc_id TEXT,
                    storage_urls     TEXT    DEFAULT '[]',
                    error_message    TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_status_retry "
                "ON sync_queue(status, next_retry_at)"
            )
            conn.commit()

    def enqueue(self, event_type: str, payload: dict) -> int:
        now = time.time()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO sync_queue
                   (event_type, payload, next_retry_at, created_at, status)
                   VALUES (?, ?, ?, ?, 'pending')""",
                (event_type, json.dumps(payload), now, now),
            )
            conn.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    def dequeue_ready(self) -> list[QueueItem]:
        now = time.time()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM sync_queue
                   WHERE status = 'pending' AND next_retry_at <= ?
                   ORDER BY created_at ASC LIMIT 5""",
                (now,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE sync_queue SET status='processing' WHERE id IN ({placeholders})",
                    ids,
                )
                conn.commit()
            return [self._row_to_item(r) for r in rows]

    def mark_done(
        self,
        item_id: int,
        firestore_doc_id: str,
        storage_urls: list[str],
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE sync_queue
                   SET status='done', firestore_doc_id=?, storage_urls=?
                   WHERE id=?""",
                (firestore_doc_id, json.dumps(storage_urls), item_id),
            )
            conn.commit()

    def mark_failed(self, item_id: int, error: str, retry: bool = True) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT retry_count FROM sync_queue WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                return
            retry_count = row["retry_count"] + 1
            if retry and retry_count <= MAX_RETRIES:
                backoff = BASE_BACKOFF_SEC * (2 ** (retry_count - 1))
                conn.execute(
                    """UPDATE sync_queue
                       SET status='pending', retry_count=?, next_retry_at=?,
                           error_message=?
                       WHERE id=?""",
                    (retry_count, time.time() + backoff, error, item_id),
                )
            else:
                conn.execute(
                    """UPDATE sync_queue
                       SET status='failed', retry_count=?, error_message=?
                       WHERE id=?""",
                    (retry_count, error, item_id),
                )
            conn.commit()

    def get_item(self, item_id: int) -> Optional[QueueItem]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_queue WHERE id=?", (item_id,)
            ).fetchone()
        return self._row_to_item(row) if row else None

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM sync_queue GROUP BY status"
            ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> QueueItem:
        import datetime as dt
        return QueueItem(
            id=row["id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload"]),
            retry_count=row["retry_count"],
            next_retry_at=row["next_retry_at"],
            created_at=row["created_at"],
            status=row["status"],
            firestore_doc_id=row["firestore_doc_id"],
            storage_urls=json.loads(row["storage_urls"] or "[]"),
            error_message=row["error_message"],
        )

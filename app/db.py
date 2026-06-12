from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


@dataclass
class WorkRecord:
    work_id: str
    calibre_book_id: int | None
    title: str | None
    author: str | None
    source_url: str
    status: str
    message: str | None
    date_added: str
    last_updated: str


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS works (
                    work_id TEXT PRIMARY KEY,
                    calibre_book_id INTEGER,
                    title TEXT,
                    author TEXT,
                    source_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    date_added TEXT NOT NULL,
                    last_updated TEXT NOT NULL
                )
                """
            )

    def get_work(self, work_id: str) -> WorkRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM works WHERE work_id = ?", (work_id,)).fetchone()
        return WorkRecord(**dict(row)) if row else None

    def upsert_work(
        self,
        *,
        work_id: str,
        source_url: str,
        status: str,
        calibre_book_id: int | None = None,
        title: str | None = None,
        author: str | None = None,
        message: str | None = None,
    ) -> WorkRecord:
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get_work(work_id)
        date_added = existing.date_added if existing else now
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO works (
                    work_id, calibre_book_id, title, author, source_url,
                    status, message, date_added, last_updated
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_id) DO UPDATE SET
                    calibre_book_id = excluded.calibre_book_id,
                    title = COALESCE(excluded.title, works.title),
                    author = COALESCE(excluded.author, works.author),
                    source_url = excluded.source_url,
                    status = excluded.status,
                    message = excluded.message,
                    last_updated = excluded.last_updated
                """,
                (
                    work_id,
                    calibre_book_id if calibre_book_id is not None else (existing.calibre_book_id if existing else None),
                    title,
                    author,
                    source_url,
                    status,
                    message,
                    date_added,
                    now,
                ),
            )
        return self.get_work(work_id)  # type: ignore[return-value]

    def recent(self, limit: int = 25) -> list[WorkRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM works ORDER BY last_updated DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [WorkRecord(**dict(row)) for row in rows]

import datetime
import os
import sqlite3
from typing import Any


class SqliteReportRepository:
    def __init__(self, db_path: str = "data/reports.db"):
        """Initialize the repository with the specified database path."""
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    date DATE NOT NULL,
                    channel_id TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    md_path TEXT,
                    pdf_path TEXT,
                    summary TEXT
                )
            """)
            conn.commit()

    def add_report(
        self,
        date: datetime.date,
        channel_id: str,
        task_name: str,
        md_path: str | None = None,
        pdf_path: str | None = None,
        summary: str | None = None
    ):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO reports (date, channel_id, task_name, md_path, pdf_path, summary) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (date.isoformat(), str(channel_id), task_name, md_path, pdf_path, summary),
            )
            conn.commit()

    def get_latest_reports(self, limit: int = 10) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM reports ORDER BY timestamp DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

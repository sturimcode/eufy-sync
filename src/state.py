from __future__ import annotations

import sqlite3
from pathlib import Path


class SyncState:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path))
        self._init_db()

    def _init_db(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name TEXT NOT NULL,
                eufy_measurement_id TEXT NOT NULL,
                measurement_timestamp TEXT NOT NULL,
                weight_kg REAL,
                synced_to_garmin_at TEXT NOT NULL,
                garmin_response TEXT,
                UNIQUE(user_name, eufy_measurement_id)
            );
        """)
        self._conn.commit()

    def is_synced(self, user_name: str, measurement_id: str) -> bool:
        cursor = self._conn.execute(
            "SELECT 1 FROM sync_log WHERE user_name = ? AND eufy_measurement_id = ?",
            (user_name, measurement_id),
        )
        return cursor.fetchone() is not None

    def record_sync(
        self,
        user_name: str,
        measurement_id: str,
        measurement_timestamp: str,
        weight_kg: float,
        synced_at: str,
        garmin_response: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO sync_log
               (user_name, eufy_measurement_id, measurement_timestamp, weight_kg, synced_to_garmin_at, garmin_response)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_name, measurement_id, measurement_timestamp, weight_kg, synced_at, garmin_response),
        )
        self._conn.commit()

    def get_latest_sync_timestamp(self, user_name: str) -> int | None:
        """Return the unix timestamp of the most recent synced measurement, or None."""
        cursor = self._conn.execute(
            "SELECT MAX(measurement_timestamp) FROM sync_log WHERE user_name = ?",
            (user_name,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            from datetime import datetime
            dt = datetime.fromisoformat(row[0])
            return int(dt.timestamp())
        return None

    def close(self) -> None:
        self._conn.close()

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path


class SyncState:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path))
        self._migrate_if_needed()
        self._init_db()

    def _init_db(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name TEXT NOT NULL,
                eufy_measurement_id TEXT NOT NULL,
                measurement_timestamp TEXT NOT NULL,
                weight_kg REAL,
                target TEXT NOT NULL DEFAULT 'garmin',
                synced_at TEXT NOT NULL,
                response TEXT,
                UNIQUE(user_name, eufy_measurement_id, target)
            );
        """)
        self._conn.commit()

    def _migrate_if_needed(self) -> None:
        """Migrate v1 schema (garmin-only) to v2 (multi-target)."""
        cursor = self._conn.execute("PRAGMA table_info(sync_log)")
        columns = {row[1] for row in cursor.fetchall()}
        if not columns:
            return  # Table doesn't exist yet, _init_db will create it
        if "target" in columns:
            return  # Already migrated

        with self._conn:
            self._conn.execute("""
                CREATE TABLE sync_log_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_name TEXT NOT NULL,
                    eufy_measurement_id TEXT NOT NULL,
                    measurement_timestamp TEXT NOT NULL,
                    weight_kg REAL,
                    target TEXT NOT NULL DEFAULT 'garmin',
                    synced_at TEXT NOT NULL,
                    response TEXT,
                    UNIQUE(user_name, eufy_measurement_id, target)
                )
            """)
            self._conn.execute("""
                INSERT INTO sync_log_v2 (
                    user_name, eufy_measurement_id, measurement_timestamp,
                    weight_kg, target, synced_at, response
                )
                SELECT
                    user_name, eufy_measurement_id, measurement_timestamp,
                    weight_kg, 'garmin', synced_to_garmin_at, garmin_response
                FROM sync_log
            """)
            self._conn.execute("DROP TABLE sync_log")
            self._conn.execute("ALTER TABLE sync_log_v2 RENAME TO sync_log")

    def has_any_syncs(self, user_name: str, target: str) -> bool:
        """Check if a target has ever been synced to."""
        cursor = self._conn.execute(
            "SELECT 1 FROM sync_log WHERE user_name = ? AND target = ? LIMIT 1",
            (user_name, target),
        )
        return cursor.fetchone() is not None

    def is_synced(self, user_name: str, measurement_id: str, target: str) -> bool:
        cursor = self._conn.execute(
            "SELECT 1 FROM sync_log WHERE user_name = ? AND eufy_measurement_id = ? AND target = ?",
            (user_name, measurement_id, target),
        )
        return cursor.fetchone() is not None

    def has_synced_on_date(self, user_name: str, target: str, local_date: date) -> bool:
        """Whether WE have already synced something to target for this local
        calendar date. Used to tell "our own earlier upload today" apart from
        "another source already has this date in Garmin" (see the same-date
        guard in sync.py). The table is small, so the date comparison is done
        in Python rather than as date math against mixed-tz ISO strings in
        SQL.
        """
        cursor = self._conn.execute(
            "SELECT measurement_timestamp FROM sync_log WHERE user_name = ? AND target = ?",
            (user_name, target),
        )
        for (ts,) in cursor.fetchall():
            if datetime.fromisoformat(ts).astimezone().date() == local_date:
                return True
        return False

    def record_sync(
        self,
        user_name: str,
        measurement_id: str,
        measurement_timestamp: str,
        weight_kg: float,
        synced_at: str,
        target: str = "garmin",
        response: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO sync_log
               (user_name, eufy_measurement_id, measurement_timestamp, weight_kg, target, synced_at, response)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_name, measurement_id, measurement_timestamp, weight_kg, target, synced_at, response),
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

    def get_history(self, user_name: str, limit: int = 14) -> list[dict]:
        """Return recent synced measurements with per-target status."""
        cursor = self._conn.execute(
            """SELECT measurement_timestamp, weight_kg, GROUP_CONCAT(target) as targets
               FROM sync_log
               WHERE user_name = ?
               GROUP BY eufy_measurement_id
               ORDER BY measurement_timestamp DESC
               LIMIT ?""",
            (user_name, limit),
        )
        results = []
        for row in cursor.fetchall():
            synced_targets = set(row[2].split(",")) if row[2] else set()
            results.append({
                "timestamp": row[0],
                "weight_kg": row[1],
                "targets": synced_targets,
            })
        return results

    def close(self) -> None:
        self._conn.close()

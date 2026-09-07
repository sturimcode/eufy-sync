from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

# Response recorded when an upload is skipped because Garmin already holds an
# entry for that date from another source. Such rows dedup the measurement
# (is_synced) but do not count as "we uploaded on this date".
SKIPPED_IN_GARMIN_RESPONSE = '{"skipped": "already_in_garmin"}'


def _date_window(local_date: date) -> tuple[str, str]:
    """ISO-string bounds that contain every timestamp whose local date could
    be local_date. Offsets are under a day, so the string's date part differs
    from the local date by at most one; ISO strings compare lexicographically.
    """
    return (local_date - timedelta(days=1)).isoformat(), (local_date + timedelta(days=2)).isoformat()


class SyncState:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
                weight_only INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_name, eufy_measurement_id, target)
            );
            CREATE TABLE IF NOT EXISTS pending_upgrades (
                user_name TEXT NOT NULL,
                measurement_id TEXT NOT NULL,
                previous_measurement_id TEXT NOT NULL,
                measurement_json TEXT NOT NULL,
                PRIMARY KEY(user_name, measurement_id)
            );
        """)
        self._conn.commit()

    def _migrate_if_needed(self) -> None:
        """Migrate v1 schema (garmin-only) to v2 (multi-target), then v2 to v3
        (weight_only flag)."""
        self._migrate_multi_target()
        self._migrate_weight_only_column()

    def _migrate_multi_target(self) -> None:
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

    def _migrate_weight_only_column(self) -> None:
        """v2 -> v3: add the weight_only flag. Existing rows predate the raw
        Wi-Fi fallback, so 0 (full record) is correct for all of them."""
        cursor = self._conn.execute("PRAGMA table_info(sync_log)")
        columns = {row[1] for row in cursor.fetchall()}
        if not columns or "weight_only" in columns:
            return
        with self._conn:
            self._conn.execute(
                "ALTER TABLE sync_log ADD COLUMN weight_only INTEGER NOT NULL DEFAULT 0"
            )

    def is_synced(self, user_name: str, measurement_id: str, target: str) -> bool:
        cursor = self._conn.execute(
            "SELECT 1 FROM sync_log WHERE user_name = ? AND eufy_measurement_id = ? AND target = ?",
            (user_name, measurement_id, target),
        )
        return cursor.fetchone() is not None

    def has_synced_on_date(self, user_name: str, target: str, local_date: date) -> bool:
        """Whether WE have already uploaded something to target for this local
        calendar date. Used to tell "our own earlier upload today" apart from
        "another source already has this date in Garmin" (see the same-date
        guard in sync.py). Skipped-because-already-in-Garmin rows do not
        count: treating them as uploads would disable the guard for a second
        measurement on the same day and create the very duplicate the guard
        exists to prevent. SQL narrows to a two-day window; the exact check
        against mixed-tz ISO strings stays in Python.
        """
        lo, hi = _date_window(local_date)
        cursor = self._conn.execute(
            "SELECT measurement_timestamp FROM sync_log"
            " WHERE user_name = ? AND target = ?"
            " AND measurement_timestamp >= ? AND measurement_timestamp < ?"
            " AND (response IS NULL OR response != ?)",
            (user_name, target, lo, hi, SKIPPED_IN_GARMIN_RESPONSE),
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
        weight_only: bool = False,
    ) -> None:
        self._conn.execute(
            """INSERT INTO sync_log
               (user_name, eufy_measurement_id, measurement_timestamp, weight_kg, target, synced_at, response, weight_only)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_name, measurement_id, measurement_timestamp, weight_kg, target, synced_at, response, int(weight_only)),
        )
        self._conn.commit()

    def weight_only_syncs_on_date(self, user_name: str, target: str, local_date: date) -> list[dict]:
        """Weight-only (raw Wi-Fi) syncs for a local calendar date that have
        not been upgraded to a full record yet. Each dict carries
        measurement_id, measurement_timestamp, and weight_kg. Windowed in SQL,
        exact date check in Python, as in has_synced_on_date."""
        lo, hi = _date_window(local_date)
        cursor = self._conn.execute(
            """SELECT eufy_measurement_id, measurement_timestamp, weight_kg
               FROM sync_log
               WHERE user_name = ? AND target = ? AND weight_only = 1
                 AND measurement_timestamp >= ? AND measurement_timestamp < ?""",
            (user_name, target, lo, hi),
        )
        return [
            {"measurement_id": mid, "measurement_timestamp": ts, "weight_kg": kg}
            for mid, ts, kg in cursor.fetchall()
            if datetime.fromisoformat(ts).astimezone().date() == local_date
        ]

    def mark_upgraded(self, user_name: str, measurement_id: str, target: str) -> None:
        """Clear a sync's weight-only flag once its full body-comp record has
        replaced it in the target. The row stays, so the raw record itself
        never re-syncs."""
        self._conn.execute(
            """UPDATE sync_log SET weight_only = 0
               WHERE user_name = ? AND eufy_measurement_id = ? AND target = ?""",
            (user_name, measurement_id, target),
        )
        self._conn.commit()

    def save_pending_upgrade(self, user_name: str, previous_id: str, measurement: dict) -> None:
        """Commit the replacement before the remote weight-only entry is deleted."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_upgrades VALUES (?, ?, ?, ?)",
                (user_name, measurement["measurement_id"], previous_id, json.dumps(measurement)),
            )

    def get_pending_upgrades(self, user_name: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT previous_measurement_id, measurement_json FROM pending_upgrades WHERE user_name = ?",
            (user_name,),
        )
        return [{"previous_id": previous_id, "measurement": json.loads(payload)} for previous_id, payload in rows]

    def clear_pending_upgrade(self, user_name: str, measurement_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM pending_upgrades WHERE user_name = ? AND measurement_id = ?",
                (user_name, measurement_id),
            )

    def get_oldest_weight_only_timestamp(self, user_name: str, target: str) -> int | None:
        rows = self._conn.execute(
            "SELECT measurement_timestamp FROM sync_log WHERE user_name = ? AND target = ? AND weight_only = 1",
            (user_name, target),
        )
        timestamps = [datetime.fromisoformat(row[0]).timestamp() for row in rows]
        return int(min(timestamps)) if timestamps else None

    def get_latest_sync_timestamp(self, user_name: str, target: str | None = None) -> int | None:
        """Return the unix timestamp of the most recent synced measurement,
        or None. With target, only that target's rows count - each target
        keeps its own fetch cursor so one target's progress cannot hide
        measurements another target missed while it was down.
        """
        query = "SELECT MAX(measurement_timestamp) FROM sync_log WHERE user_name = ?"
        params: tuple = (user_name,)
        if target is not None:
            query += " AND target = ?"
            params = (user_name, target)
        row = self._conn.execute(query, params).fetchone()
        if row and row[0]:
            return int(datetime.fromisoformat(row[0]).timestamp())
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

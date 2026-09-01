"""Optional BLE RSSI experiment recorder and server sync client.

This module belongs to the long-lived ``feat/ble-rssi-measurement`` experiment
branch. Enabling it is never required for normal door-lock operation. While it
is enabled every valid RSSI observation is stored locally; a run is only a
nullable experiment label, not a recording switch.
"""

import argparse
import csv
import hashlib
import hmac
import json
import logging
import queue
import random
import sqlite3
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

SCHEMA_VERSION = 1
AGENT_VERSION = "measurement-v2"
DEFAULT_BATCH_SIZE = 100
DEFAULT_QUEUE_SIZE = 10_000
DEFAULT_OUTSIDE_RUN_RETENTION_HOURS = 24


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class PendingObservation:
    event_id: str
    schema_version: int
    pi_id: str
    boot_id: str
    sequence: int
    observed_at: str
    monotonic_ns: int
    device_tag: str
    packet_kind: str
    service_uuid: str
    rssi: int
    pi_phase: str


@dataclass(frozen=True)
class FlushRequest:
    completed: threading.Event


class SQLiteMeasurementStore:
    def __init__(self, path: str):
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    expected_result TEXT NOT NULL CHECK (expected_result IN ('allow', 'deny')),
                    target_device_tag TEXT NOT NULL,
                    position TEXT NOT NULL,
                    carrying TEXT NOT NULL,
                    body_orientation TEXT NOT NULL,
                    other_devices TEXT NOT NULL,
                    notes TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_measurement_run
                    ON runs ((1)) WHERE ended_at IS NULL;
                CREATE TABLE IF NOT EXISTS recent_devices (
                    device_tag TEXT PRIMARY KEY,
                    rssi INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    packet_kind TEXT NOT NULL,
                    service_uuid TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS measurement_stats (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS measurement_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    applied_desired_version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO measurement_state VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS run_updates (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'COMPLETED')),
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    expected_result TEXT NOT NULL,
                    target_device_tag TEXT NOT NULL,
                    position TEXT NOT NULL,
                    carrying TEXT NOT NULL,
                    body_orientation TEXT NOT NULL,
                    other_devices TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    acked_at TEXT,
                    PRIMARY KEY (run_id, revision)
                );
                """
            )
            self._initialize_observations(connection)
            self._seed_legacy_run_updates(connection)

    @staticmethod
    def _initialize_observations(connection: sqlite3.Connection) -> None:
        columns = connection.execute("PRAGMA table_info(observations)").fetchall()
        run_id_column = next((row for row in columns if row["name"] == "run_id"), None)
        if run_id_column is not None and run_id_column["notnull"]:
            connection.executescript(
                """
                ALTER TABLE observations RENAME TO observations_legacy;
                DROP INDEX IF EXISTS observations_by_run_and_time;
                DROP INDEX IF EXISTS observations_pending_upload;
                CREATE TABLE observations (
                    event_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                    run_id TEXT REFERENCES runs(run_id), pi_id TEXT NOT NULL,
                    boot_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    observed_at TEXT NOT NULL, monotonic_ns INTEGER NOT NULL,
                    device_tag TEXT NOT NULL, packet_kind TEXT NOT NULL,
                    service_uuid TEXT NOT NULL, rssi INTEGER NOT NULL,
                    pi_phase TEXT NOT NULL, uploaded_at TEXT
                );
                INSERT INTO observations SELECT * FROM observations_legacy;
                DROP TABLE observations_legacy;
                """
            )
        elif not columns:
            connection.execute(
                """
                CREATE TABLE observations (
                    event_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                    run_id TEXT REFERENCES runs(run_id), pi_id TEXT NOT NULL,
                    boot_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    observed_at TEXT NOT NULL, monotonic_ns INTEGER NOT NULL,
                    device_tag TEXT NOT NULL, packet_kind TEXT NOT NULL,
                    service_uuid TEXT NOT NULL, rssi INTEGER NOT NULL,
                    pi_phase TEXT NOT NULL, uploaded_at TEXT
                )
                """
            )
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS observations_by_run_and_time
                ON observations (run_id, monotonic_ns);
            CREATE INDEX IF NOT EXISTS observations_pending_upload
                ON observations (uploaded_at, observed_at);
            """
        )

    @staticmethod
    def _seed_legacy_run_updates(connection: sqlite3.Connection) -> None:
        base = """
            INSERT OR IGNORE INTO run_updates (
                run_id, revision, status, started_at, ended_at, expected_result,
                target_device_tag, position, carrying, body_orientation,
                other_devices, notes
            )
        """
        connection.execute(base + """
            SELECT run_id, 1, 'ACTIVE', started_at, NULL, expected_result,
                   target_device_tag, position, carrying, body_orientation,
                   other_devices, notes FROM runs
        """)
        connection.execute(base + """
            SELECT run_id, 2, 'COMPLETED', started_at, ended_at, expected_result,
                   target_device_tag, position, carrying, body_orientation,
                   other_devices, notes FROM runs WHERE ended_at IS NOT NULL
        """)

    def start_run(self, *, expected_result: str, target_device_tag: str,
                  position: str, carrying: str, body_orientation: str,
                  other_devices: list[str], notes: str,
                  started_at: Optional[str] = None,
                  run_id: Optional[str] = None) -> str:
        if expected_result not in {"allow", "deny"}:
            raise ValueError("expected_result must be 'allow' or 'deny'")
        run_id = run_id or str(uuid.uuid4())
        try:
            with self._connection() as connection:
                self._insert_run(
                    connection, run_id=run_id, started_at=started_at or utc_now(),
                    expected_result=expected_result,
                    target_device_tag=target_device_tag, position=position,
                    carrying=carrying, body_orientation=body_orientation,
                    other_devices_json=json.dumps(other_devices, ensure_ascii=False),
                    notes=notes,
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("another measurement run is already active or run ID exists") from error
        return run_id

    @staticmethod
    def _insert_run(connection: sqlite3.Connection, *, run_id: str,
                    started_at: str, expected_result: str,
                    target_device_tag: str, position: str, carrying: str,
                    body_orientation: str, other_devices_json: str,
                    notes: str) -> None:
        values = (run_id, started_at, expected_result, target_device_tag,
                  position, carrying, body_orientation, other_devices_json, notes)
        connection.execute(
            """INSERT INTO runs (
                   run_id, started_at, expected_result, target_device_tag,
                   position, carrying, body_orientation, other_devices, notes
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", values)
        connection.execute(
            """INSERT INTO run_updates (
                   run_id, revision, status, started_at, ended_at,
                   expected_result, target_device_tag, position, carrying,
                   body_orientation, other_devices, notes
               ) VALUES (?, 1, 'ACTIVE', ?, NULL, ?, ?, ?, ?, ?, ?, ?)""", values)

    def stop_active_run(self, ended_at: Optional[str] = None) -> str:
        ended_at = ended_at or utc_now()
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE ended_at IS NULL").fetchone()
            if row is None:
                raise ValueError("no active measurement run")
            self._complete_run(connection, row, ended_at)
            return row["run_id"]

    @staticmethod
    def _complete_run(connection: sqlite3.Connection, row: sqlite3.Row,
                      ended_at: str) -> None:
        connection.execute("UPDATE runs SET ended_at = ? WHERE run_id = ?",
                           (ended_at, row["run_id"]))
        connection.execute(
            """INSERT INTO run_updates (
                   run_id, revision, status, started_at, ended_at,
                   expected_result, target_device_tag, position, carrying,
                   body_orientation, other_devices, notes
               ) VALUES (?, 2, 'COMPLETED', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["run_id"], row["started_at"], ended_at,
             row["expected_result"], row["target_device_tag"], row["position"],
             row["carrying"], row["body_orientation"], row["other_devices"],
             row["notes"]),
        )

    def apply_desired_state(self, desired_state: dict) -> bool:
        if not isinstance(desired_state, dict) or not isinstance(desired_state.get("version"), int):
            raise ValueError("measurement server response must contain desiredState.version")
        if desired_state["version"] < 0 or "run" not in desired_state:
            raise ValueError("invalid desiredState")
        with self._connection() as connection:
            current = connection.execute(
                "SELECT applied_desired_version FROM measurement_state WHERE singleton = 1"
            ).fetchone()[0]
            if desired_state["version"] <= current:
                return False
            active = connection.execute("SELECT * FROM runs WHERE ended_at IS NULL").fetchone()
            desired_run = desired_state["run"]
            if desired_run is None:
                if active is not None:
                    self._complete_run(connection, active, utc_now())
            else:
                required = {"runId", "expectedResult", "targetDeviceTag", "position",
                            "carrying", "bodyOrientation", "otherDevices", "notes"}
                if not isinstance(desired_run, dict) or not required.issubset(desired_run):
                    raise ValueError("desiredState.run is missing required fields")
                if active is not None and active["run_id"] != desired_run["runId"]:
                    raise ValueError("desired state conflicts with active measurement run")
                if active is None:
                    self._insert_run(
                        connection, run_id=desired_run["runId"], started_at=utc_now(),
                        expected_result=desired_run["expectedResult"],
                        target_device_tag=desired_run["targetDeviceTag"],
                        position=desired_run["position"], carrying=desired_run["carrying"],
                        body_orientation=desired_run["bodyOrientation"],
                        other_devices_json=json.dumps(desired_run["otherDevices"], ensure_ascii=False),
                        notes=desired_run["notes"])
            connection.execute(
                "UPDATE measurement_state SET applied_desired_version = ? WHERE singleton = 1",
                (desired_state["version"],))
        return True

    def status(self) -> dict:
        with self._connection() as connection:
            active = connection.execute("SELECT * FROM runs WHERE ended_at IS NULL").fetchone()
            version = connection.execute(
                "SELECT applied_desired_version FROM measurement_state WHERE singleton = 1"
            ).fetchone()[0]
            stats = {row["name"]: row["value"] for row in connection.execute(
                "SELECT name, value FROM measurement_stats ORDER BY name")}
            recent = [dict(row) for row in connection.execute(
                """SELECT device_tag, rssi, observed_at, packet_kind, service_uuid
                   FROM recent_devices ORDER BY observed_at DESC LIMIT 20""")]
        active_run = dict(active) if active is not None else None
        if active_run is not None:
            active_run["other_devices"] = json.loads(active_run["other_devices"])
        return {"appliedDesiredVersion": version, "activeRun": active_run,
                "stats": stats, "recentObservations": recent}

    def insert_observations(self, observations: list[PendingObservation],
                            stats: Optional[dict[str, int]] = None) -> None:
        if not observations and not stats:
            return
        written = outside_run = 0
        with self._connection() as connection:
            for observation in observations:
                connection.execute(
                    """INSERT INTO recent_devices
                           (device_tag, rssi, observed_at, packet_kind, service_uuid)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(device_tag) DO UPDATE SET
                           rssi=excluded.rssi, observed_at=excluded.observed_at,
                           packet_kind=excluded.packet_kind, service_uuid=excluded.service_uuid
                       WHERE excluded.observed_at >= recent_devices.observed_at""",
                    (observation.device_tag, observation.rssi, observation.observed_at,
                     observation.packet_kind, observation.service_uuid))
                run = connection.execute(
                    """SELECT run_id FROM runs
                       WHERE started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)
                       ORDER BY started_at DESC LIMIT 1""",
                    (observation.observed_at, observation.observed_at)).fetchone()
                run_id = run["run_id"] if run is not None else None
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO observations (
                           event_id, schema_version, run_id, pi_id, boot_id, sequence,
                           observed_at, monotonic_ns, device_tag, packet_kind,
                           service_uuid, rssi, pi_phase
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (observation.event_id, observation.schema_version, run_id,
                     observation.pi_id, observation.boot_id, observation.sequence,
                     observation.observed_at, observation.monotonic_ns,
                     observation.device_tag, observation.packet_kind,
                     observation.service_uuid, observation.rssi, observation.pi_phase))
                written += cursor.rowcount
                outside_run += int(run_id is None and cursor.rowcount == 1)
            increments = dict(stats or {})
            increments["written"] = increments.get("written", 0) + written
            increments["outside_run_written"] = increments.get("outside_run_written", 0) + outside_run
            self._increment_stats(connection, increments)

    @staticmethod
    def _increment_stats(connection: sqlite3.Connection,
                         increments: dict[str, int]) -> None:
        for name, value in increments.items():
            if value:
                connection.execute(
                    """INSERT INTO measurement_stats VALUES (?, ?)
                       ON CONFLICT(name) DO UPDATE SET value=value+excluded.value""",
                    (name, value))

    def export_csv(self, output_path: str, run_id: Optional[str] = None) -> int:
        query = """SELECT event_id, schema_version, run_id, pi_id, boot_id, sequence,
                          observed_at, monotonic_ns, device_tag, packet_kind,
                          service_uuid, rssi, pi_phase, uploaded_at FROM observations"""
        parameters: tuple = ()
        if run_id is not None:
            query += " WHERE run_id = ?"
            parameters = (run_id,)
        query += " ORDER BY observed_at, monotonic_ns"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        fieldnames = ["event_id", "schema_version", "run_id", "pi_id", "boot_id",
                      "sequence", "observed_at", "monotonic_ns", "device_tag",
                      "packet_kind", "service_uuid", "rssi", "pi_phase", "uploaded_at"]
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
        return len(rows)

    def fetch_unuploaded(self, limit: int = DEFAULT_BATCH_SIZE) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT event_id, schema_version, run_id, pi_id, boot_id, sequence,
                          observed_at, monotonic_ns, device_tag, packet_kind,
                          service_uuid, rssi, pi_phase FROM observations
                   WHERE uploaded_at IS NULL
                     AND boot_id = (
                         SELECT boot_id FROM observations
                         WHERE uploaded_at IS NULL
                         ORDER BY observed_at, monotonic_ns LIMIT 1
                     )
                   ORDER BY observed_at, monotonic_ns LIMIT ?""",
                (limit,)).fetchall()
        return [{"eventId": row["event_id"], "schemaVersion": row["schema_version"],
                 "runId": row["run_id"], "piId": row["pi_id"],
                 "bootId": row["boot_id"], "sequence": row["sequence"],
                 "observedAt": row["observed_at"], "monotonicNs": row["monotonic_ns"],
                 "deviceTag": row["device_tag"], "packetKind": row["packet_kind"],
                 "serviceUuid": row["service_uuid"], "rssi": row["rssi"],
                 "piPhase": row["pi_phase"]} for row in rows]

    def count_unuploaded(self) -> int:
        with self._connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM observations WHERE uploaded_at IS NULL").fetchone()[0]

    def fetch_unacked_run_updates(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM run_updates WHERE acked_at IS NULL ORDER BY started_at, revision"
            ).fetchall()
        return [{"runId": row["run_id"], "revision": row["revision"],
                 "status": row["status"], "startedAt": row["started_at"],
                 "endedAt": row["ended_at"], "expectedResult": row["expected_result"],
                 "targetDeviceTag": row["target_device_tag"], "position": row["position"],
                 "carrying": row["carrying"], "bodyOrientation": row["body_orientation"],
                 "otherDevices": json.loads(row["other_devices"]), "notes": row["notes"]}
                for row in rows]

    def sync_state(self) -> dict:
        with self._connection() as connection:
            version = connection.execute(
                "SELECT applied_desired_version FROM measurement_state WHERE singleton = 1"
            ).fetchone()[0]
            active = connection.execute("SELECT run_id FROM runs WHERE ended_at IS NULL").fetchone()
            stats = {row["name"]: row["value"] for row in connection.execute(
                "SELECT name, value FROM measurement_stats")}
        return {"appliedDesiredVersion": version,
                "activeRunId": active["run_id"] if active is not None else None,
                "stats": stats}

    def mark_uploaded(self, event_ids: list[str],
                      uploaded_at: Optional[str] = None) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE observations SET uploaded_at = ? WHERE event_id IN ({placeholders})",
                (uploaded_at or utc_now(), *event_ids))

    def mark_run_updates_acked(self, updates: list[tuple[str, int]]) -> None:
        if not updates:
            return
        acked_at = utc_now()
        with self._connection() as connection:
            connection.executemany(
                "UPDATE run_updates SET acked_at = ? WHERE run_id = ? AND revision = ?",
                [(acked_at, run_id, revision) for run_id, revision in updates])

    def prune_uploaded_outside_run(self,
                                   retention_hours: int = DEFAULT_OUTSIDE_RUN_RETENTION_HOURS) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        with self._connection() as connection:
            cursor = connection.execute(
                """DELETE FROM observations WHERE run_id IS NULL
                   AND uploaded_at IS NOT NULL AND observed_at < ?""", (cutoff,))
            return cursor.rowcount


class BleMeasurementRecorder:
    def __init__(self, store: SQLiteMeasurementStore, *, pi_id: str,
                 hash_key: bytes, queue_size: int = DEFAULT_QUEUE_SIZE,
                 logger: Optional[logging.Logger] = None):
        if not hash_key:
            raise ValueError("measurement hash key must not be empty")
        self.store = store
        self.pi_id = pi_id
        self.hash_key = hash_key
        self.boot_id = str(uuid.uuid4())
        self.logger = logger or logging.getLogger("ble-measurement")
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._stats: dict[str, int] = {}
        self._stats_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._writer_main,
                                            name="ble-measurement-writer", daemon=True)
            self._thread.start()

    def observe(self, *, raw_identifier: bytes, packet_kind: str,
                service_uuid: str, rssi: Optional[int], pi_phase: str,
                observed_at: Optional[str] = None,
                monotonic_ns: Optional[int] = None) -> bool:
        if rssi is None:
            self._add_stat("missing_rssi")
            return False
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        device_tag = hmac.new(self.hash_key, raw_identifier, hashlib.sha256).hexdigest()[:16]
        observation = PendingObservation(
            f"{self.boot_id}:{sequence}", SCHEMA_VERSION, self.pi_id, self.boot_id,
            sequence, observed_at or utc_now(),
            monotonic_ns if monotonic_ns is not None else time.monotonic_ns(),
            device_tag, packet_kind, service_uuid, int(rssi), pi_phase)
        try:
            self._queue.put_nowait(observation)
            return True
        except queue.Full:
            self._add_stat("queue_dropped")
            return False

    def flush(self, timeout: float = 5.0) -> bool:
        completed = threading.Event()
        try:
            self._queue.put(FlushRequest(completed), timeout=timeout)
        except queue.Full:
            return False
        return completed.wait(timeout)

    def close(self, timeout: float = 5.0) -> None:
        self.flush(timeout)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _add_stat(self, name: str) -> None:
        with self._stats_lock:
            self._stats[name] = self._stats.get(name, 0) + 1

    def _take_stats(self) -> dict[str, int]:
        with self._stats_lock:
            stats, self._stats = self._stats, {}
            return stats

    def _writer_main(self) -> None:
        pending: list[PendingObservation] = []
        while not self._stop.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                self._write_batch(pending)
                pending = []
                continue
            if isinstance(item, FlushRequest):
                self._write_batch(pending)
                pending = []
                item.completed.set()
            else:
                pending.append(item)
                if len(pending) >= DEFAULT_BATCH_SIZE:
                    self._write_batch(pending)
                    pending = []
        self._write_batch(pending)

    def _write_batch(self, pending: list[PendingObservation]) -> None:
        stats = self._take_stats()
        if not pending and not stats:
            return
        while True:
            try:
                self.store.insert_observations(pending, stats)
                return
            except sqlite3.Error as error:
                self.logger.error("measurement sqlite write failed: %s", error)
                if self._stop.wait(1):
                    self.logger.error("measurement stopped before pending data could be written")
                    return


class HttpSyncTransport:
    def __init__(self, endpoint: str, token: str, timeout: float = 10.0):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout

    def __call__(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.endpoint}/api/v1/pi/sync", data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode())
        if not isinstance(body, dict):
            raise ValueError("measurement server response must be a JSON object")
        return body


class MeasurementUploader:
    def __init__(self, store: SQLiteMeasurementStore, *, pi_id: str,
                 boot_id: str, transport: Callable[[dict], dict],
                 batch_size: int = DEFAULT_BATCH_SIZE,
                 logger: Optional[logging.Logger] = None):
        self.store = store
        self.pi_id = pi_id
        self.boot_id = boot_id
        self.transport = transport
        self.batch_size = batch_size
        self.logger = logger or logging.getLogger("ble-measurement")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run,
                                            name="ble-measurement-uploader", daemon=True)
            self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def upload_once(self) -> int:
        backlog = self.store.count_unuploaded()
        observations = self.store.fetch_unuploaded(self.batch_size)
        run_updates = self.store.fetch_unacked_run_updates()
        state = self.store.sync_state()
        payload = {
            "schemaVersion": SCHEMA_VERSION, "piId": self.pi_id,
            "bootId": observations[0]["bootId"] if observations else self.boot_id,
            "agentVersion": AGENT_VERSION, "sentAt": utc_now(),
            "appliedDesiredVersion": state["appliedDesiredVersion"],
            "activeRunId": state["activeRunId"],
            "stats": {"queueBacklog": backlog,
                      "missingRssi": state["stats"].get("missing_rssi", 0),
                      "queueDropped": state["stats"].get("queue_dropped", 0)},
            "runUpdates": run_updates, "observations": observations,
        }
        response = self.transport(payload)
        acked_events = response.get("ackedEventIds")
        acked_runs = response.get("ackedRunRevisions")
        if not isinstance(acked_events, list) or not all(isinstance(x, str) for x in acked_events):
            raise ValueError("measurement server response must contain ackedEventIds")
        if not isinstance(acked_runs, list) or not all(
                isinstance(x, dict) and isinstance(x.get("runId"), str)
                and isinstance(x.get("revision"), int) for x in acked_runs):
            raise ValueError("measurement server response must contain ackedRunRevisions")
        if set(acked_events) - {x["eventId"] for x in observations}:
            raise ValueError("measurement server acked unknown event ids")
        acked_run_keys = {(x["runId"], x["revision"]) for x in acked_runs}
        if acked_run_keys - {(x["runId"], x["revision"]) for x in run_updates}:
            raise ValueError("measurement server acked unknown run revisions")
        self.store.mark_uploaded(acked_events)
        self.store.mark_run_updates_acked(list(acked_run_keys))
        self.store.apply_desired_state(response.get("desiredState"))
        self.store.prune_uploaded_outside_run()
        return len(acked_events)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                backlog_before = self.store.count_unuploaded()
                self.upload_once()
                backlog_after = self.store.count_unuploaded()
                backoff = 1.0
                wait = 0.25 if backlog_after >= self.batch_size else (
                    2.0 if backlog_before or backlog_after else 5.0)
                self._stop.wait(wait)
            except Exception as error:
                self.logger.warning("measurement sync failed: %s", error)
                self._stop.wait(backoff * random.uniform(0.8, 1.2))
                backoff = min(backoff * 2, 60.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(
        "BLE RSSI experiment tool. Recording is continuous while measurement is "
        "enabled; runs only label experiment intervals."))
    parser.add_argument("--db", required=True, help="measurement SQLite database path")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="manage a local experiment label")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    start = run_commands.add_parser("start")
    start.add_argument("--expected", required=True, choices=["allow", "deny"])
    start.add_argument("--target", required=True)
    start.add_argument("--position", required=True)
    start.add_argument("--carrying", required=True)
    start.add_argument("--orientation", required=True)
    start.add_argument("--other-device", action="append", default=[])
    start.add_argument("--notes", default="")
    run_commands.add_parser("stop")
    subcommands.add_parser("status")
    export = subcommands.add_parser("export")
    export.add_argument("--run")
    export.add_argument("--output", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    store = SQLiteMeasurementStore(args.db)
    if args.command == "run" and args.run_command == "start":
        print(store.start_run(
            expected_result=args.expected, target_device_tag=args.target,
            position=args.position, carrying=args.carrying,
            body_orientation=args.orientation, other_devices=args.other_device,
            notes=args.notes))
        return 0
    if args.command == "run" and args.run_command == "stop":
        print(store.stop_active_run())
        return 0
    if args.command == "status":
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "export":
        print(store.export_csv(args.output, args.run))
        return 0
    raise AssertionError("unhandled command")


if __name__ == "__main__":
    raise SystemExit(main())

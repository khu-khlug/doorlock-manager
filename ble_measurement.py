"""Optional BLE RSSI experiment recorder and server sync client.

This module belongs to the long-lived ``experiment/ble-rssi-measurement`` branch.
Enabling it is never required for normal door-lock operation.

The Pi only records and uploads. It never acts on anything the server sends back,
so it cannot end up in a wrong commanded state -- and there is nothing to debug on
the Pi when an experiment goes wrong. Experiment intervals ("runs") are time
windows owned by the measurement server, which selects observations by timestamp.
That also means a mislabelled or mistimed interval is fixed on the server rather
than by walking the experiment again.
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

SCHEMA_VERSION = 2
AGENT_VERSION = "measurement-v3"
DEFAULT_BATCH_SIZE = 100
DEFAULT_QUEUE_SIZE = 10_000

# Uploaded observations are kept this long so a sync that is later found to be
# wrong can still be inspected on the Pi. The server keeps them permanently.
DEFAULT_UPLOADED_RETENTION_HOURS = 24


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


class LegacyDatabaseError(RuntimeError):
    """The database file predates the removal of Pi-side run labels."""


class SQLiteMeasurementStore:
    def __init__(self, path: str):
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
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
            self._reject_legacy_schema(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    event_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    pi_id TEXT NOT NULL,
                    boot_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    monotonic_ns INTEGER NOT NULL,
                    device_tag TEXT NOT NULL,
                    packet_kind TEXT NOT NULL,
                    service_uuid TEXT NOT NULL,
                    rssi INTEGER NOT NULL,
                    pi_phase TEXT NOT NULL,
                    uploaded_at TEXT
                );
                CREATE INDEX IF NOT EXISTS observations_pending_upload
                    ON observations (uploaded_at, observed_at);
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
                """
            )

    @staticmethod
    def _reject_legacy_schema(connection: sqlite3.Connection) -> None:
        """Fail loudly instead of quietly ignoring an old database.

        Older builds stored a ``run_id`` on every observation and kept ``runs``
        and ``run_updates`` tables. Reusing such a file would silently keep the
        stale columns around, so say what to do instead.
        """
        columns = connection.execute("PRAGMA table_info(observations)").fetchall()
        if any(row["name"] == "run_id" for row in columns):
            raise LegacyDatabaseError(
                "this measurement database was written by a build that labelled runs "
                "on the Pi; runs now live on the server. Delete the database file "
                "and start a new recording.")

    def insert_observations(self, observations: list[PendingObservation],
                            stats: Optional[dict[str, int]] = None) -> None:
        if not observations and not stats:
            return
        written = 0
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
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO observations (
                           event_id, schema_version, pi_id, boot_id, sequence,
                           observed_at, monotonic_ns, device_tag, packet_kind,
                           service_uuid, rssi, pi_phase
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (observation.event_id, observation.schema_version,
                     observation.pi_id, observation.boot_id, observation.sequence,
                     observation.observed_at, observation.monotonic_ns,
                     observation.device_tag, observation.packet_kind,
                     observation.service_uuid, observation.rssi, observation.pi_phase))
                written += cursor.rowcount
            increments = dict(stats or {})
            increments["written"] = increments.get("written", 0) + written
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

    def stats(self) -> dict[str, int]:
        with self._connection() as connection:
            return {row["name"]: row["value"]
                    for row in connection.execute("SELECT name, value FROM measurement_stats")}

    def status(self) -> dict:
        with self._connection() as connection:
            recent = [dict(row) for row in connection.execute(
                "SELECT * FROM recent_devices ORDER BY observed_at DESC LIMIT 20")]
            stored = connection.execute(
                "SELECT COUNT(*) AS n FROM observations").fetchone()["n"]
            pending = connection.execute(
                "SELECT COUNT(*) AS n FROM observations WHERE uploaded_at IS NULL"
            ).fetchone()["n"]
        return {"stored": stored, "pendingUpload": pending,
                "stats": self.stats(), "recentObservations": recent}

    def export_csv(self, output_path: str) -> int:
        fieldnames = ["event_id", "schema_version", "pi_id", "boot_id", "sequence",
                      "observed_at", "monotonic_ns", "device_tag", "packet_kind",
                      "service_uuid", "rssi", "pi_phase", "uploaded_at"]
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(fieldnames)} FROM observations "
                "ORDER BY observed_at, monotonic_ns").fetchall()
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
        return len(rows)

    def fetch_unuploaded(self, limit: int = DEFAULT_BATCH_SIZE) -> list[dict]:
        """One batch never mixes boots.

        The request carries a single ``bootId`` and the server quarantines any
        observation that disagrees with it, so a reboot with a backlog would lose
        everything recorded after the restart.
        """
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT event_id, schema_version, pi_id, boot_id, sequence,
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
                 "piId": row["pi_id"], "bootId": row["boot_id"],
                 "sequence": row["sequence"], "observedAt": row["observed_at"],
                 "monotonicNs": row["monotonic_ns"], "deviceTag": row["device_tag"],
                 "packetKind": row["packet_kind"], "serviceUuid": row["service_uuid"],
                 "rssi": row["rssi"], "piPhase": row["pi_phase"]} for row in rows]

    def count_unuploaded(self) -> int:
        with self._connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) AS n FROM observations WHERE uploaded_at IS NULL"
            ).fetchone()["n"]

    def mark_uploaded(self, event_ids: list[str], uploaded_at: Optional[str] = None) -> None:
        if not event_ids:
            return
        uploaded_at = uploaded_at or utc_now()
        with self._connection() as connection:
            connection.executemany(
                "UPDATE observations SET uploaded_at = ? WHERE event_id = ?",
                [(uploaded_at, event_id) for event_id in event_ids])

    def prune_uploaded(self,
                       retention_hours: int = DEFAULT_UPLOADED_RETENTION_HOURS) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM observations WHERE uploaded_at IS NOT NULL AND observed_at < ?",
                (cutoff,))
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
        stats = self.store.stats()
        payload = {
            "schemaVersion": SCHEMA_VERSION, "piId": self.pi_id,
            "bootId": observations[0]["bootId"] if observations else self.boot_id,
            "agentVersion": AGENT_VERSION,
            # The server subtracts this from its own clock to correct every
            # observed_at in this batch, so it must be sent on every sync.
            "sentAt": utc_now(),
            "stats": {"queueBacklog": backlog,
                      "missingRssi": stats.get("missing_rssi", 0),
                      "queueDropped": stats.get("queue_dropped", 0)},
            "observations": observations,
        }
        response = self.transport(payload)
        acked = response.get("ackedEventIds")
        if not isinstance(acked, list) or not all(isinstance(x, str) for x in acked):
            raise ValueError("measurement server response must contain ackedEventIds")
        if set(acked) - {x["eventId"] for x in observations}:
            raise ValueError("measurement server acked unknown event ids")
        self.store.mark_uploaded(acked)
        self.store.prune_uploaded()
        return len(acked)

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
        "enabled. Experiment intervals live on the measurement server, so there is "
        "nothing to start or stop here."))
    parser.add_argument("--db", required=True, help="measurement SQLite database path")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("status")
    export = subcommands.add_parser("export")
    export.add_argument("--output", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    store = SQLiteMeasurementStore(args.db)
    if args.command == "status":
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "export":
        print(store.export_csv(args.output))
        return 0
    raise AssertionError("unhandled command")


if __name__ == "__main__":
    raise SystemExit(main())

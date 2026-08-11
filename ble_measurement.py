import argparse
import csv
import hashlib
import hmac
import json
import logging
import queue
import sqlite3
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


SCHEMA_VERSION = 1
DEFAULT_BATCH_SIZE = 100
DEFAULT_QUEUE_SIZE = 10_000


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
                    ON runs ((1))
                    WHERE ended_at IS NULL;

                CREATE TABLE IF NOT EXISTS observations (
                    event_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
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

                CREATE INDEX IF NOT EXISTS observations_by_run_and_time
                    ON observations (run_id, monotonic_ns);

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

    def start_run(
        self,
        *,
        expected_result: str,
        target_device_tag: str,
        position: str,
        carrying: str,
        body_orientation: str,
        other_devices: list[str],
        notes: str,
        started_at: Optional[str] = None,
    ) -> str:
        if expected_result not in {"allow", "deny"}:
            raise ValueError("expected_result must be 'allow' or 'deny'")
        run_id = str(uuid.uuid4())
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO runs (
                        run_id, started_at, expected_result, target_device_tag,
                        position, carrying, body_orientation, other_devices, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        started_at or utc_now(),
                        expected_result,
                        target_device_tag,
                        position,
                        carrying,
                        body_orientation,
                        json.dumps(other_devices, ensure_ascii=False),
                        notes,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("another measurement run is already active") from error
        return run_id

    def stop_active_run(self, ended_at: Optional[str] = None) -> str:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT run_id FROM runs WHERE ended_at IS NULL"
            ).fetchone()
            if row is None:
                raise ValueError("no active measurement run")
            connection.execute(
                "UPDATE runs SET ended_at = ? WHERE run_id = ?",
                (ended_at or utc_now(), row["run_id"]),
            )
            return row["run_id"]

    def status(self) -> dict:
        with self._connection() as connection:
            active = connection.execute(
                "SELECT * FROM runs WHERE ended_at IS NULL"
            ).fetchone()
            stats = {
                row["name"]: row["value"]
                for row in connection.execute(
                    "SELECT name, value FROM measurement_stats ORDER BY name"
                )
            }
            recent_devices = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT device_tag, rssi, observed_at, packet_kind, service_uuid
                    FROM recent_devices
                    ORDER BY observed_at DESC
                    LIMIT 20
                    """
                )
            ]
            active_run = dict(active) if active is not None else None
            if active_run is not None:
                active_run["other_devices"] = json.loads(active_run["other_devices"])
            return {
                "activeRun": active_run,
                "stats": stats,
                "recentObservations": recent_devices,
            }

    def insert_observations(
        self,
        observations: list[PendingObservation],
        stats: Optional[dict[str, int]] = None,
    ) -> None:
        if not observations and not stats:
            return
        outside_run = 0
        written = 0
        with self._connection() as connection:
            for observation in observations:
                # Keep only the latest pseudonymous sighting outside a run. This lets
                # the operator identify a test phone without retaining raw samples.
                connection.execute(
                    """
                    INSERT INTO recent_devices (
                        device_tag, rssi, observed_at, packet_kind, service_uuid
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(device_tag) DO UPDATE SET
                        rssi = excluded.rssi,
                        observed_at = excluded.observed_at,
                        packet_kind = excluded.packet_kind,
                        service_uuid = excluded.service_uuid
                    WHERE excluded.observed_at >= recent_devices.observed_at
                    """,
                    (
                        observation.device_tag,
                        observation.rssi,
                        observation.observed_at,
                        observation.packet_kind,
                        observation.service_uuid,
                    ),
                )
                run = connection.execute(
                    """
                    SELECT run_id
                    FROM runs
                    WHERE started_at <= ?
                      AND (ended_at IS NULL OR ended_at >= ?)
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    (observation.observed_at, observation.observed_at),
                ).fetchone()
                if run is None:
                    outside_run += 1
                    continue
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO observations (
                        event_id, schema_version, run_id, pi_id, boot_id, sequence,
                        observed_at, monotonic_ns, device_tag, packet_kind,
                        service_uuid, rssi, pi_phase
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.event_id,
                        observation.schema_version,
                        run["run_id"],
                        observation.pi_id,
                        observation.boot_id,
                        observation.sequence,
                        observation.observed_at,
                        observation.monotonic_ns,
                        observation.device_tag,
                        observation.packet_kind,
                        observation.service_uuid,
                        observation.rssi,
                        observation.pi_phase,
                    ),
                )
                written += cursor.rowcount
            increments = dict(stats or {})
            increments["outside_run"] = increments.get("outside_run", 0) + outside_run
            increments["written"] = increments.get("written", 0) + written
            self._increment_stats(connection, increments)

    @staticmethod
    def _increment_stats(connection: sqlite3.Connection, increments: dict[str, int]) -> None:
        for name, value in increments.items():
            if value == 0:
                continue
            connection.execute(
                """
                INSERT INTO measurement_stats (name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = value + excluded.value
                """,
                (name, value),
            )

    def export_csv(self, output_path: str, run_id: Optional[str] = None) -> int:
        query = """
            SELECT
                event_id, schema_version, run_id, pi_id, boot_id, sequence,
                observed_at, monotonic_ns, device_tag, packet_kind,
                service_uuid, rssi, pi_phase, uploaded_at
            FROM observations
        """
        parameters: tuple = ()
        if run_id is not None:
            query += " WHERE run_id = ?"
            parameters = (run_id,)
        query += " ORDER BY monotonic_ns"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        fieldnames = [
            "event_id",
            "schema_version",
            "run_id",
            "pi_id",
            "boot_id",
            "sequence",
            "observed_at",
            "monotonic_ns",
            "device_tag",
            "packet_kind",
            "service_uuid",
            "rssi",
            "pi_phase",
            "uploaded_at",
        ]
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
                """
                SELECT
                    event_id, schema_version, run_id, pi_id, boot_id, sequence,
                    observed_at, monotonic_ns, device_tag, packet_kind,
                    service_uuid, rssi, pi_phase
                FROM observations
                WHERE uploaded_at IS NULL
                ORDER BY observed_at
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "eventId": row["event_id"],
                "schemaVersion": row["schema_version"],
                "runId": row["run_id"],
                "piId": row["pi_id"],
                "bootId": row["boot_id"],
                "sequence": row["sequence"],
                "observedAt": row["observed_at"],
                "monotonicNs": row["monotonic_ns"],
                "deviceTag": row["device_tag"],
                "packetKind": row["packet_kind"],
                "serviceUuid": row["service_uuid"],
                "rssi": row["rssi"],
                "piPhase": row["pi_phase"],
            }
            for row in rows
        ]

    def fetch_runs(self, run_ids: list[str]) -> list[dict]:
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    run_id, started_at, ended_at, expected_result,
                    target_device_tag, position, carrying, body_orientation,
                    other_devices, notes
                FROM runs
                WHERE run_id IN ({placeholders})
                """,
                tuple(run_ids),
            ).fetchall()
        return [
            {
                "runId": row["run_id"],
                "startedAt": row["started_at"],
                "endedAt": row["ended_at"],
                "expectedResult": row["expected_result"],
                "targetDeviceTag": row["target_device_tag"],
                "position": row["position"],
                "carrying": row["carrying"],
                "bodyOrientation": row["body_orientation"],
                "otherDevices": json.loads(row["other_devices"]),
                "notes": row["notes"],
            }
            for row in rows
        ]

    def mark_uploaded(self, event_ids: list[str], uploaded_at: Optional[str] = None) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE observations SET uploaded_at = ? WHERE event_id IN ({placeholders})",
                (uploaded_at or utc_now(), *event_ids),
            )


class BleMeasurementRecorder:
    def __init__(
        self,
        store: SQLiteMeasurementStore,
        *,
        pi_id: str,
        hash_key: bytes,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        logger: Optional[logging.Logger] = None,
    ):
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
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._writer_main,
            name="ble-measurement-writer",
            daemon=True,
        )
        self._thread.start()

    def observe(
        self,
        *,
        raw_identifier: bytes,
        packet_kind: str,
        service_uuid: str,
        rssi: Optional[int],
        pi_phase: str,
        observed_at: Optional[str] = None,
        monotonic_ns: Optional[int] = None,
    ) -> bool:
        if rssi is None:
            self._add_stat("missing_rssi")
            return False
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        device_tag = hmac.new(self.hash_key, raw_identifier, hashlib.sha256).hexdigest()[:16]
        observation = PendingObservation(
            event_id=f"{self.boot_id}:{sequence}",
            schema_version=SCHEMA_VERSION,
            pi_id=self.pi_id,
            boot_id=self.boot_id,
            sequence=sequence,
            observed_at=observed_at or utc_now(),
            monotonic_ns=monotonic_ns if monotonic_ns is not None else time.monotonic_ns(),
            device_tag=device_tag,
            packet_kind=packet_kind,
            service_uuid=service_uuid,
            rssi=int(rssi),
            pi_phase=pi_phase,
        )
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

    def _add_stat(self, name: str, value: int = 1) -> None:
        with self._stats_lock:
            self._stats[name] = self._stats.get(name, 0) + value

    def _take_stats(self) -> dict[str, int]:
        with self._stats_lock:
            stats = self._stats
            self._stats = {}
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
                continue
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


class HttpBatchTransport:
    def __init__(self, endpoint: str, token: str, timeout: float = 10.0):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout

    def __call__(self, payload: dict) -> list[str]:
        request = urllib.request.Request(
            f"{self.endpoint}/api/observations/batch",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        acked = body.get("ackedEventIds")
        if not isinstance(acked, list) or not all(isinstance(item, str) for item in acked):
            raise ValueError("measurement server response must contain ackedEventIds")
        return acked


class MeasurementUploader:
    def __init__(
        self,
        store: SQLiteMeasurementStore,
        *,
        pi_id: str,
        transport: Callable[[dict], list[str]],
        batch_size: int = DEFAULT_BATCH_SIZE,
        logger: Optional[logging.Logger] = None,
    ):
        self.store = store
        self.pi_id = pi_id
        self.transport = transport
        self.batch_size = batch_size
        self.logger = logger or logging.getLogger("ble-measurement")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="ble-measurement-uploader",
            daemon=True,
        )
        self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def upload_once(self) -> int:
        observations = self.store.fetch_unuploaded(self.batch_size)
        if not observations:
            return 0
        payload = {
            "piId": self.pi_id,
            "schemaVersion": SCHEMA_VERSION,
            "runs": self.store.fetch_runs(
                sorted({item["runId"] for item in observations})
            ),
            "observations": observations,
        }
        acked = self.transport(payload)
        sent_ids = {item["eventId"] for item in observations}
        unexpected = set(acked) - sent_ids
        if unexpected:
            raise ValueError("measurement server acked unknown event ids")
        self.store.mark_uploaded(acked)
        return len(acked)

    def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                uploaded = self.upload_once()
                backoff = 1
                self._stop.wait(0.25 if uploaded else 2)
            except Exception as error:
                self.logger.warning("measurement upload failed: %s", error)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BLE RSSI measurement tool")
    parser.add_argument("--db", required=True, help="measurement SQLite database path")
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run")
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
        run_id = store.start_run(
            expected_result=args.expected,
            target_device_tag=args.target,
            position=args.position,
            carrying=args.carrying,
            body_orientation=args.orientation,
            other_devices=args.other_device,
            notes=args.notes,
        )
        print(run_id)
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

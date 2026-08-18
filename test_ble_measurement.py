import csv
import hashlib
import hmac
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ble_measurement import (
    BleMeasurementRecorder,
    HttpSyncTransport,
    MeasurementUploader,
    SQLiteMeasurementStore,
)


class MeasurementTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.database = self.directory / "measurement.sqlite3"
        self.store = SQLiteMeasurementStore(str(self.database))

    def tearDown(self):
        self.temporary_directory.cleanup()

    def start_run(self) -> str:
        return self.store.start_run(
            expected_result="allow",
            target_device_tag="phone-a",
            position="outside-0.5m",
            carrying="hand",
            body_orientation="facing-pi",
            other_devices=["phone-b"],
            notes="test run",
        )

    def recorder(self) -> BleMeasurementRecorder:
        recorder = BleMeasurementRecorder(
            self.store,
            pi_id="pi-test",
            hash_key=b"test-secret",
        )
        recorder.start()
        return recorder

    @staticmethod
    def sync_response(payload, *, desired_version=0, desired_run=None):
        return {
            "ackedEventIds": [item["eventId"] for item in payload["observations"]],
            "ackedRunRevisions": [
                {"runId": item["runId"], "revision": item["revision"]}
                for item in payload["runUpdates"]
            ],
            "desiredState": {"version": desired_version, "run": desired_run},
        }

    def test_only_one_run_can_be_active(self):
        run_id = self.start_run()

        with self.assertRaisesRegex(ValueError, "already active"):
            self.start_run()

        self.assertEqual(run_id, self.store.stop_active_run())
        self.assertIsNone(self.store.status()["activeRun"])

    def test_records_pseudonymous_observation_in_active_run(self):
        run_id = self.start_run()
        recorder = self.recorder()
        try:
            self.assertTrue(
                recorder.observe(
                    raw_identifier=b"2026123456",
                    packet_kind="register",
                    service_uuid="register-uuid",
                    rssi=-63,
                    pi_phase="triggered",
                )
            )
            self.assertTrue(recorder.flush())
        finally:
            recorder.close()

        expected_tag = hmac.new(
            b"test-secret", b"2026123456", hashlib.sha256
        ).hexdigest()[:16]
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT run_id, device_tag, rssi, pi_phase FROM observations"
            ).fetchone()
        self.assertEqual((run_id, expected_tag, -63, "triggered"), row)
        self.assertNotEqual("2026123456", expected_tag)

    def test_outside_run_keeps_raw_observation_with_null_run(self):
        recorder = self.recorder()
        try:
            self.assertTrue(
                recorder.observe(
                    raw_identifier=b"2026123456",
                    packet_kind="register",
                    service_uuid="register-uuid",
                    rssi=-71,
                    pi_phase="idle",
                )
            )
            self.assertFalse(
                recorder.observe(
                    raw_identifier=b"2026123456",
                    packet_kind="register",
                    service_uuid="register-uuid",
                    rssi=None,
                    pi_phase="idle",
                )
            )
            self.assertTrue(recorder.flush())
        finally:
            recorder.close()

        status = self.store.status()
        self.assertEqual(1, status["stats"]["outside_run_written"])
        self.assertEqual(1, status["stats"]["missing_rssi"])
        self.assertEqual(-71, status["recentObservations"][0]["rssi"])
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT run_id, rssi FROM observations"
            ).fetchone()
        self.assertEqual((None, -71), row)

    def test_export_csv_preserves_raw_observation_fields(self):
        run_id = self.start_run()
        recorder = self.recorder()
        try:
            recorder.observe(
                raw_identifier=b"2026123456",
                packet_kind="heartbeat",
                service_uuid="register-uuid",
                rssi=-54,
                pi_phase="idle",
                monotonic_ns=1234,
            )
            self.assertTrue(recorder.flush())
        finally:
            recorder.close()

        output = self.directory / "export.csv"
        self.assertEqual(1, self.store.export_csv(str(output), run_id))
        with output.open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual("-54", rows[0]["rssi"])
        self.assertEqual("1234", rows[0]["monotonic_ns"])
        self.assertEqual("heartbeat", rows[0]["packet_kind"])

    def test_uploader_sends_run_contract_and_marks_only_acknowledged_events(self):
        run_id = self.start_run()
        recorder = self.recorder()
        try:
            recorder.observe(
                raw_identifier=b"2026123456",
                packet_kind="register",
                service_uuid="register-uuid",
                rssi=-60,
                pi_phase="triggered",
            )
            self.assertTrue(recorder.flush())
        finally:
            recorder.close()

        payloads = []

        def transport(payload):
            payloads.append(payload)
            return self.sync_response(payload)

        uploader = MeasurementUploader(
            self.store,
            pi_id="pi-test",
            boot_id="current-boot",
            transport=transport,
        )
        self.assertEqual(1, uploader.upload_once())
        self.assertEqual(run_id, payloads[0]["runUpdates"][0]["runId"])
        self.assertEqual(1, payloads[0]["runUpdates"][0]["revision"])
        self.assertEqual("pi-test", payloads[0]["observations"][0]["piId"])
        self.assertIn("bootId", payloads[0]["observations"][0])
        self.assertEqual([], self.store.fetch_unuploaded())

    def test_unknown_ack_does_not_mark_event_uploaded(self):
        self.start_run()
        recorder = self.recorder()
        try:
            recorder.observe(
                raw_identifier=b"2026123456",
                packet_kind="register",
                service_uuid="register-uuid",
                rssi=-60,
                pi_phase="triggered",
            )
            self.assertTrue(recorder.flush())
        finally:
            recorder.close()

        uploader = MeasurementUploader(
            self.store,
            pi_id="pi-test",
            boot_id="current-boot",
            transport=lambda payload: {
                "ackedEventIds": ["unknown-event"],
                "ackedRunRevisions": [],
                "desiredState": {"version": 0, "run": None},
            },
        )
        with self.assertRaisesRegex(ValueError, "unknown event"):
            uploader.upload_once()
        self.assertEqual(1, len(self.store.fetch_unuploaded()))

    def test_http_transport_uses_sync_endpoint_and_bearer_token(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received["path"] = self.path
                received["authorization"] = self.headers["Authorization"]
                received["body"] = json.loads(self.rfile.read(length))
                body = json.dumps({
                    "ackedEventIds": ["event-1"],
                    "ackedRunRevisions": [],
                    "desiredState": {"version": 0, "run": None},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            transport = HttpSyncTransport(
                f"http://127.0.0.1:{server.server_port}", "test-token"
            )
            response = transport({"observations": [{"eventId": "event-1"}]})
            self.assertEqual(["event-1"], response["ackedEventIds"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.assertEqual("/api/v1/pi/sync", received["path"])
        self.assertEqual("Bearer test-token", received["authorization"])
        self.assertEqual("event-1", received["body"]["observations"][0]["eventId"])

    def test_empty_sync_applies_versioned_start_and_stop(self):
        desired_run = {
            "runId": "server-run-1",
            "expectedResult": "deny",
            "targetDeviceTag": "phone-a",
            "position": "inside-room",
            "carrying": "pocket",
            "bodyOrientation": "back-to-pi",
            "otherDevices": ["phone-b"],
            "notes": "server controlled",
        }
        payloads = []
        responses = [
            {"version": 4, "run": desired_run},
            {"version": 5, "run": None},
        ]

        def transport(payload):
            payloads.append(payload)
            desired = responses.pop(0)
            return self.sync_response(
                payload, desired_version=desired["version"], desired_run=desired["run"]
            )

        uploader = MeasurementUploader(
            self.store,
            pi_id="pi-test",
            boot_id="current-boot",
            transport=transport,
        )
        self.assertEqual(0, uploader.upload_once())
        self.assertEqual("server-run-1", self.store.status()["activeRun"]["run_id"])
        self.assertEqual(4, self.store.status()["appliedDesiredVersion"])

        self.assertEqual(0, uploader.upload_once())
        self.assertIsNone(self.store.status()["activeRun"])
        self.assertEqual(5, self.store.status()["appliedDesiredVersion"])
        self.assertEqual([], payloads[0]["observations"])
        self.assertEqual("server-run-1", payloads[1]["activeRunId"])
        self.assertEqual(1, payloads[1]["runUpdates"][0]["revision"])

    def test_same_desired_version_is_not_applied_twice(self):
        desired = {
            "version": 1,
            "run": {
                "runId": "server-run-1", "expectedResult": "allow",
                "targetDeviceTag": "phone-a", "position": "outside-0.5m",
                "carrying": "hand", "bodyOrientation": "facing-pi",
                "otherDevices": [], "notes": "",
            },
        }
        self.assertTrue(self.store.apply_desired_state(desired))
        self.assertFalse(self.store.apply_desired_state(desired))
        self.assertEqual(1, len(self.store.fetch_unacked_run_updates()))

    def test_existing_database_migrates_run_id_to_nullable(self):
        legacy_database = self.directory / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_database)) as connection:
            connection.executescript(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                    expected_result TEXT NOT NULL, target_device_tag TEXT NOT NULL,
                    position TEXT NOT NULL, carrying TEXT NOT NULL,
                    body_orientation TEXT NOT NULL, other_devices TEXT NOT NULL,
                    notes TEXT NOT NULL
                );
                CREATE TABLE observations (
                    event_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(run_id), pi_id TEXT NOT NULL,
                    boot_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    observed_at TEXT NOT NULL, monotonic_ns INTEGER NOT NULL,
                    device_tag TEXT NOT NULL, packet_kind TEXT NOT NULL,
                    service_uuid TEXT NOT NULL, rssi INTEGER NOT NULL,
                    pi_phase TEXT NOT NULL, uploaded_at TEXT
                );
                """
            )
        migrated = SQLiteMeasurementStore(str(legacy_database))
        with closing(sqlite3.connect(legacy_database)) as connection:
            columns = {
                row[1]: row[3]
                for row in connection.execute("PRAGMA table_info(observations)")
            }
        self.assertEqual(0, columns["run_id"])
        self.assertEqual([], migrated.fetch_unuploaded())

    def test_sync_batch_contains_only_one_boot_id(self):
        with self.store._connection() as connection:
            for boot_id, sequence in (("old-boot", 1), ("new-boot", 1)):
                connection.execute(
                    """INSERT INTO observations (
                           event_id, schema_version, run_id, pi_id, boot_id, sequence,
                           observed_at, monotonic_ns, device_tag, packet_kind,
                           service_uuid, rssi, pi_phase
                       ) VALUES (?, 1, NULL, 'pi-test', ?, ?, ?, ?, 'tag',
                                 'register', 'uuid', -60, 'idle')""",
                    (f"{boot_id}:{sequence}", boot_id, sequence,
                     f"2026-08-18T00:00:0{sequence}Z", sequence),
                )
        first_batch = self.store.fetch_unuploaded(100)
        self.assertEqual({"old-boot"}, {item["bootId"] for item in first_batch})


if __name__ == "__main__":
    unittest.main()

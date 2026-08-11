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
    HttpBatchTransport,
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

    def test_outside_run_keeps_only_recent_tag_and_counts_drops(self):
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
        self.assertEqual(1, status["stats"]["outside_run"])
        self.assertEqual(1, status["stats"]["missing_rssi"])
        self.assertEqual(-71, status["recentObservations"][0]["rssi"])
        with closing(sqlite3.connect(self.database)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        self.assertEqual(0, count)

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
            return [payload["observations"][0]["eventId"]]

        uploader = MeasurementUploader(
            self.store,
            pi_id="pi-test",
            transport=transport,
        )
        self.assertEqual(1, uploader.upload_once())
        self.assertEqual(run_id, payloads[0]["runs"][0]["runId"])
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
            transport=lambda payload: ["unknown-event"],
        )
        with self.assertRaisesRegex(ValueError, "unknown event"):
            uploader.upload_once()
        self.assertEqual(1, len(self.store.fetch_unuploaded()))

    def test_http_transport_uses_batch_endpoint_and_bearer_token(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received["path"] = self.path
                received["authorization"] = self.headers["Authorization"]
                received["body"] = json.loads(self.rfile.read(length))
                body = json.dumps({"ackedEventIds": ["event-1"]}).encode("utf-8")
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
            transport = HttpBatchTransport(
                f"http://127.0.0.1:{server.server_port}", "test-token"
            )
            self.assertEqual(
                ["event-1"],
                transport({"observations": [{"eventId": "event-1"}]}),
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.assertEqual("/api/observations/batch", received["path"])
        self.assertEqual("Bearer test-token", received["authorization"])
        self.assertEqual("event-1", received["body"]["observations"][0]["eventId"])


if __name__ == "__main__":
    unittest.main()

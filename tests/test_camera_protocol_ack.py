"""Camera CoLa request/ACK correlation tests (real network ishlatilmaydi)."""
from __future__ import annotations

import socket
import unittest

from backend.camera.camera_client import (
    Lector652Client,
    _cola_expected_ack,
    _cola_recv,
)
from backend.logger import _SecretSanitizer


def _telegram(text: str) -> bytes:
    return b"\x02" + text.encode("latin1") + b"\x03"


class _FakeSocket:
    def __init__(self, *recv_items):
        self.recv_items = list(recv_items)
        self.sent = []
        self.timeouts = []
        self.closed = False

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self, _size):
        if not self.recv_items:
            raise socket.timeout()
        item = self.recv_items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


class CameraProtocolAckTests(unittest.TestCase):
    def test_expected_ack_depends_on_request_type_and_name(self):
        self.assertEqual(_cola_expected_ack("sMN mLIStart 0"), ("sAN", "mLIStart"))
        self.assertEqual(_cola_expected_ack("sRN DeviceIdent"), ("sRA", "DeviceIdent"))
        self.assertEqual(_cola_expected_ack("sEN LIIsActive 1"), ("sEA", "LIIsActive"))

    def test_recv_skips_measurement_and_stale_ack_until_matching_ack(self):
        sock = _FakeSocket(
            _telegram("TT=59ms OTL=10mm CC=0\n*NoRead*")
            + _telegram("sAN mLIStop")
            + _telegram("sAN mLIStart 0")
        )
        skipped = []
        response = _cola_recv(
            sock,
            timeout=0.2,
            expected_ack=("sAN", "mLIStart"),
            on_unmatched=skipped.append,
        )
        self.assertEqual(response, "sAN mLIStart 0")
        self.assertEqual(len(skipped), 2)
        self.assertIn("NoRead", skipped[0])
        self.assertEqual(skipped[1], "sAN mLIStop")

    def test_start_stream_only_activates_after_matching_ack(self):
        logs = []
        client = Lector652Client("127.0.0.1", password="CameraSecret", on_log=logs.append)
        client._ctrl = _FakeSocket(
            _telegram("sAN mLIStop") + _telegram("sAN mLIStart 0")
        )
        client._blob = _FakeSocket()

        client.start_stream()

        self.assertTrue(client._live_active)
        self.assertTrue(any("skip" in line and "mLIStop" in line for line in logs))
        self.assertEqual(client._ctrl.sent, [_telegram("sMN mLIStart 0")])

    def test_no_matching_ack_times_out_and_forces_clean_reconnect_state(self):
        ctrl = _FakeSocket(_telegram("sAN mLIStop"), socket.timeout())
        blob = _FakeSocket()
        client = Lector652Client("127.0.0.1", on_log=lambda _msg: None)
        client._ctrl = ctrl
        client._blob = blob

        with self.assertRaises(TimeoutError):
            client.start_stream()

        self.assertFalse(client._live_active)
        self.assertIsNone(client._ctrl)
        self.assertIsNone(client._blob)
        self.assertTrue(ctrl.closed)
        self.assertTrue(blob.closed)

    def test_check_password_is_redacted_at_source_and_logger(self):
        command = "sMN CheckPassword 3 CameraSecret"
        self.assertEqual(
            Lector652Client._safe_cola_command(command),
            "sMN CheckPassword 3 ***",
        )
        sanitized = _SecretSanitizer._sanitize("C->K  " + command)
        self.assertNotIn("CameraSecret", sanitized)
        self.assertIn("***", sanitized)

    def test_logger_masks_key_value_auth_and_cookie_secrets(self):
        text = (
            "password=RfidSecret Authorization: Bearer abc.def.ghi "
            "Cookie: ai_cam_session=SOMETOKENVALUE"
        )
        sanitized = _SecretSanitizer._sanitize(text)
        for secret in ("RfidSecret", "abc.def.ghi", "SOMETOKENVALUE"):
            self.assertNotIn(secret, sanitized)
        self.assertGreaterEqual(sanitized.count("***"), 3)


if __name__ == "__main__":
    unittest.main()

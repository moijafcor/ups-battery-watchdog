"""Unit tests for ups_battery_watchdog.py."""

import socket
import struct
import threading
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unittest.mock import patch

import ups_battery_watchdog as w


# ── Helpers ───────────────────────────────────────────────────────────────────

def _nis_response(records: list[str]) -> bytes:
    """Encode *records* as a NIS-framed byte stream ending with a zero-length terminator."""
    out = b""
    for r in records:
        encoded = r.encode()
        out += struct.pack(">H", len(encoded)) + encoded
    out += struct.pack(">H", 0)
    return out


def _run_fake_nis_server(records: list[str]) -> tuple[int, threading.Thread]:
    """Start a one-shot TCP server that serves *records* as a NIS response.

    Returns (port, thread). The thread exits after the first connection.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(1)

    payload = _nis_response(records)

    def serve():
        conn, _ = server.accept()
        conn.recv(1024)  # consume the request
        conn.sendall(payload)
        conn.close()
        server.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return port, t


def _write_log(lines: list[str]) -> Path:
    """Write *lines* to a temporary log file and return its path."""
    with NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        f.write("\n".join(lines) + "\n")
        return Path(f.name)


# ── Sample log lines ──────────────────────────────────────────────────────────

_ONLINE_LINE = (
    "2026-04-03T22:44:00 INFO UPS "
    "status=ONLINE load=31.0 Percent battery=100.0 Percent runtime=2.9 Minutes"
)
_ONBATT_LINE = (
    "2026-04-03T22:46:00 INFO UPS "
    "status=ONBATT load=34.0 Percent battery=97.0 Percent runtime=2.6 Minutes"
)
_SHUTDOWN_LINE = (
    "2026-04-03T22:46:00 WARNING UPS on battery — initiating shutdown: "
    "/usr/sbin/shutdown -h +1 UPS on battery: shutting down"
)
_COMMLOST_LINE = (
    "2026-04-03T22:50:01 ERROR Cannot reach apcupsd NIS "
    "at localhost:3551 — Connection refused"
)
_DRY_RUN_LINE = (
    "2026-04-03T22:46:00 WARNING DRY RUN — would execute: "
    "/usr/sbin/shutdown -h +1 UPS on battery: shutting down"
)


# ── is_on_battery ─────────────────────────────────────────────────────────────

class TestIsOnBattery(unittest.TestCase):
    """Tests for the is_on_battery status check."""

    def test_onbatt_returns_true(self):
        """STATUS=ONBATT should trigger a shutdown."""
        self.assertTrue(w.is_on_battery({"STATUS": "ONBATT"}))

    def test_online_returns_false(self):
        """STATUS=ONLINE is normal mains operation."""
        self.assertFalse(w.is_on_battery({"STATUS": "ONLINE"}))

    def test_commlost_returns_false(self):
        """COMMLOST means apcupsd lost contact with the UPS; not actionable here."""
        self.assertFalse(w.is_on_battery({"STATUS": "COMMLOST"}))

    def test_missing_key_returns_false(self):
        """An empty status dict should not trigger shutdown."""
        self.assertFalse(w.is_on_battery({}))

    def test_case_sensitive(self):
        """apcupsd always uppercases STATUS; lowercase must not match."""
        self.assertFalse(w.is_on_battery({"STATUS": "onbatt"}))


# ── shutdown ──────────────────────────────────────────────────────────────────

class TestShutdown(unittest.TestCase):
    """Tests for the shutdown command builder."""

    @patch("ups_battery_watchdog.subprocess.run")
    def test_dry_run_does_not_call_subprocess(self, mock_run):
        """--dry-run must never invoke the real shutdown binary."""
        w.shutdown(delay=1, dry_run=True)
        mock_run.assert_not_called()

    @patch("ups_battery_watchdog.subprocess.run")
    def test_calls_shutdown_with_correct_delay(self, mock_run):
        """shutdown -h +N must use the delay value passed in."""
        w.shutdown(delay=3, dry_run=False)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("+3", cmd)
        self.assertEqual(cmd[0], "/usr/sbin/shutdown")

    @patch("ups_battery_watchdog.subprocess.run")
    def test_zero_delay(self, mock_run):
        """delay=0 should pass +0 to shutdown for an immediate halt."""
        w.shutdown(delay=0, dry_run=False)
        cmd = mock_run.call_args[0][0]
        self.assertIn("+0", cmd)

    @patch("ups_battery_watchdog.subprocess.run")
    def test_shutdown_message_in_command(self, mock_run):
        """The broadcast message must be present in the shutdown command."""
        w.shutdown(delay=1, dry_run=False)
        cmd = mock_run.call_args[0][0]
        self.assertIn("UPS on battery: shutting down", cmd)


# ── read_nis_status ───────────────────────────────────────────────────────────

class TestReadNisStatus(unittest.TestCase):
    """Tests for the apcupsd NIS socket reader."""

    def test_parses_standard_response(self):
        """All key/value pairs in a standard ONLINE response should be parsed."""
        records = [
            "STATUS   : ONLINE",
            "LOADPCT  : 31.0 Percent",
            "BCHARGE  : 98.0 Percent",
            "TIMELEFT : 2.9 Minutes",
        ]
        port, t = _run_fake_nis_server(records)
        t.join(timeout=0)  # server is already waiting
        result = w.read_nis_status("127.0.0.1", port)
        t.join(timeout=2)

        self.assertEqual(result["STATUS"], "ONLINE")
        self.assertEqual(result["LOADPCT"], "31.0 Percent")
        self.assertEqual(result["BCHARGE"], "98.0 Percent")
        self.assertEqual(result["TIMELEFT"], "2.9 Minutes")

    def test_parses_onbatt_response(self):
        """STATUS=ONBATT should be parsed correctly."""
        records = ["STATUS   : ONBATT", "BCHARGE  : 72.0 Percent"]
        port, _ = _run_fake_nis_server(records)
        result = w.read_nis_status("127.0.0.1", port)
        self.assertEqual(result["STATUS"], "ONBATT")

    def test_empty_response_returns_empty_dict(self):
        """A server that sends only the terminator should yield an empty dict."""
        port, _ = _run_fake_nis_server([])
        result = w.read_nis_status("127.0.0.1", port)
        self.assertEqual(result, {})

    def test_lines_without_colon_are_ignored(self):
        """Records with no colon delimiter must not appear in the output."""
        records = ["HEADER LINE", "STATUS   : ONLINE"]
        port, _ = _run_fake_nis_server(records)
        result = w.read_nis_status("127.0.0.1", port)
        self.assertIn("STATUS", result)
        self.assertNotIn("HEADER LINE", result)

    def test_raises_on_connection_refused(self):
        """OSError must propagate when NIS is unreachable."""
        with self.assertRaises(OSError):
            w.read_nis_status("127.0.0.1", 1)  # port 1 is never listening

    def test_value_with_colon_is_preserved(self):
        """Only the first colon is the key/value delimiter; timestamps must be intact."""
        records = ["DATE     : 2026-04-03 22:40:11 -0400"]
        port, _ = _run_fake_nis_server(records)
        result = w.read_nis_status("127.0.0.1", port)
        self.assertEqual(result["DATE"], "2026-04-03 22:40:11 -0400")


# ── parse_outages ─────────────────────────────────────────────────────────────

class TestParseOutages(unittest.TestCase):
    """Tests for the log file outage parser."""

    def test_returns_empty_list_for_missing_file(self):
        """A non-existent log file should return an empty list, not raise."""
        result = w.parse_outages(Path("/nonexistent/path/ups.log"))
        self.assertEqual(result, [])

    def test_detects_on_battery_event(self):
        """An ONBATT status line should produce an on_battery event with metrics."""
        log = _write_log([_ONBATT_LINE])
        events = w.parse_outages(log)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "on_battery")
        self.assertEqual(events[0]["ts"], "2026-04-03T22:46:00")
        self.assertEqual(events[0]["load"], "34.0")
        self.assertEqual(events[0]["battery"], "97.0")
        self.assertEqual(events[0]["runtime"], "2.6")

    def test_detects_shutdown_event(self):
        """A WARNING shutdown line should produce a shutdown event."""
        log = _write_log([_SHUTDOWN_LINE])
        events = w.parse_outages(log)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "shutdown")
        self.assertEqual(events[0]["ts"], "2026-04-03T22:46:00")

    def test_detects_commlost_event(self):
        """An ERROR NIS-unreachable line should produce a commlost event."""
        log = _write_log([_COMMLOST_LINE])
        events = w.parse_outages(log)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "commlost")

    def test_online_status_lines_are_ignored(self):
        """Routine ONLINE poll lines must not appear as outage events."""
        log = _write_log([
            _ONLINE_LINE,
            "2026-04-03T22:45:00 INFO UPS is not on battery — no action taken.",
        ])
        events = w.parse_outages(log)
        self.assertEqual(events, [])

    def test_full_outage_sequence(self):
        """A complete outage cycle should yield three events in order."""
        log = _write_log([_ONLINE_LINE, _ONBATT_LINE, _SHUTDOWN_LINE, _COMMLOST_LINE])
        events = w.parse_outages(log)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["kind"], "on_battery")
        self.assertEqual(events[1]["kind"], "shutdown")
        self.assertEqual(events[2]["kind"], "commlost")

    def test_multiple_outages_over_time(self):
        """Multiple outage cycles across different days should all be captured."""
        onbatt_1 = (
            "2026-04-01T10:00:00 INFO UPS "
            "status=ONBATT load=30.0 Percent battery=95.0 Percent runtime=3.0 Minutes"
        )
        shutdown_1 = (
            "2026-04-01T10:00:00 WARNING UPS on battery — initiating shutdown: "
            "/usr/sbin/shutdown -h +1 UPS on battery: shutting down"
        )
        onbatt_2 = (
            "2026-04-02T15:30:00 INFO UPS "
            "status=ONBATT load=28.0 Percent battery=88.0 Percent runtime=2.5 Minutes"
        )
        shutdown_2 = (
            "2026-04-02T15:30:00 WARNING UPS on battery — initiating shutdown: "
            "/usr/sbin/shutdown -h +1 UPS on battery: shutting down"
        )
        log = _write_log([onbatt_1, shutdown_1, onbatt_2, shutdown_2])
        events = w.parse_outages(log)
        on_battery = [e for e in events if e["kind"] == "on_battery"]
        shutdowns = [e for e in events if e["kind"] == "shutdown"]
        self.assertEqual(len(on_battery), 2)
        self.assertEqual(len(shutdowns), 2)

    def test_dry_run_line_not_parsed_as_shutdown(self):
        """DRY RUN lines do not start with 'WARNING UPS on battery' and must be ignored."""
        log = _write_log([_DRY_RUN_LINE])
        events = w.parse_outages(log)
        self.assertEqual(events, [])


# ── setup_logging ─────────────────────────────────────────────────────────────

class TestSetupLogging(unittest.TestCase):
    """Tests for the logging setup helper."""

    def setUp(self):
        """Reset root logger handlers between tests, closing any open file handles."""
        import logging as _logging
        root = _logging.getLogger()
        for h in root.handlers[:]:
            h.close()
        root.handlers.clear()

    def test_creates_log_file(self):
        """setup_logging should create the log file if it does not exist."""
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "test.log"
            w.setup_logging(log_path)
            self.assertTrue(log_path.exists())

    def test_creates_parent_directory(self):
        """setup_logging should create missing parent directories."""
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "subdir" / "test.log"
            w.setup_logging(log_path)
            self.assertTrue(log_path.exists())

    def test_falls_back_gracefully_on_unwritable_path(self):
        """setup_logging must not raise when the log path is unwritable."""
        try:
            w.setup_logging(Path("/proc/nonexistent/ups.log"))
        except OSError as exc:
            self.fail(f"setup_logging raised unexpectedly: {exc}")


if __name__ == "__main__":
    unittest.main()

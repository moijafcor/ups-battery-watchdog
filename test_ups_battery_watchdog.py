"""Unit tests for ups_battery_watchdog.py."""

import logging
import socket
from subprocess import CalledProcessError
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
_CANCEL_LINE = (
    "2026-04-03T22:48:00 WARNING UPS power restored — cancelled pending shutdown"
)
_DEFERRED_LINE = (
    "2026-04-19T18:36:29 WARNING UPS on battery but runtime 16.4 min >= threshold 8 min"
    " — monitoring, shutdown deferred"
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
        self.assertTrue(w.is_on_battery({"STATUS": "ONBATT"}))

    def test_online_returns_false(self):
        self.assertFalse(w.is_on_battery({"STATUS": "ONLINE"}))

    def test_commlost_returns_false(self):
        self.assertFalse(w.is_on_battery({"STATUS": "COMMLOST"}))

    def test_missing_key_returns_false(self):
        self.assertFalse(w.is_on_battery({}))

    def test_case_sensitive(self):
        self.assertFalse(w.is_on_battery({"STATUS": "onbatt"}))


# ── timeleft_minutes ──────────────────────────────────────────────────────────

class TestTimeleftMinutes(unittest.TestCase):
    """Tests for TIMELEFT parsing."""

    def test_parses_standard_format(self):
        self.assertAlmostEqual(w.timeleft_minutes({"TIMELEFT": "16.4 Minutes"}), 16.4)

    def test_parses_integer_value(self):
        self.assertAlmostEqual(w.timeleft_minutes({"TIMELEFT": "5 Minutes"}), 5.0)

    def test_returns_none_when_missing(self):
        self.assertIsNone(w.timeleft_minutes({}))

    def test_returns_none_when_unparseable(self):
        self.assertIsNone(w.timeleft_minutes({"TIMELEFT": "unknown"}))

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(w.timeleft_minutes({"TIMELEFT": ""}))


# ── shutdown ──────────────────────────────────────────────────────────────────

class TestShutdown(unittest.TestCase):
    """Tests for the shutdown command builder."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.sentinel = Path(self.tmp.name) / "sentinel"

    def tearDown(self):
        self.tmp.cleanup()

    @patch("ups_battery_watchdog.subprocess.run")
    def test_dry_run_does_not_call_subprocess(self, mock_run):
        w.shutdown(delay=1, dry_run=True, sentinel=self.sentinel)
        mock_run.assert_not_called()

    @patch("ups_battery_watchdog.subprocess.run")
    def test_dry_run_writes_sentinel(self, mock_run):
        w.shutdown(delay=1, dry_run=True, sentinel=self.sentinel)
        self.assertTrue(self.sentinel.exists())

    @patch("ups_battery_watchdog.subprocess.run")
    def test_calls_shutdown_with_correct_delay(self, mock_run):
        w.shutdown(delay=3, dry_run=False, sentinel=self.sentinel)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("+3", cmd)
        self.assertEqual(cmd[0], "/usr/sbin/shutdown")

    @patch("ups_battery_watchdog.subprocess.run")
    def test_writes_sentinel_on_real_shutdown(self, mock_run):
        w.shutdown(delay=3, dry_run=False, sentinel=self.sentinel)
        self.assertTrue(self.sentinel.exists())

    @patch("ups_battery_watchdog.subprocess.run")
    def test_zero_delay(self, mock_run):
        w.shutdown(delay=0, dry_run=False, sentinel=self.sentinel)
        cmd = mock_run.call_args[0][0]
        self.assertIn("+0", cmd)

    @patch("ups_battery_watchdog.subprocess.run")
    def test_shutdown_message_in_command(self, mock_run):
        w.shutdown(delay=1, dry_run=False, sentinel=self.sentinel)
        cmd = mock_run.call_args[0][0]
        self.assertIn("UPS on battery: shutting down", cmd)


# ── cancel_shutdown ───────────────────────────────────────────────────────────

class TestCancelShutdown(unittest.TestCase):
    """Tests for the shutdown cancellation helper."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.sentinel = Path(self.tmp.name) / "sentinel"
        self.sentinel.touch()

    def tearDown(self):
        self.tmp.cleanup()

    @patch("ups_battery_watchdog.subprocess.run")
    def test_removes_sentinel(self, mock_run):
        w.cancel_shutdown(dry_run=False, sentinel=self.sentinel)
        self.assertFalse(self.sentinel.exists())

    @patch("ups_battery_watchdog.subprocess.run")
    def test_dry_run_removes_sentinel_without_subprocess(self, mock_run):
        w.cancel_shutdown(dry_run=True, sentinel=self.sentinel)
        mock_run.assert_not_called()
        self.assertFalse(self.sentinel.exists())

    @patch("ups_battery_watchdog.subprocess.run")
    def test_calls_shutdown_cancel(self, mock_run):
        w.cancel_shutdown(dry_run=False, sentinel=self.sentinel)
        cmd = mock_run.call_args[0][0]
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[0], "/usr/sbin/shutdown")

    @patch("ups_battery_watchdog.subprocess.run",
           side_effect=CalledProcessError(1, "shutdown"))
    def test_tolerates_no_pending_shutdown(self, mock_run):
        try:
            w.cancel_shutdown(dry_run=False, sentinel=self.sentinel)
        except Exception:
            self.fail("cancel_shutdown raised unexpectedly when no shutdown was pending")


# ── read_nis_status ───────────────────────────────────────────────────────────

class TestReadNisStatus(unittest.TestCase):
    """Tests for the apcupsd NIS socket reader."""

    def test_parses_standard_response(self):
        records = [
            "STATUS   : ONLINE",
            "LOADPCT  : 31.0 Percent",
            "BCHARGE  : 98.0 Percent",
            "TIMELEFT : 2.9 Minutes",
        ]
        port, t = _run_fake_nis_server(records)
        t.join(timeout=0)
        result = w.read_nis_status("127.0.0.1", port)
        t.join(timeout=2)

        self.assertEqual(result["STATUS"], "ONLINE")
        self.assertEqual(result["LOADPCT"], "31.0 Percent")
        self.assertEqual(result["BCHARGE"], "98.0 Percent")
        self.assertEqual(result["TIMELEFT"], "2.9 Minutes")

    def test_parses_onbatt_response(self):
        records = ["STATUS   : ONBATT", "BCHARGE  : 72.0 Percent"]
        port, _ = _run_fake_nis_server(records)
        result = w.read_nis_status("127.0.0.1", port)
        self.assertEqual(result["STATUS"], "ONBATT")

    def test_empty_response_returns_empty_dict(self):
        port, _ = _run_fake_nis_server([])
        result = w.read_nis_status("127.0.0.1", port)
        self.assertEqual(result, {})

    def test_lines_without_colon_are_ignored(self):
        records = ["HEADER LINE", "STATUS   : ONLINE"]
        port, _ = _run_fake_nis_server(records)
        result = w.read_nis_status("127.0.0.1", port)
        self.assertIn("STATUS", result)
        self.assertNotIn("HEADER LINE", result)

    def test_raises_on_connection_refused(self):
        with self.assertRaises(OSError):
            w.read_nis_status("127.0.0.1", 1)

    def test_value_with_colon_is_preserved(self):
        records = ["DATE     : 2026-04-03 22:40:11 -0400"]
        port, _ = _run_fake_nis_server(records)
        result = w.read_nis_status("127.0.0.1", port)
        self.assertEqual(result["DATE"], "2026-04-03 22:40:11 -0400")


# ── parse_outages ─────────────────────────────────────────────────────────────

class TestParseOutages(unittest.TestCase):
    """Tests for the log file outage parser."""

    def test_returns_empty_list_for_missing_file(self):
        result = w.parse_outages(Path("/nonexistent/path/ups.log"))
        self.assertEqual(result, [])

    def test_detects_on_battery_event(self):
        log = _write_log([_ONBATT_LINE])
        events = w.parse_outages(log)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "on_battery")
        self.assertEqual(events[0]["ts"], "2026-04-03T22:46:00")
        self.assertEqual(events[0]["load"], "34.0")
        self.assertEqual(events[0]["battery"], "97.0")
        self.assertEqual(events[0]["runtime"], "2.6")

    def test_detects_shutdown_event(self):
        log = _write_log([_SHUTDOWN_LINE])
        events = w.parse_outages(log)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "shutdown")
        self.assertEqual(events[0]["ts"], "2026-04-03T22:46:00")

    def test_detects_cancel_event(self):
        log = _write_log([_CANCEL_LINE])
        events = w.parse_outages(log)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "cancel")
        self.assertEqual(events[0]["ts"], "2026-04-03T22:48:00")

    def test_detects_deferred_event(self):
        log = _write_log([_DEFERRED_LINE])
        events = w.parse_outages(log)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "deferred")
        self.assertEqual(events[0]["runtime"], "16.4")

    def test_detects_commlost_event(self):
        log = _write_log([_COMMLOST_LINE])
        events = w.parse_outages(log)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "commlost")

    def test_online_status_lines_are_ignored(self):
        log = _write_log([
            _ONLINE_LINE,
            "2026-04-03T22:45:00 INFO UPS is not on battery — no action taken.",
        ])
        events = w.parse_outages(log)
        self.assertEqual(events, [])

    def test_full_outage_with_cancel(self):
        """Short outage: on-battery → deferred → power restored → cancelled."""
        log = _write_log([_ONLINE_LINE, _ONBATT_LINE, _DEFERRED_LINE, _CANCEL_LINE])
        events = w.parse_outages(log)
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["on_battery", "deferred", "cancel"])

    def test_full_outage_with_shutdown(self):
        """Long outage: on-battery → shutdown triggered."""
        log = _write_log([_ONLINE_LINE, _ONBATT_LINE, _SHUTDOWN_LINE, _COMMLOST_LINE])
        events = w.parse_outages(log)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["kind"], "on_battery")
        self.assertEqual(events[1]["kind"], "shutdown")
        self.assertEqual(events[2]["kind"], "commlost")

    def test_multiple_outages_over_time(self):
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
        log = _write_log([_DRY_RUN_LINE])
        events = w.parse_outages(log)
        self.assertEqual(events, [])


# ── setup_logging ─────────────────────────────────────────────────────────────

class TestSetupLogging(unittest.TestCase):
    """Tests for the logging setup helper."""

    def setUp(self):
        root = logging.getLogger()
        for h in root.handlers[:]:
            h.close()
        root.handlers.clear()

    def test_creates_log_file(self):
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "test.log"
            w.setup_logging(log_path)
            self.assertTrue(log_path.exists())

    def test_creates_parent_directory(self):
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "subdir" / "test.log"
            w.setup_logging(log_path)
            self.assertTrue(log_path.exists())

    def test_falls_back_gracefully_on_unwritable_path(self):
        try:
            w.setup_logging(Path("/proc/nonexistent/ups.log"))
        except OSError as exc:
            self.fail(f"setup_logging raised unexpectedly: {exc}")


if __name__ == "__main__":
    unittest.main()

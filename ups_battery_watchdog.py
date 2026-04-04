#!/usr/bin/env python3
"""
UPS battery watchdog — shuts down the host if APC UPS is running on battery.
Communicates directly with apcupsd NIS (port 3551) using the native protocol.

Usage:
    # Run watchdog (intended for systemd timer / cron):
    python3 ups_battery_watchdog.py [--host HOST] [--port PORT] [--delay MINUTES] [--log-file PATH] [--dry-run]

    # Read outage history from the log:
    python3 ups_battery_watchdog.py outages [--log-file PATH]

Design:
    Each invocation is stateless — it queries apcupsd NIS once, logs the result,
    optionally triggers a shutdown, and exits. Persistent state lives only in the
    log file, which the `outages` subcommand can parse to reconstruct outage history.

    The NIS protocol is a simple length-prefixed request/response:
        - Client sends: 2-byte big-endian length + command bytes (e.g. b"status")
        - Server replies: N records, each prefixed with a 2-byte length
        - A zero-length record signals end of stream
    This is the same wire format used by `apcaccess`.
"""

import argparse
import logging
import re
import socket
import struct
import subprocess
import sys
from pathlib import Path

APCUPSD_NIS_HOST = "localhost"
APCUPSD_NIS_PORT = 3551
SHUTDOWN_DELAY_MINUTES = 1
DEFAULT_LOG_FILE = Path("/var/log/ups-battery-watchdog.log")

LOG_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"

log = logging.getLogger(__name__)


def setup_logging(log_file: Path) -> None:
    """Configure logging to write to both stderr and *log_file*.

    If *log_file* cannot be opened (e.g. permission denied), falls back to
    stderr-only and logs a warning. The parent directory is created
    automatically if it does not exist.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    except OSError as exc:
        logging.basicConfig(level=logging.INFO)
        log.warning("Cannot open log file %s — %s", log_file, exc)
        return

    logging.basicConfig(
        level=logging.INFO,
        handlers=handlers,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt=LOG_TIMESTAMP_FORMAT,
    )


# ── NIS protocol ─────────────────────────────────────────────────────────────

def read_nis_status(host: str, port: int) -> dict[str, str]:
    """Query apcupsd NIS and return a parsed key/value status dict.

    Connects to *host*:*port*, sends the ``status`` command, and reads all
    length-prefixed response records until the server signals end-of-stream
    with a zero-length record.

    Each record is a line of the form ``KEY : VALUE``. The returned dict maps
    stripped keys to stripped values, e.g.::

        {
            "STATUS":  "ONLINE",
            "BCHARGE": "98.0 Percent",
            "LOADPCT": "31.0 Percent",
            ...
        }

    Raises:
        OSError: if the connection cannot be established or drops unexpectedly.
    """
    cmd = b"status"
    request = struct.pack(">H", len(cmd)) + cmd

    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(request)

        status = {}
        buf = b""

        while True:
            # Read 2-byte length prefix
            header = b""
            while len(header) < 2:
                chunk = sock.recv(2 - len(header))
                if not chunk:
                    return status
                header += chunk

            length = struct.unpack(">H", header)[0]
            if length == 0:
                break

            # Read exactly `length` bytes
            while len(buf) < length:
                chunk = sock.recv(length - len(buf))
                if not chunk:
                    return status
                buf += chunk

            line = buf[:length].decode("utf-8", errors="replace").strip()
            buf = buf[length:]

            if ":" in line:
                key, _, value = line.partition(":")
                status[key.strip()] = value.strip()

    return status


# ── Watchdog ──────────────────────────────────────────────────────────────────

def is_on_battery(status: dict[str, str]) -> bool:
    """Return True if the UPS status dict indicates battery operation.

    apcupsd sets ``STATUS`` to ``ONBATT`` when mains power is lost and the UPS
    is supplying power from its battery. All other values (``ONLINE``,
    ``COMMLOST``, etc.) are treated as non-actionable.
    """
    return status.get("STATUS", "") == "ONBATT"


def shutdown(delay: int, dry_run: bool) -> None:
    """Invoke the system shutdown command with a *delay*-minute grace period.

    Args:
        delay:   Minutes before the host halts. ``0`` shuts down immediately.
        dry_run: If True, log the command that would be run without executing it.

    The shutdown message broadcast to logged-in users is:
    ``"UPS on battery: shutting down"``.
    """
    cmd = ["/usr/sbin/shutdown", "-h", f"+{delay}", "UPS on battery: shutting down"]
    if dry_run:
        log.warning("DRY RUN — would execute: %s", " ".join(cmd))
        return
    log.warning("UPS on battery — initiating shutdown: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def cmd_watch(args: argparse.Namespace) -> None:
    """Poll apcupsd NIS once and shut down the host if the UPS is on battery.

    Intended to be called on a schedule (systemd timer or cron). Logs UPS
    status on every run; only triggers shutdown when ``STATUS == ONBATT``.

    Exits with code 2 if apcupsd NIS is unreachable.
    """
    setup_logging(args.log_file)

    try:
        status = read_nis_status(args.host, args.port)
    except OSError as exc:
        log.error("Cannot reach apcupsd NIS at %s:%d — %s", args.host, args.port, exc)
        sys.exit(2)

    ups_status = status.get("STATUS", "<unknown>")
    load = status.get("LOADPCT", "?")
    bcharge = status.get("BCHARGE", "?")
    timeleft = status.get("TIMELEFT", "?")

    log.info("UPS status=%s load=%s battery=%s runtime=%s", ups_status, load, bcharge, timeleft)

    if is_on_battery(status):
        shutdown(args.delay, args.dry_run)
    else:
        log.info("UPS is not on battery — no action taken.")


# ── Outage reader ─────────────────────────────────────────────────────────────

# Matches: 2026-04-03T10:00:01 WARNING UPS on battery — initiating shutdown...
_SHUTDOWN_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) WARNING UPS on battery"
)
# Matches: 2026-04-03T10:00:01 INFO UPS status=ONBATT load=33.0 Percent battery=98.0 Percent runtime=2.8 Minutes
_STATUS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) INFO UPS "
    r"status=(?P<status>\S+)\s+"
    r"load=(?P<load>\S+).*?"
    r"battery=(?P<battery>\S+).*?"
    r"runtime=(?P<runtime>\S+)"
)
# Matches: 2026-04-03T10:00:01 ERROR Cannot reach apcupsd NIS ...
_COMMLOST_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) ERROR Cannot reach apcupsd NIS"
)


def parse_outages(log_file: Path) -> list[dict]:
    """Parse *log_file* and return a list of outage events in chronological order.

    Each event is a dict with the following keys:

    - ``ts``      — ISO-8601 timestamp string (``YYYY-MM-DDTHH:MM:SS``)
    - ``kind``    — one of ``'on_battery'``, ``'shutdown'``, or ``'commlost'``
    - ``load``    — load percentage string, e.g. ``"34.0"`` (on_battery only)
    - ``battery`` — battery charge percentage string (on_battery only)
    - ``runtime`` — estimated runtime string in minutes (on_battery only)

    Returns an empty list if *log_file* does not exist.

    Three patterns are recognised:

    ``on_battery``
        An ``INFO`` line with ``status=ONBATT``, recording the load, charge,
        and estimated runtime at the moment power was lost.

    ``shutdown``
        A ``WARNING`` line confirming the shutdown command was dispatched.

    ``commlost``
        An ``ERROR`` line indicating apcupsd NIS was unreachable.
    """
    if not log_file.exists():
        return []

    events = []
    with log_file.open() as fh:
        for line in fh:
            line = line.rstrip()
            m = _SHUTDOWN_RE.match(line)
            if m:
                events.append({"ts": m.group("ts"), "kind": "shutdown"})
                continue
            m = _STATUS_RE.match(line)
            if m and m.group("status") == "ONBATT":
                events.append({
                    "ts": m.group("ts"),
                    "kind": "on_battery",
                    "load": m.group("load"),
                    "battery": m.group("battery"),
                    "runtime": m.group("runtime"),
                })
                continue
            m = _COMMLOST_RE.match(line)
            if m:
                events.append({"ts": m.group("ts"), "kind": "commlost"})

    return events


def cmd_outages(args: argparse.Namespace) -> None:
    """Print a formatted table of outage events parsed from the log file."""
    events = parse_outages(args.log_file)

    if not events:
        print(f"No outage events found in {args.log_file}")
        return

    print(f"Outage events from {args.log_file}:\n")
    print(f"{'Timestamp':<22} {'Event':<12} {'Load':<12} {'Battery':<12} {'Runtime'}")
    print("-" * 78)
    kind_labels = {"shutdown": "SHUTDOWN", "on_battery": "ON BATTERY", "commlost": "COMM LOST"}
    for e in events:
        kind_label = kind_labels.get(e["kind"], e["kind"])
        load    = e.get("load", "—")
        battery = e.get("battery", "—")
        runtime = e.get("runtime", "—")
        print(f"{e['ts']:<22} {kind_label:<12} {load:<12} {battery:<12} {runtime}")

    shutdowns  = sum(1 for e in events if e["kind"] == "shutdown")
    on_battery = sum(1 for e in events if e["kind"] == "on_battery")
    commlost   = sum(1 for e in events if e["kind"] == "commlost")
    print(f"\nTotal: {on_battery} on-battery event(s), {shutdowns} shutdown(s), {commlost} comm-lost event(s)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate command."""
    parser = argparse.ArgumentParser(
        description="APC UPS battery watchdog. Run without a subcommand to poll the UPS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Subcommands:\n  outages    Print outage history from the log file",
    )
    parser.add_argument("--host", default=APCUPSD_NIS_HOST, help="apcupsd NIS host (default: localhost)")
    parser.add_argument("--port", type=int, default=APCUPSD_NIS_PORT, help="apcupsd NIS port (default: 3551)")
    parser.add_argument("--delay", type=int, default=SHUTDOWN_DELAY_MINUTES,
                        help="Minutes to wait before shutdown (default: 1)")
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE,
                        help=f"Log file path (default: {DEFAULT_LOG_FILE})")
    parser.add_argument("--dry-run", action="store_true", help="Report action without executing shutdown")
    parser.add_argument("subcommand", nargs="?", choices=["outages"],
                        help="outages: print outage history from the log file")

    args = parser.parse_args()

    if args.subcommand == "outages":
        cmd_outages(args)
    else:
        cmd_watch(args)


if __name__ == "__main__":
    main()

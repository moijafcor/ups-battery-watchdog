#!/usr/bin/env python3
"""
UPS battery watchdog — shuts down the host if APC UPS is running on battery.
Communicates directly with apcupsd NIS (port 3551) using the native protocol.

Usage:
    # Run watchdog (intended for systemd timer / cron):
    python3 ups_battery_watchdog.py [--host HOST] [--port PORT] [--delay MINUTES] [--log-file PATH] [--dry-run]

    # Read outage history from the log:
    python3 ups_battery_watchdog.py outages [--log-file PATH]
"""

import argparse
import logging
import re
import socket
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path

APCUPSD_NIS_HOST = "localhost"
APCUPSD_NIS_PORT = 3551
SHUTDOWN_DELAY_MINUTES = 1
DEFAULT_LOG_FILE = Path("/var/log/ups-battery-watchdog.log")

LOG_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"

log = logging.getLogger(__name__)


def setup_logging(log_file: Path) -> None:
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt=LOG_TIMESTAMP_FORMAT,
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    except OSError as exc:
        # Fall back to stderr-only if the log file can't be opened
        logging.basicConfig(level=logging.INFO)
        log.warning("Cannot open log file %s — %s", log_file, exc)
        return

    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt=LOG_TIMESTAMP_FORMAT)


# ── NIS protocol ─────────────────────────────────────────────────────────────

def read_nis_status(host: str, port: int) -> dict[str, str]:
    """
    Query apcupsd NIS and return parsed key/value status dict.

    Protocol: send a 2-byte big-endian length prefix followed by the command
    string. Each response record is also length-prefixed; a zero-length record
    signals end of stream.
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
    return status.get("STATUS", "") == "ONBATT"


def shutdown(delay: int, dry_run: bool) -> None:
    cmd = ["/usr/sbin/shutdown", "-h", f"+{delay}", "UPS on battery: shutting down"]
    if dry_run:
        log.warning("DRY RUN — would execute: %s", " ".join(cmd))
        return
    log.warning("UPS on battery — initiating shutdown: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def cmd_watch(args: argparse.Namespace) -> None:
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

# Matches lines like: 2026-04-03T10:00:01 WARNING UPS on battery — initiating shutdown...
_SHUTDOWN_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) WARNING UPS on battery"
)
# Matches lines like: 2026-04-03T10:00:01 INFO UPS status=ONBATT load=33.0 Percent battery=98.0 Percent runtime=2.8 Minutes
_STATUS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) INFO UPS "
    r"status=(?P<status>\S+)\s+"
    r"load=(?P<load>\S+).*?"
    r"battery=(?P<battery>\S+).*?"
    r"runtime=(?P<runtime>\S+)"
)
_COMMLOST_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) ERROR Cannot reach apcupsd NIS"
)


def parse_outages(log_file: Path) -> list[dict]:
    """
    Parse the log file and return a list of outage events, each a dict with:
      ts        — timestamp of the event
      kind      — 'shutdown', 'on_battery', or 'commlost'
      load      — load % at time of event (shutdown/on_battery only)
      battery   — battery % at time of event
      runtime   — estimated runtime at time of event
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
    events = parse_outages(args.log_file)

    if not events:
        print(f"No outage events found in {args.log_file}")
        return

    print(f"Outage events from {args.log_file}:\n")
    print(f"{'Timestamp':<22} {'Event':<12} {'Load':<12} {'Battery':<12} {'Runtime'}")
    print("-" * 78)
    for e in events:
        kind_label = {"shutdown": "SHUTDOWN", "on_battery": "ON BATTERY", "commlost": "COMM LOST"}.get(e["kind"], e["kind"])
        load    = e.get("load", "—")
        battery = e.get("battery", "—")
        runtime = e.get("runtime", "—")
        print(f"{e['ts']:<22} {kind_label:<12} {load:<12} {battery:<12} {runtime}")

    shutdowns = sum(1 for e in events if e["kind"] == "shutdown")
    on_battery = sum(1 for e in events if e["kind"] == "on_battery")
    commlost = sum(1 for e in events if e["kind"] == "commlost")
    print(f"\nTotal: {on_battery} on-battery event(s), {shutdowns} shutdown(s), {commlost} comm-lost event(s)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
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

#!/usr/bin/env python3
"""
UPS battery watchdog — shuts down the host if APC UPS is running on battery.
Communicates directly with apcupsd NIS (port 3551) using the native protocol.

Usage:
    python3 ups_battery_watchdog.py [--host HOST] [--port PORT] [--delay MINUTES] [--dry-run]

Intended to run as a systemd service or cron job on the host with the UPS attached.
"""

import argparse
import logging
import socket
import struct
import subprocess
import sys

APCUPSD_NIS_HOST = "localhost"
APCUPSD_NIS_PORT = 3551
SHUTDOWN_DELAY_MINUTES = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


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


def is_on_battery(status: dict[str, str]) -> bool:
    return status.get("STATUS", "") == "ONBATT"


def shutdown(delay: int, dry_run: bool) -> None:
    cmd = ["/usr/sbin/shutdown", "-h", f"+{delay}", "UPS on battery: shutting down"]
    if dry_run:
        log.warning("DRY RUN — would execute: %s", " ".join(cmd))
        return
    log.warning("UPS on battery — initiating shutdown: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Shut down host when UPS is on battery.")
    parser.add_argument("--host", default=APCUPSD_NIS_HOST, help="apcupsd NIS host (default: localhost)")
    parser.add_argument("--port", type=int, default=APCUPSD_NIS_PORT, help="apcupsd NIS port (default: 3551)")
    parser.add_argument("--delay", type=int, default=SHUTDOWN_DELAY_MINUTES,
                        help="Minutes to wait before shutdown (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="Report action without executing shutdown")
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()

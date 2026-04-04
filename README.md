# apcupsd-battery-watchdog

Lightweight Python daemon that monitors an APC UPS via the apcupsd Network Information Server (NIS) and initiates a graceful host shutdown when the UPS switches to battery power.

No dependencies beyond the Python standard library. Works locally or remotely — useful for protecting machines that are powered by a UPS connected to a different host.

## Requirements

- Python 3.9+
- [apcupsd](https://www.apcupsd.com/) installed and running with NIS enabled (default port 3551)
- Linux host with `/usr/sbin/shutdown`

## How it works

The script queries the apcupsd NIS socket directly using the native length-prefixed protocol. If `STATUS` is `ONBATT`, it calls `shutdown -h +1` to give users a 1-minute grace period before the host halts.

It is designed to run on a schedule (systemd timer or cron). Each invocation is stateless: it queries, decides, and exits.

## Installation

```bash
sudo mkdir -p /opt/ups-battery-watchdog
sudo cp ups_battery_watchdog.py /opt/ups-battery-watchdog/

sudo cp ups-battery-watchdog.service /etc/systemd/system/
sudo cp ups-battery-watchdog.timer   /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now ups-battery-watchdog.timer
```

## Configuration

### apcupsd NIS

apcupsd must have NIS enabled in `/etc/apcupsd/apcupsd.conf`:

```
NETSERVER on
NISIP 0.0.0.0   # or 127.0.0.1 if watchdog runs on the same host
NISPORT 3551
```

Restart apcupsd after any config change:

```bash
sudo systemctl restart apcupsd
```

### Remote UPS (UPS attached to a different host)

If the UPS is connected to a different machine, point the watchdog at it:

```bash
python3 ups_battery_watchdog.py --host 192.168.1.10
```

Or override in the systemd service unit:

```ini
ExecStart=/usr/bin/python3 /opt/ups-battery-watchdog/ups_battery_watchdog.py --host 192.168.1.10
```

Make sure the remote host's NIS is listening on `0.0.0.0` (not `127.0.0.1`) and that port 3551 is reachable.

## CLI options

```
usage: ups_battery_watchdog.py [-h] [--host HOST] [--port PORT] [--delay MINUTES] [--dry-run]

  --host HOST      apcupsd NIS host (default: localhost)
  --port PORT      apcupsd NIS port (default: 3551)
  --delay MINUTES  minutes to wait before shutdown (default: 1)
  --dry-run        log what would happen without executing shutdown
```

## Testing

Use `--dry-run` to verify connectivity and status reporting without triggering a shutdown:

```bash
python3 ups_battery_watchdog.py --dry-run
```

Example output (UPS on mains):
```
2026-04-03T10:00:01 INFO UPS status=ONLINE load=31.0 Percent battery=98.0 Percent runtime=2.9 Minutes
2026-04-03T10:00:01 INFO UPS is not on battery — no action taken.
```

Example output (UPS on battery):
```
2026-04-03T10:00:01 INFO UPS status=ONBATT load=31.0 Percent battery=97.0 Percent runtime=2.7 Minutes
2026-04-03T10:00:01 WARNING DRY RUN — would execute: /usr/sbin/shutdown -h +1 UPS on battery: shutting down
```

## Checking timer status

```bash
systemctl list-timers ups-battery-watchdog.timer
journalctl -u ups-battery-watchdog.service -f
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | UPS online, no action taken |
| 0    | Shutdown initiated (or dry-run) |
| 2    | Could not reach apcupsd NIS |

## License

AGPLv3 — see [LICENSE](LICENSE).

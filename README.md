# apcupsd-battery-watchdog

Lightweight Python daemon that monitors an APC UPS via the apcupsd Network Information Server (NIS) and initiates a graceful host shutdown when the UPS switches to battery power.

No dependencies beyond the Python standard library. Works locally or remotely — useful for protecting machines that are powered by a UPS connected to a different host.

## Requirements

- Python 3.9+
- [apcupsd](https://www.apcupsd.com/) installed and running with NIS enabled (default port 3551)
- Linux host with `/usr/sbin/shutdown`

## Sample UPS status

Output from a live APC Back-UPS XS 1500M via `apcaccess status`:

```
APC      : 001,036,0865
DATE     : 2026-04-03 22:40:11 -0400
HOSTNAME : myhost
VERSION  : 3.14.14 (31 May 2016) debian
UPSNAME  : myups
CABLE    : USB Cable
DRIVER   : USB UPS Driver
UPSMODE  : Stand Alone
STARTTIME: 2026-04-03 18:06:20 -0400
MODEL    : Back-UPS XS 1500M
STATUS   : ONLINE
LINEV    : 124.0 Volts
LOADPCT  : 32.0 Percent
BCHARGE  : 100.0 Percent
TIMELEFT : 2.8 Minutes
MBATTCHG : 5 Percent
MINTIMEL : 3 Minutes
MAXTIME  : 0 Seconds
SENSE    : Medium
LOTRANS  : 88.0 Volts
HITRANS  : 139.0 Volts
ALARMDEL : No alarm
BATTV    : 27.3 Volts
LASTXFER : Low line voltage
NUMXFERS : 0
TONBATT  : 0 Seconds
CUMONBATT: 0 Seconds
XOFFBATT : N/A
SELFTEST : NO
STATFLAG : 0x05000008
SERIALNO : XXXXXXXXXXXX
BATTDATE : 2021-03-03
NOMINV   : 120 Volts
NOMBATTV : 24.0 Volts
NOMPOWER : 900 Watts
FIRMWARE : 947.d10 .D USB FW:d
END APC  : 2026-04-03 22:40:14 -0400
```

The watchdog reads the `STATUS` field. `ONLINE` means mains power; `ONBATT` triggers shutdown.

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
usage: ups_battery_watchdog.py [-h] [--host HOST] [--port PORT] [--delay MINUTES]
                                [--log-file PATH] [--dry-run] [{outages}]

  --host HOST      apcupsd NIS host (default: localhost)
  --port PORT      apcupsd NIS port (default: 3551)
  --delay MINUTES  minutes to wait before shutdown (default: 1)
  --log-file PATH  log file path (default: /var/log/ups-battery-watchdog.log)
  --dry-run        log what would happen without executing shutdown

Subcommands:
  outages          print outage history from the log file
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

## Viewing outage history

```bash
python3 ups_battery_watchdog.py outages
```

Example output:

```
Outage events from /var/log/ups-battery-watchdog.log:

Timestamp              Event        Load         Battery      Runtime
------------------------------------------------------------------------------
2026-04-03T22:46:00    ON BATTERY   34.0         97.0         2.6
2026-04-03T22:46:00    SHUTDOWN     —            —            —
2026-04-03T22:50:01    COMM LOST    —            —            —

Total: 1 on-battery event(s), 1 shutdown(s), 1 comm-lost event(s)
```

Three event types are tracked:

| Event | Meaning |
|---|---|
| `ON BATTERY` | UPS switched to battery; load/battery/runtime recorded |
| `SHUTDOWN` | Shutdown command was issued |
| `COMM LOST` | Could not reach apcupsd NIS |

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

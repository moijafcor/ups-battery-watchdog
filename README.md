# ups-battery-watchdog

Lightweight Python watchdog that monitors an APC UPS via the apcupsd Network
Information Server (NIS) and initiates a graceful host shutdown when the UPS
switches to battery power and estimated runtime falls below a configurable
threshold.

No dependencies beyond the Python standard library. Works locally or remotely —
useful for protecting machines that draw power from a UPS connected to a
different host.

## Requirements

- Python 3.9+
- [apcupsd](https://www.apcupsd.com/) installed and running with NIS enabled (default port 3551)
- Linux host with `/usr/sbin/shutdown`

## How it works

```
┌─────────────────────────────────────────────────────────────┐
│  systemd timer (every 1 min)                                │
│       │                                                     │
│       ▼                                                     │
│  ups_battery_watchdog.py                                    │
│       │                                                     │
│       ├─ connect to apcupsd NIS (localhost:3551)            │
│       │       sends: [len] "status"                         │
│       │       recvs: [len] "KEY : VALUE" × N + [0]          │
│       │                                                     │
│       ├─ STATUS == ONBATT?                                  │
│       │       no  ──► sentinel present? → cancel shutdown   │
│       │               log status, exit 0                    │
│       │                                                     │
│       │       yes ──► TIMELEFT >= --timeleft-min?           │
│       │                 yes ──► log DEFERRED, exit 0        │
│       │                 no  ──► write sentinel              │
│       │                         shutdown -h +N              │
│       │                                                     │
│       └─ append structured log line to log file             │
└─────────────────────────────────────────────────────────────┘
```

Each invocation queries NIS once, acts on the result, and exits. The only
persistent state between invocations is a **sentinel file**
(`/run/ups-battery-watchdog.shutdown`) written when a shutdown is scheduled.
Because `/run` is a tmpfs, the sentinel is always cleared on reboot.

### Shutdown decision

A shutdown is triggered only when **both** conditions are true:

1. `STATUS == ONBATT` — the UPS is running on battery
2. `TIMELEFT < --timeleft-min` — estimated runtime is below the threshold

If `TIMELEFT` is unavailable (apcupsd not yet calibrated, etc.), the watchdog
shuts down immediately on any `ONBATT` event — the safe default.

### Cancellation

When the watchdog fires and sees `STATUS == ONLINE` while the sentinel file is
present, it runs `shutdown -c` to cancel the pending countdown and removes the
sentinel. This handles short outages where mains power is restored before the
`--delay` window expires.

### NIS wire protocol

apcupsd NIS uses a simple length-prefixed framing:

1. Client sends a 2-byte big-endian length followed by the command (`status`).
2. Server replies with N records, each prefixed by a 2-byte length.
3. A zero-length record signals end of stream.

This is the same protocol used by `apcaccess`.

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
TIMELEFT : 19.4 Minutes
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
BATTDATE : 2026-04-08
NOMINV   : 120 Volts
NOMBATTV : 24.0 Volts
NOMPOWER : 900 Watts
FIRMWARE : 947.d10 .D USB FW:d
END APC  : 2026-04-03 22:40:14 -0400
```

The watchdog reads `STATUS` and `TIMELEFT`. `ONLINE` means mains power;
`ONBATT` with a low `TIMELEFT` triggers shutdown.

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
ExecStart=/usr/bin/python3 /opt/ups-battery-watchdog/ups_battery_watchdog.py \
  --host 192.168.1.10 \
  --timeleft-min 8 \
  --delay 5
```

Make sure the remote host's NIS is listening on `0.0.0.0` (not `127.0.0.1`)
and that port 3551 is reachable.

### Choosing --timeleft-min

`--timeleft-min` (default: `8` minutes) is the runtime floor below which a
shutdown is triggered. Short outages where the battery has ample headroom are
ignored and logged as `DEFERRED` — power often returns before the threshold
is crossed.

**Calibrate after a battery replacement:** `TIMELEFT` is estimated by the UPS
from load and battery voltage. After replacing a battery, let apcupsd condition
it for 12–24 hours before setting thresholds:

```bash
watch -n 30 'apcaccess status | grep -E "TIMELEFT|BCHARGE|BATTV"'
```

Once `TIMELEFT` stabilises, set `--timeleft-min` to roughly half the
steady-state value. For a battery reporting 19 minutes at normal load, 8
minutes leaves a comfortable margin:

```
--timeleft-min 8
```

Set `--timeleft-min 0` to disable the guard and shut down on any `ONBATT`
event regardless of runtime (backwards-compatible behaviour).

### Choosing --delay

`--delay` (default: `1` minute) is the grace period between the watchdog
deciding to shut down and the host halting. It gives in-flight workloads
(database flushes, NFS syncs, etc.) time to finish.

**General formula:**

```
safe delay = (timeleft-min) − shutdown overhead − safety margin
```

Where:
- **Shutdown overhead** — time the OS takes to stop services, unmount
  filesystems, and power off. Usually 15–60 seconds; check with
  `systemd-analyze`.
- **Safety margin** — buffer for load spikes. At least 30–60 seconds.

**Example:** With `--timeleft-min 8` and ~30 s shutdown overhead plus 60 s
margin, a 5-minute delay leaves ~2.5 minutes of margin before the battery
would reach the threshold:

```bash
python3 ups_battery_watchdog.py --timeleft-min 8 --delay 5
```

> **Note:** `TIMELEFT` can drop faster than expected when load spikes. Always
> leave a margin, and validate periodically under peak load.

### Log file permissions

The default log path is `/var/log/ups-battery-watchdog.log`. When running as
root (the default for a systemd service), this is created automatically.

To run as a non-root user, pre-create the file with appropriate permissions:

```bash
sudo touch /var/log/ups-battery-watchdog.log
sudo chown myuser /var/log/ups-battery-watchdog.log
```

Or redirect to a user-writable path via `--log-file`.

## CLI options

```
usage: ups_battery_watchdog.py [-h] [--host HOST] [--port PORT] [--delay MINUTES]
                                [--timeleft-min MINUTES] [--sentinel PATH]
                                [--log-file PATH] [--dry-run] [{outages}]

  --host HOST           apcupsd NIS host (default: localhost)
  --port PORT           apcupsd NIS port (default: 3551)
  --delay MINUTES       grace period before host halts (default: 1)
  --timeleft-min MINS   only shut down if TIMELEFT is below this value
                        (default: 8; set to 0 to disable)
  --sentinel PATH       sentinel file tracking a pending shutdown
                        (default: /run/ups-battery-watchdog.shutdown)
  --log-file PATH       log file path (default: /var/log/ups-battery-watchdog.log)
  --dry-run             log what would happen without executing shutdown or cancel

Subcommands:
  outages               print outage history from the log file
```

## Testing

Use `--dry-run` to verify connectivity and decision logic without triggering
a real shutdown or cancellation:

```bash
python3 ups_battery_watchdog.py --dry-run
```

Example output (UPS on mains, no sentinel):
```
2026-04-03T10:00:01 INFO UPS status=ONLINE load=31.0 Percent battery=100.0 Percent runtime=19.4 Minutes
2026-04-03T10:00:01 INFO UPS is not on battery — no action taken.
```

Example output (UPS on battery, runtime above threshold — shutdown deferred):
```
2026-04-03T10:00:01 INFO UPS status=ONBATT load=36.0 Percent battery=95.0 Percent runtime=16.4 Minutes
2026-04-03T10:00:01 WARNING UPS on battery but runtime 16.4 min >= threshold 8 min — monitoring, shutdown deferred
```

Example output (UPS on battery, runtime below threshold — shutdown scheduled):
```
2026-04-03T10:00:01 INFO UPS status=ONBATT load=38.0 Percent battery=60.0 Percent runtime=6.1 Minutes
2026-04-03T10:00:01 WARNING UPS on battery — runtime 6.1 min below threshold 8 min, triggering shutdown
2026-04-03T10:00:01 WARNING DRY RUN — would execute: /usr/sbin/shutdown -h +5 UPS on battery: shutting down
```

Example output (power restored, pending shutdown cancelled):
```
2026-04-03T10:05:00 INFO UPS status=ONLINE load=31.0 Percent battery=90.0 Percent runtime=17.1 Minutes
2026-04-03T10:05:00 WARNING UPS power restored — cancelled pending shutdown
```

### Unit tests

```bash
python3 -m pytest test_ups_battery_watchdog.py -v
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
2026-04-19T18:36:29    ON BATTERY   36.0         95.0         16.4
2026-04-19T18:36:29    DEFERRED     —            —            16.4
2026-04-19T18:37:30    CANCELLED    —            —            —
2026-04-20T02:14:05    ON BATTERY   38.0         61.0         6.1
2026-04-20T02:14:05    SHUTDOWN     —            —            —

Total: 2 on-battery event(s), 1 shutdown(s), 1 cancel(s), 1 deferred(s), 0 comm-lost event(s)
```

Five event types are tracked:

| Event | Meaning |
|---|---|
| `ON BATTERY` | UPS switched to battery; load/battery/runtime recorded |
| `DEFERRED` | TIMELEFT above threshold — shutdown skipped this poll |
| `SHUTDOWN` | Shutdown command was issued |
| `CANCELLED` | Power restored during delay window; pending shutdown cancelled |
| `COMM LOST` | Could not reach apcupsd NIS |

## Checking timer status

```bash
systemctl list-timers ups-battery-watchdog.timer
journalctl -u ups-battery-watchdog.service -f
```

## Troubleshooting

**`STATUS: COMMLOST` in apcaccess output**
apcupsd is running but cannot communicate with the UPS device. Check that the
USB cable is connected and that the kernel's HID driver hasn't claimed the
device. Restarting apcupsd usually resolves it:
`sudo systemctl restart apcupsd`.

**`Cannot reach apcupsd NIS`**
apcupsd is not running, or `NETSERVER` is off, or `NISIP` is `127.0.0.1` and
you're connecting remotely. Verify with: `apcaccess status`.

**`Cannot open log file`**
The process doesn't have write permission to the log file or its parent
directory. See [Log file permissions](#log-file-permissions) above.

**Shutdown fires on short outages**
Check that `--timeleft-min` is set appropriately for your battery's capacity.
Run `apcaccess status | grep TIMELEFT` under normal load to see the steady-state
value, then set the threshold to roughly half that.

**Shutdown not cancelled when power returns**
Verify the sentinel file path is on a writable tmpfs. The default
`/run/ups-battery-watchdog.shutdown` requires the service to run as root or a
user with write access to `/run`. Override with `--sentinel /tmp/ups-watchdog.shutdown`
if needed.

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | UPS online, no action taken |
| 0    | Shutdown initiated, deferred, or cancelled (or dry-run) |
| 2    | Could not reach apcupsd NIS |

## License

AGPLv3 — see [LICENSE](LICENSE).

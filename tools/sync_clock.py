#!/usr/bin/env python3
"""Sync runtime/clock/faketime.rc to match named's current view.

After the orchestrator dies, named's libfaketime keeps projecting
virtual time forward from its last-read anchor. Fresh processes
(rndc, dig) load libfaketime fresh and read the now-stale file,
getting an OLDER view. TSIG fails with "clock skew".

This tool pulls the FILE forward to match NAMED, so:

  - named keeps its accumulated DNSSEC state (no backward time-travel)
  - subsequent fresh processes read the same anchor named is using
  - TSIG works again

Modes:
  default          — preserve file's current speed
  --freeze         — set speed to x1 (slowest practical; libfaketime
                     doesn't support x0)
  --check          — print state + warn if drift > 5 min, don't write

`make time` invokes --check; `make sync` and `make freeze` invoke the
write modes.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RC_PATH = Path("runtime/clock/faketime.rc")
CONTAINER = "bind-auth"
FAKETIME_LIB = "/opt/faketime/lib/faketime/libfaketimeMT.so.1"
RNDC_KEY = "/etc/bind/rndc.key"

# Named log lines look like: 01-Mar-2026 12:01:49.216 <message>
NAMED_TS_RE = re.compile(
    r"^\s*(\d{1,2})-([A-Z][a-z]{2})-(\d{4})\s+(\d{2}):(\d{2}):(\d{2})"
)
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()

# rc file format: @YYYY-MM-DD HH:MM:SS xN
RC_RE = re.compile(r"@(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+x(\d+(?:\.\d+)?)")

TSIG_FUDGE_SEC = 300  # named rejects rndc when |skew| > 5 virtual minutes


def get_named_view() -> datetime | None:
    """Trigger named to emit a log line, parse the timestamp.

    Returns None if the container isn't reachable or no log line could
    be parsed (so the caller can degrade gracefully)."""
    try:
        # Trigger an rndc — TSIG may fail, doesn't matter; we want a log line
        subprocess.run(
            ["docker", "compose", "exec", "-T",
             "-e", f"LD_PRELOAD={FAKETIME_LIB}",
             CONTAINER, "rndc", "-k", RNDC_KEY, "status"],
            capture_output=True, timeout=10,
        )
        # Give docker logs a moment to flush
        time.sleep(0.2)
        result = subprocess.run(
            ["docker", "compose", "logs", "--tail", "1", CONTAINER],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None

    line = result.stdout.strip()
    # Strip docker compose prefix: "dnssec-lab-auth  | <log line>"
    if "|" in line:
        line = line.split("|", 1)[1].strip()
    m = NAMED_TS_RE.match(line)
    if not m:
        return None
    dd, mon, yyyy, hh, mm, ss = m.groups()
    if mon not in MONTHS:
        return None
    return datetime(int(yyyy), MONTHS.index(mon) + 1,
                    int(dd), int(hh), int(mm), int(ss))


def read_rc(rc_path: Path) -> tuple[datetime, float]:
    text = rc_path.read_text().strip()
    m = RC_RE.search(text)
    if not m:
        raise RuntimeError(f"unparseable rc file: {text!r}")
    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return ts, float(m.group(2))


def write_rc(rc_path: Path, ts: datetime, speed: float) -> None:
    """In-place overwrite — preserves the inode for the bind mount."""
    line = f"@{ts.strftime('%Y-%m-%d %H:%M:%S')} x{speed:g}\n".encode()
    fd = os.open(str(rc_path), os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
        os.ftruncate(fd, len(line))
    finally:
        os.close(fd)


def fmt_drift(seconds: float) -> str:
    sign = "+" if seconds >= 0 else "-"
    a = abs(seconds)
    if a < 120:
        return f"{sign}{a:.0f}s"
    if a < 7200:
        return f"{sign}{a/60:.1f} min"
    if a < 172800:
        return f"{sign}{a/3600:.1f} h"
    return f"{sign}{a/86400:.1f} d"


def report(file_ts: datetime, file_speed: float, named_ts: datetime | None,
           rc_path: Path) -> float | None:
    """Print state. Returns drift in seconds (named - file), or None."""
    print(f"file   : @{file_ts.strftime('%Y-%m-%d %H:%M:%S')} x{file_speed:g}")
    mtime = datetime.fromtimestamp(rc_path.stat().st_mtime)
    print(f"mtime  : {mtime.strftime('%Y-%m-%d %H:%M:%S')} (real)")
    if named_ts is None:
        print("named  : (container unreachable or no parseable log line)")
        return None
    drift = (named_ts - file_ts).total_seconds()
    print(f"named  : @{named_ts.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"drift  : {fmt_drift(drift)}  (named - file)")
    return drift


def main() -> int:
    p = argparse.ArgumentParser(
        description="Sync faketime.rc to named's current view")
    p.add_argument("--rc", type=Path, default=RC_PATH)
    p.add_argument("--freeze", action="store_true",
                   help="after sync, set speed to x1")
    p.add_argument("--check", action="store_true",
                   help="report drift only, don't write")
    args = p.parse_args()

    file_ts, file_speed = read_rc(args.rc)
    named_ts = get_named_view()
    drift = report(file_ts, file_speed, named_ts, args.rc)

    if args.check:
        if drift is not None and abs(drift) > TSIG_FUDGE_SEC:
            print(f"\nWARNING: drift exceeds {TSIG_FUDGE_SEC//60} virtual "
                  f"minutes — TSIG will fail. Run `make sync` to recover.",
                  file=sys.stderr)
            return 1
        return 0

    if named_ts is None:
        print("\ncannot sync: named's view is unavailable", file=sys.stderr)
        return 1

    new_speed = 1.0 if args.freeze else file_speed
    write_rc(args.rc, named_ts, new_speed)
    print(f"\nwrote  : @{named_ts.strftime('%Y-%m-%d %H:%M:%S')} x{new_speed:g}")
    print("waiting ~1.8s for named to re-anchor on next mtime check...")
    time.sleep(1.8)

    verify_ts = get_named_view()
    file_ts2, _ = read_rc(args.rc)
    if verify_ts is None:
        print("verify : (could not probe named)")
        return 0
    drift2 = (verify_ts - file_ts2).total_seconds()
    print(f"verify : file=@{file_ts2.strftime('%Y-%m-%d %H:%M:%S')} "
          f"named=@{verify_ts.strftime('%Y-%m-%d %H:%M:%S')} "
          f"drift={fmt_drift(drift2)}")
    if abs(drift2) > 60:
        print("\nWARNING: drift still > 1 minute after sync. Try `make sync` "
              "again, or `docker compose restart bind-auth` to fully reset.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
DNSSEC Time-Warp Orchestrator
=============================

Drives a containerized BIND running under libfaketime through a scripted
scenario expressed in virtual time.  The orchestrator itself runs in
real time.  It:

  1. Manages the shared faketime.rc file (start instant + speed multiplier).
  2. Translates scenario events ("at virtual 2026-01-15", "after 3 virtual
     days") into real-time sleeps.
  3. Invokes rndc / dig at the right real-world moment to simulate
     external events (parent DS publication, manual key rolls, etc).
  4. Polls BIND for observable state (rndc dnssec -status, DNSKEY RRset,
     .state file contents, log tail) and writes a unified timeline.

The clock model
---------------
libfaketime's config file format is:
    @YYYY-MM-DD hh:mm:ss xN
where N is the speed multiplier. "Virtual time now" is therefore:
    fake_start + (real_now - real_start_of_current_segment) * N
We track (fake_start, real_anchor, speed) as a TimeSegment.  Every time
we change speed OR jump, we write a NEW segment — because libfaketime
re-reads the file on every call (FAKETIME_NO_CACHE=1), the transition is
near-instant from BIND's perspective.
"""

from __future__ import annotations
import argparse
import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

import yaml


# ---------------------------------------------------------------------------
# Virtual clock
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class TimeSegment:
    """One continuous stretch of virtual time at a fixed speed."""
    fake_start: dt.datetime         # virtual time at real_anchor
    real_anchor: float              # time.time() when this segment began
    speed: float                    # virtual seconds per real second

    def virtual_now(self) -> dt.datetime:
        elapsed_real = time.time() - self.real_anchor
        return self.fake_start + dt.timedelta(seconds=elapsed_real * self.speed)

    def real_seconds_until(self, virtual_target: dt.datetime) -> float:
        """How many real seconds from NOW until virtual_target arrives?"""
        virtual_delta = (virtual_target - self.virtual_now()).total_seconds()
        if virtual_delta <= 0:
            return 0.0
        return virtual_delta / self.speed

    def to_faketime_line(self) -> str:
        # libfaketime parses "@YYYY-MM-DD hh:mm:ss xN"
        return f"@{self.fake_start.strftime('%Y-%m-%d %H:%M:%S')} x{self.speed:g}\n"


class VirtualClock:
    """Owns the shared faketime.rc file and the current segment.

    Background ticker
    -----------------
    libfaketime's `@TIMESTAMP xSPEED` format anchors virtual time at
    @TIMESTAMP at the moment the config is read. Long-lived processes
    (named) read it once and compute virtual_now correctly from there.
    But short-lived processes (rndc, dig, date) load libfaketime fresh
    on every invocation — and they all see @TIMESTAMP, never
    @TIMESTAMP + elapsed. This breaks TSIG (rndc signs at the static
    scenario-start time; named rejects with "clock skew").

    Fix: rewrite the rc file at `tick_interval` real seconds with the
    current virtual_now. Short-lived processes then always see a
    fresh anchor. named re-reads on its cache_duration and re-anchors
    — the math stays consistent because we write what named's
    virtual_now would have been at the moment of the write.
    """

    def __init__(self, rc_path: pathlib.Path, start: dt.datetime, speed: float,
                 tick_interval: float = 0.5):
        self.rc_path = rc_path
        self.rc_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.segment = TimeSegment(
            fake_start=start, real_anchor=time.time(), speed=speed
        )
        self._write()
        self._tick_interval = tick_interval
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick_loop, daemon=True)
        self._ticker.start()

    def _write(self) -> None:
        # In-place overwrite — DO NOT use atomic rename (breaks bind
        # mounts) or open("w") (truncates to 0 before writing, so a
        # concurrent reader sees an empty file → libfaketime parse
        # error). Instead: open for write without truncation, write
        # the new line from offset 0, THEN ftruncate to the new
        # length. The reader always sees either old valid content or
        # new valid content, never an empty file.
        line = self.segment.to_faketime_line().encode()
        fd = os.open(str(self.rc_path), os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
            os.ftruncate(fd, len(line))
        finally:
            os.close(fd)

    def _refresh_file(self) -> None:
        """Rewrite the rc file anchoring @TIMESTAMP at the CURRENT virtual
        time. This keeps short-lived rndc/dig invocations' TSIG signatures
        close to named's own virtual clock."""
        with self._lock:
            now_virtual = self.segment.virtual_now()
            self.segment = TimeSegment(
                fake_start=now_virtual,
                real_anchor=time.time(),
                speed=self.segment.speed,
            )
            self._write()

    def _tick_loop(self) -> None:
        while not self._stop.wait(self._tick_interval):
            try:
                self._refresh_file()
            except Exception:
                pass  # never let the ticker die

    def stop(self) -> None:
        self._stop.set()

    def set_speed(self, new_speed: float) -> None:
        with self._lock:
            # Anchor the current virtual instant, then change speed.
            now_virtual = self.segment.virtual_now()
            self.segment = TimeSegment(
                fake_start=now_virtual, real_anchor=time.time(), speed=new_speed
            )
            self._write()

    def jump_to(self, virtual_target: dt.datetime) -> None:
        """Discontinuously move virtual time forward. Use sparingly —
        this hides any bug that depends on timers firing in sequence."""
        with self._lock:
            self.segment = TimeSegment(
                fake_start=virtual_target,
                real_anchor=time.time(),
                speed=self.segment.speed,
            )
            self._write()

    def virtual_now(self) -> dt.datetime:
        with self._lock:
            return self.segment.virtual_now()

    def wait_until(self, virtual_target: dt.datetime,
                   on_tick: Optional[Callable[[dt.datetime], None]] = None,
                   tick_virtual_seconds: float = 300.0) -> None:
        """Sleep in real time until virtual_target arrives, invoking
        on_tick every `tick_virtual_seconds` of VIRTUAL time so the
        observer can sample state while we wait."""
        while True:
            remaining_real = self.segment.real_seconds_until(virtual_target)
            if remaining_real <= 0:
                return
            tick_real = tick_virtual_seconds / self.segment.speed
            sleep_for = min(remaining_real, tick_real)
            time.sleep(sleep_for)
            if on_tick is not None:
                on_tick(self.virtual_now())


_ZONE_FILE_TEMPLATE = """\
$TTL 3600
@   IN SOA  ns1.{zone}. hostmaster.{zone}. (
            {serial} ; serial
            3600       ; refresh
            900        ; retry
            1209600    ; expire
            3600 )     ; negative TTL
    IN NS   ns1.{zone}.
ns1 IN A    127.0.0.1
www IN A    192.0.2.1
"""


# ---------------------------------------------------------------------------
# Talking to BIND (at real speed — NOT under faketime)
# ---------------------------------------------------------------------------

class BindController:
    """Wraps rndc / dig / log-tail.

    rndc and dig are run INSIDE the BIND container so they see the same
    faked virtual time BIND does — otherwise rndc's TSIG signatures are
    rejected with "clock skew" (host is at real time ~2026-04, BIND is
    at virtual time ~2026-01-01 + elapsed).

    The container's entrypoint only sets LD_PRELOAD for named itself,
    not for exec'd shells (so debugging via `docker exec` is ergonomic).
    We therefore set LD_PRELOAD explicitly here when exec'ing rndc/dig.

    Note: under libfaketime, socket timeouts scale with the speed
    multiplier (rndc's gettimeofday returns virtual time, so "10 real
    seconds" becomes "10 virtual seconds = 10/speed real seconds").
    We compensate by passing a generous timeout multiplied by speed."""

    def __init__(self, container: str, rndc_key_in_container: str,
                 faketime_lib: str, log_dir: pathlib.Path,
                 dns_host: str = "127.0.0.1", dns_port: int = 53):
        self.container = container
        self.rndc_key = rndc_key_in_container
        self.faketime_lib = faketime_lib
        self.log_dir = log_dir
        self.dns_host = dns_host
        self.dns_port = dns_port

    def _exec(self, *cmd: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
        full = [
            "docker", "compose", "exec", "-T",
            "-e", f"LD_PRELOAD={self.faketime_lib}",
            self.container, *cmd,
        ]
        return subprocess.run(full, capture_output=True, text=True, timeout=timeout)

    def rndc(self, *args: str, timeout: float = 30.0, retries: int = 0) -> str:
        cmd = ("rndc", "-k", self.rndc_key, *args)
        last_err = None
        for attempt in range(1 + retries):
            try:
                result = self._exec(*cmd, timeout=timeout)
                if result.returncode != 0:
                    raise RuntimeError(
                        f"rndc failed ({result.returncode}): {result.stderr.strip()}"
                    )
                return result.stdout
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                last_err = e
                if attempt < retries:
                    time.sleep(1)
        raise last_err  # type: ignore[misc]

    def dig(self, qname: str, qtype: str, timeout: float = 15.0) -> str:
        result = self._exec(
            "dig", f"@{self.dns_host}", "-p", str(self.dns_port),
            "+dnssec", "+multiline", "+norecurse", qname, qtype,
            timeout=timeout,
        )
        return result.stdout

    def dnssec_status(self, zone: str) -> str:
        return self.rndc("dnssec", "-status", zone, retries=3)

    def checkds_published(self, zone: str, when: Optional[str] = None) -> str:
        args = ["dnssec", "-checkds"]
        if when:
            args += ["-when", when]
        args += ["published", zone]
        return self.rndc(*args, retries=3)

    def checkds_withdrawn(self, zone: str, when: Optional[str] = None) -> str:
        args = ["dnssec", "-checkds"]
        if when:
            args += ["-when", when]
        args += ["withdrawn", zone]
        return self.rndc(*args, retries=3)

    def rollover(self, zone: str, key_id: int) -> str:
        return self.rndc("dnssec", "-rollover", "-key", str(key_id), zone, retries=3)

    def ensure_zone(self, zone: str, policy: str, serial: str) -> str:
        """Ensure `zone` exists in BIND, creating it via rndc addzone if
        not. Returns one of: "exists", "created".

        Raises RuntimeError if the requested dnssec-policy isn't defined
        in named.conf (or for any other unhandled rndc failure).
        Idempotent — re-running on an existing zone returns "exists"."""
        # Write the zone file inside the container (avoids host-vs-container
        # uid/perm mismatch on the runtime/bind-data bind mount). _exec
        # doesn't take stdin, so this is a direct docker compose exec call.
        body = _ZONE_FILE_TEMPLATE.format(zone=zone, serial=serial)
        proc = subprocess.run(
            ["docker", "compose", "exec", "-T",
             self.container, "sh", "-c",
             f"cat > /var/lib/bind/{zone}.db"],
            input=body, capture_output=True, text=True, timeout=15.0,
        )
        if proc.returncode != 0:
            err = proc.stderr.lower()
            if "read-only file system" in err:
                # The zone file path is bind-mounted read-only from the
                # host — meaning this is a statically declared zone
                # (e.g., example.test). It's already known to BIND, so
                # there's nothing to do.
                return "exists"
            raise RuntimeError(
                f"failed to write zone file for {zone}: {proc.stderr.strip()}"
            )

        zone_config = (
            f'{{ type primary; file "/var/lib/bind/{zone}.db"; '
            f'dnssec-policy "{policy}"; inline-signing yes; }};'
        )
        try:
            self.rndc("addzone", zone, zone_config, retries=2)
            return "created"
        except RuntimeError as e:
            msg = str(e).lower()
            if "already exists" in msg:
                return "exists"
            if "configure_zone failed" in msg or "not found" in msg:
                # Most common cause once the zone file is on disk: the
                # named dnssec-policy doesn't exist. (BIND's error is
                # generic — "configure_zone failed: not found" — but
                # the file write above already succeeded, so the
                # remaining unknown thing is the policy name.)
                raise RuntimeError(
                    f"`rndc addzone {zone}` failed (most likely "
                    f"dnssec-policy {policy!r} is not defined in "
                    f"docker/bind/named.conf — add a "
                    f"`dnssec-policy {policy!r} {{ ... }}` block and "
                    f"`docker compose down && docker compose up -d`). "
                    f"Raw rndc error: {e}"
                ) from e
            raise

    def parse_dnssec_status(self, raw: str) -> list[dict[str, Any]]:
        """Turn rndc dnssec -status into structured key records.
        Output format looks like:
            key: 12345 (ECDSAP256SHA256), KSK
              published:      yes - since ...
              key signing:    yes - since ...
              ...
        """
        keys: list[dict[str, Any]] = []
        current: Optional[dict[str, Any]] = None
        for line in raw.splitlines():
            m = re.match(r"\s*key:\s+(\d+)\s+\(([^)]+)\),\s*(\S+)", line)
            if m:
                if current is not None:
                    keys.append(current)
                current = {
                    "id": int(m.group(1)),
                    "algorithm": m.group(2),
                    "role": m.group(3),
                    "states": {},
                }
                continue
            if current is None:
                continue
            m = re.match(r"\s+([a-z ]+?):\s+(yes|no)(?:\s+-\s+(.*))?", line)
            if m:
                current["states"][m.group(1).strip()] = {
                    "value": m.group(2),
                    "since": m.group(3),
                }
        if current is not None:
            keys.append(current)
        return keys

    def tail_dnssec_log(self, max_lines: int = 50) -> list[str]:
        path = self.log_dir / "dnssec.log"
        if not path.exists():
            return []
        with path.open() as f:
            lines = f.readlines()
        return [ln.rstrip() for ln in lines[-max_lines:]]


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Observation:
    virtual_time: dt.datetime
    real_time: dt.datetime
    kind: str
    data: Any


class TimelineLog:
    """Single append-only timeline of everything that happens, keyed by
    virtual time.  Written as JSONL for easy post-hoc analysis."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = self.path.open("a", buffering=1)  # line-buffered

    def record(self, obs: Observation) -> None:
        record = {
            "v": obs.virtual_time.isoformat(),
            "r": obs.real_time.isoformat(),
            "kind": obs.kind,
            "data": obs.data,
        }
        self.fp.write(json.dumps(record, default=str) + "\n")
        # Pretty live output for the watcher.
        short = obs.virtual_time.strftime("%Y-%m-%d %H:%M:%S")
        summary = obs.data if isinstance(obs.data, str) else obs.kind
        print(f"[virtual {short}] {obs.kind}: {summary}"[:140])


def _parse_duration(s: str) -> dt.timedelta:
    """Accepts '3d', '12h', '90m', '45s', '1d12h', 'P30D' (ISO)."""
    s = s.strip()
    if s.startswith("P"):
        # crude ISO8601 duration parse; covers what dnssec-policy uses
        m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s)
        if not m:
            raise ValueError(f"bad ISO duration: {s}")
        d, h, mi, se = (int(x) if x else 0 for x in m.groups())
        return dt.timedelta(days=d, hours=h, minutes=mi, seconds=se)
    matches = re.findall(r"(\d+)([dhms])", s)
    if not matches:
        raise ValueError(f"bad duration: {s}")
    total = dt.timedelta()
    for num, unit in matches:
        total += {
            "d": dt.timedelta(days=int(num)),
            "h": dt.timedelta(hours=int(num)),
            "m": dt.timedelta(minutes=int(num)),
            "s": dt.timedelta(seconds=int(num)),
        }[unit]
    return total


def _coerce_datetime(v: Any) -> dt.datetime:
    """Accept either an ISO-8601 string or a datetime (PyYAML auto-parses
    unquoted timestamps into datetime). Always return a naive datetime."""
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    if isinstance(v, str):
        return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    raise ValueError(f"cannot interpret {v!r} as datetime")


def _parse_virtual_target(spec: str | dict, clock: VirtualClock) -> dt.datetime:
    """Resolve a scenario step's time spec to an absolute virtual datetime."""
    if isinstance(spec, dict):
        if "at" in spec:
            return _coerce_datetime(spec["at"])
        if "after" in spec:
            return clock.virtual_now() + _parse_duration(spec["after"])
    raise ValueError(f"step needs 'at' or 'after': {spec}")


class ScenarioRunner:
    def __init__(self, scenario: dict, clock: VirtualClock,
                 bind: BindController, timeline: TimelineLog):
        self.scenario = scenario
        self.clock = clock
        self.bind = bind
        self.timeline = timeline
        self.zone = scenario["zone"]
        self.dnssec_policy = scenario.get("dnssec_policy", "lab-fast")

        obs_cfg = scenario.get("observe", {})
        self.obs_interval = _parse_duration(
            obs_cfg.get("every", "15m")
        ).total_seconds()
        self._last_obs_virtual: Optional[dt.datetime] = None
        self._last_status: Optional[list[dict[str, Any]]] = None

    def record(self, kind: str, data: Any) -> None:
        self.timeline.record(Observation(
            virtual_time=self.clock.virtual_now(),
            real_time=dt.datetime.now(),
            kind=kind,
            data=data,
        ))

    # --- observation sampling ------------------------------------------------

    def sample(self, virtual_now: dt.datetime) -> None:
        """Called periodically during waits.  Pulls current DNSSEC state
        and emits a diff event whenever it changes."""
        if (self._last_obs_virtual is not None
                and (virtual_now - self._last_obs_virtual).total_seconds()
                    < self.obs_interval):
            return
        self._last_obs_virtual = virtual_now
        try:
            raw = self.bind.rndc("dnssec", "-status", self.zone, retries=2)
        except Exception as e:
            self.record("sample_error", str(e))
            return
        keys = self.bind.parse_dnssec_status(raw)
        if keys != self._last_status:
            self.record("key_state_change", {
                "keys": keys,
                "raw": raw.strip(),
            })
            self._last_status = keys
        else:
            self.record("heartbeat", {"key_count": len(keys)})

    # --- step dispatch -------------------------------------------------------

    def run(self) -> None:
        self.record("scenario_start", {
            "zone": self.zone,
            "dnssec_policy": self.dnssec_policy,
            "start": self.clock.virtual_now().isoformat(),
            "speed": self.clock.segment.speed,
        })
        # Make sure the zone exists in BIND (auto-create if not).
        # Serial is YYYYMMDD01 derived from the scenario start so that
        # re-runs produce a stable file content.
        serial = self.clock.segment.fake_start.strftime("%Y%m%d") + "01"
        outcome = self.bind.ensure_zone(self.zone, self.dnssec_policy, serial)
        self.record(
            "zone_provisioned" if outcome == "created" else "zone_exists",
            {"zone": self.zone, "dnssec_policy": self.dnssec_policy},
        )
        for i, step in enumerate(self.scenario.get("steps", [])):
            self._run_step(i, step)
        self.record("scenario_end", "complete")

    def _run_step(self, i: int, step: dict) -> None:
        target = _parse_virtual_target(step, self.clock)
        desc = step.get("description", f"step {i}")
        self.record("step_wait", {
            "step": i, "description": desc,
            "target_virtual": target.isoformat(),
            "real_seconds": self.clock.segment.real_seconds_until(target),
        })
        self.clock.wait_until(target, on_tick=self.sample)

        if "speed" in step:
            self.clock.set_speed(float(step["speed"]))
            self.record("speed_change", {"new_speed": step["speed"]})

        action = step.get("do")
        if action:
            self._perform_action(action, step)

        for assertion in step.get("assert", []) if isinstance(step.get("assert"), list) else ([step["assert"]] if "assert" in step else []):
            self._check_assertion(assertion)

        if step.get("snapshot"):
            try:
                self.record("snapshot", {
                    "status": self.bind.dnssec_status(self.zone),
                    "dnskey": self.bind.dig(self.zone, "DNSKEY"),
                    "cds": self.bind.dig(self.zone, "CDS"),
                })
            except Exception as e:
                self.record("snapshot_error", str(e))

    def _perform_action(self, action: str, step: dict) -> None:
        if action == "ds_published":
            self.record("action", "telling BIND: parent DS is published")
            out = self.bind.checkds_published(self.zone)
            self.record("rndc_output", out.strip())
        elif action == "ds_withdrawn":
            self.record("action", "telling BIND: parent DS is withdrawn")
            out = self.bind.checkds_withdrawn(self.zone)
            self.record("rndc_output", out.strip())
        elif action == "rollover_ksk":
            keys = self.bind.parse_dnssec_status(
                self.bind.dnssec_status(self.zone))
            ksks = [k for k in keys if k["role"] == "KSK"]
            if not ksks:
                raise RuntimeError("no KSK found to roll")
            self.record("action", f"triggering KSK rollover (key {ksks[0]['id']})")
            out = self.bind.rollover(self.zone, ksks[0]["id"])
            self.record("rndc_output", out.strip())
        elif action == "rollover_zsk":
            keys = self.bind.parse_dnssec_status(
                self.bind.dnssec_status(self.zone))
            zsks = [k for k in keys if k["role"] == "ZSK"]
            if not zsks:
                raise RuntimeError("no ZSK found to roll")
            self.record("action", f"triggering ZSK rollover (key {zsks[0]['id']})")
            out = self.bind.rollover(self.zone, zsks[0]["id"])
            self.record("rndc_output", out.strip())
        elif action == "reload":
            self.record("rndc_output", self.bind.rndc("reload").strip())
        else:
            raise ValueError(f"unknown action: {action}")

    def _check_assertion(self, assertion: str) -> None:
        keys = self.bind.parse_dnssec_status(
            self.bind.dnssec_status(self.zone))
        ok = False
        detail: Any = keys
        if assertion == "zone_signed":
            dnskey = self.bind.dig(self.zone, "DNSKEY")
            ok = "RRSIG" in dnskey and "DNSKEY" in dnskey
            detail = "RRSIG present" if ok else "no RRSIG on DNSKEY"
        elif assertion == "ksk_active":
            ok = any(
                k["role"] == "KSK"
                and k["states"].get("key signing", {}).get("value") == "yes"
                for k in keys
            )
        elif assertion == "two_ksks_present":
            ok = sum(1 for k in keys if k["role"] == "KSK") >= 2
            detail = f"{sum(1 for k in keys if k['role'] == 'KSK')} KSKs present"
        elif assertion == "two_zsks_present":
            ok = sum(1 for k in keys if k["role"] == "ZSK") >= 2
            detail = f"{sum(1 for k in keys if k['role'] == 'ZSK')} ZSKs present"
        elif assertion == "one_ksk_present":
            ok = sum(1 for k in keys if k["role"] == "KSK") == 1
            detail = f"{sum(1 for k in keys if k['role'] == 'KSK')} KSKs present"
        elif assertion == "zsk_active":
            ok = any(
                k["role"] == "ZSK"
                and k["states"].get("zone signing", {}).get("value") == "yes"
                for k in keys
            )
        elif assertion == "cds_present":
            cds = self.bind.dig(self.zone, "CDS")
            ok = "CDS" in cds and "RRSIG" in cds
            detail = "CDS published" if ok else "no CDS record"
        else:
            raise ValueError(f"unknown assertion: {assertion}")
        self.record("assert", {
            "name": assertion, "passed": ok, "detail": detail,
        })
        if not ok:
            raise AssertionError(f"assertion failed: {assertion}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("scenario", type=pathlib.Path)
    p.add_argument("--rc", type=pathlib.Path,
                   default=pathlib.Path("runtime/clock/faketime.rc"))
    p.add_argument("--container", default="bind-auth",
                   help="docker compose service name of the BIND container")
    p.add_argument("--rndc-key-in-container", default="/etc/bind/rndc.key",
                   help="path to rndc.key INSIDE the container")
    p.add_argument("--faketime-lib",
                   default="/opt/faketime/lib/faketime/libfaketimeMT.so.1",
                   help="path to libfaketimeMT.so.1 INSIDE the container")
    p.add_argument("--log-dir", type=pathlib.Path,
                   default=pathlib.Path("runtime/bind-logs"))
    p.add_argument("--timeline", type=pathlib.Path,
                   default=pathlib.Path("observations/timeline.jsonl"))
    args = p.parse_args(argv)

    scenario = yaml.safe_load(args.scenario.read_text())

    # PyYAML auto-parses ISO8601 timestamps into datetime objects, so accept either.
    start_raw = scenario.get("start", "2026-01-01T00:00:00")
    start = _coerce_datetime(start_raw)
    speed = float(scenario.get("speed", 60))

    clock = VirtualClock(args.rc, start=start, speed=speed)
    bind = BindController(
        container=args.container,
        rndc_key_in_container=args.rndc_key_in_container,
        faketime_lib=args.faketime_lib,
        log_dir=args.log_dir,
    )
    timeline = TimelineLog(args.timeline)

    # Wait for BIND to come up (real-time).
    for attempt in range(60):
        try:
            bind.rndc("status", timeout=10)
            break
        except Exception:
            time.sleep(1)
    else:
        print("BIND did not become reachable in 60s", file=sys.stderr)
        clock.stop()
        return 1

    runner = ScenarioRunner(scenario, clock, bind, timeline)
    try:
        runner.run()
    except AssertionError as e:
        print(f"SCENARIO FAILED: {e}", file=sys.stderr)
        clock.stop()
        return 2
    clock.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

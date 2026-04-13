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
import pathlib
import re
import subprocess
import sys
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
    """Owns the shared faketime.rc file and the current segment."""

    def __init__(self, rc_path: pathlib.Path, start: dt.datetime, speed: float):
        self.rc_path = rc_path
        self.rc_path.parent.mkdir(parents=True, exist_ok=True)
        self.segment = TimeSegment(
            fake_start=start, real_anchor=time.time(), speed=speed
        )
        self._write()

    def _write(self) -> None:
        # Atomic replace so BIND never reads a half-written file.
        tmp = self.rc_path.with_suffix(".rc.tmp")
        tmp.write_text(self.segment.to_faketime_line())
        tmp.replace(self.rc_path)

    def set_speed(self, new_speed: float) -> None:
        # Anchor the current virtual instant, then change speed.
        now_virtual = self.segment.virtual_now()
        self.segment = TimeSegment(
            fake_start=now_virtual, real_anchor=time.time(), speed=new_speed
        )
        self._write()

    def jump_to(self, virtual_target: dt.datetime) -> None:
        """Discontinuously move virtual time forward. Use sparingly —
        this hides any bug that depends on timers firing in sequence."""
        self.segment = TimeSegment(
            fake_start=virtual_target,
            real_anchor=time.time(),
            speed=self.segment.speed,
        )
        self._write()

    def virtual_now(self) -> dt.datetime:
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


# ---------------------------------------------------------------------------
# Talking to BIND (at real speed — NOT under faketime)
# ---------------------------------------------------------------------------

class BindController:
    """Wraps rndc / dig / log-tail. All calls run at real wall time; we
    never LD_PRELOAD libfaketime into these clients because their own
    socket timeouts would then be scaled and misbehave."""

    def __init__(self, host: str, rndc_port: int, dns_port: int,
                 rndc_key: pathlib.Path, log_dir: pathlib.Path):
        self.host = host
        self.rndc_port = rndc_port
        self.dns_port = dns_port
        self.rndc_key = rndc_key
        self.log_dir = log_dir

    def rndc(self, *args: str, timeout: float = 10.0, retries: int = 0) -> str:
        cmd = [
            "rndc", "-s", self.host, "-p", str(self.rndc_port),
            "-k", str(self.rndc_key), *args,
        ]
        last_err = None
        for attempt in range(1 + retries):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout
                )
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

    def dig(self, qname: str, qtype: str, timeout: float = 5.0) -> str:
        cmd = [
            "dig", f"@{self.host}", "-p", str(self.dns_port),
            "+dnssec", "+multiline", "+norecurse", qname, qtype,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout

    def dnssec_status(self, zone: str) -> str:
        return self.rndc("dnssec", "-status", zone)

    def checkds_published(self, zone: str, when: Optional[str] = None) -> str:
        args = ["dnssec", "-checkds"]
        if when:
            args += ["-when", when]
        args += ["published", zone]
        return self.rndc(*args)

    def checkds_withdrawn(self, zone: str, when: Optional[str] = None) -> str:
        args = ["dnssec", "-checkds"]
        if when:
            args += ["-when", when]
        args += ["withdrawn", zone]
        return self.rndc(*args)

    def rollover(self, zone: str, key_id: int) -> str:
        return self.rndc("dnssec", "-rollover", "-key", str(key_id), zone)

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
            "start": self.clock.virtual_now().isoformat(),
            "speed": self.clock.segment.speed,
        })
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
            self.record("snapshot", {
                "status": self.bind.dnssec_status(self.zone),
                "dnskey": self.bind.dig(self.zone, "DNSKEY"),
                "cds": self.bind.dig(self.zone, "CDS"),
            })

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
                   default=pathlib.Path("runtime/faketime.rc"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--rndc-port", type=int, default=19953)
    p.add_argument("--dns-port",  type=int, default=15353)
    p.add_argument("--rndc-key", type=pathlib.Path,
                   default=pathlib.Path("runtime/rndc.key"))
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
        host=args.host, rndc_port=args.rndc_port, dns_port=args.dns_port,
        rndc_key=args.rndc_key, log_dir=args.log_dir,
    )
    timeline = TimelineLog(args.timeline)

    # Wait for BIND to come up (real-time).
    for attempt in range(60):
        try:
            bind.rndc("status", timeout=3)
            break
        except Exception:
            time.sleep(1)
    else:
        print("BIND did not become reachable in 60s", file=sys.stderr)
        return 1

    runner = ScenarioRunner(scenario, clock, bind, timeline)
    try:
        runner.run()
    except AssertionError as e:
        print(f"SCENARIO FAILED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

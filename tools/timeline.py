#!/usr/bin/env python3
"""
Timeline viewer for DNSSEC Time-Warp observations.

Reads observations/timeline.jsonl and renders a human-readable summary
of the scenario run, with key state transitions, assertions, speed
changes, and actions on a virtual-time axis.

Usage:
    python3 tools/timeline.py [timeline.jsonl]
    python3 tools/timeline.py --keys-only observations/timeline.jsonl
    python3 tools/timeline.py --json observations/timeline.jsonl
"""

from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


# ANSI colors for terminal output.
class C:
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    CYAN    = "\033[36m"
    RESET   = "\033[0m"


def _no_color():
    for attr in ("BOLD", "DIM", "RED", "GREEN", "YELLOW", "BLUE", "CYAN", "RESET"):
        setattr(C, attr, "")


def load_timeline(path: Path) -> list[dict]:
    records = []
    with path.open() as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"warning: line {lineno}: {e}", file=sys.stderr)
    return records


def fmt_virtual(iso: str) -> str:
    """Short virtual time display."""
    try:
        t = datetime.fromisoformat(iso)
        return t.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16]


def fmt_key_summary(keys: list[dict]) -> str:
    """One-line summary of key states."""
    parts = []
    for k in keys:
        role = k.get("role", "?")
        kid = k.get("id", "?")
        states = k.get("states", {})
        active_flags = []
        for state_name, state_val in states.items():
            if state_val.get("value") == "yes":
                active_flags.append(state_name)
        flags = ", ".join(active_flags) if active_flags else "none"
        parts.append(f"{role} {kid} [{flags}]")
    return " | ".join(parts)


def render_event(rec: dict, prev_keys: list[dict] | None) -> tuple[str, list[dict] | None]:
    """Render a single timeline record. Returns (output_line, updated_keys)."""
    vt = fmt_virtual(rec["v"])
    kind = rec["kind"]
    data = rec.get("data", "")
    keys = prev_keys

    if kind == "scenario_start":
        zone = data.get("zone", "?") if isinstance(data, dict) else "?"
        speed = data.get("speed", "?") if isinstance(data, dict) else "?"
        return f"{C.BOLD}{C.CYAN}=== Scenario start: {zone} @ x{speed} ==={C.RESET}", keys

    if kind == "scenario_end":
        return f"{C.BOLD}{C.CYAN}=== Scenario complete ==={C.RESET}", keys

    if kind == "step_wait":
        desc = data.get("description", "") if isinstance(data, dict) else ""
        real_s = data.get("real_seconds", 0) if isinstance(data, dict) else 0
        target = data.get("target_virtual", "") if isinstance(data, dict) else ""
        real_min = real_s / 60 if real_s else 0
        return (
            f"{C.DIM}{vt}{C.RESET}  {C.YELLOW}WAIT{C.RESET} "
            f"until {fmt_virtual(target)} ({real_min:.1f} real min) — {desc}"
        ), keys

    if kind == "speed_change":
        spd = data.get("new_speed", "?") if isinstance(data, dict) else data
        return f"{vt}  {C.BLUE}SPEED{C.RESET} -> x{spd}", keys

    if kind == "action":
        msg = data if isinstance(data, str) else json.dumps(data)
        return f"{vt}  {C.CYAN}ACTION{C.RESET} {msg}", keys

    if kind == "rndc_output":
        msg = data if isinstance(data, str) else str(data)
        return f"{vt}  {C.DIM}  rndc: {msg}{C.RESET}", keys

    if kind == "assert":
        name = data.get("name", "?") if isinstance(data, dict) else str(data)
        passed = data.get("passed", False) if isinstance(data, dict) else False
        detail = data.get("detail", "") if isinstance(data, dict) else ""
        if isinstance(detail, list):
            detail = fmt_key_summary(detail)
        color = C.GREEN if passed else C.RED
        icon = "PASS" if passed else "FAIL"
        return f"{vt}  {color}{icon}{C.RESET} {name}: {detail}", keys

    if kind == "key_state_change":
        new_keys = data.get("keys", []) if isinstance(data, dict) else []
        summary = fmt_key_summary(new_keys)
        keys = new_keys
        return f"{vt}  {C.GREEN}KEYS{C.RESET} {summary}", keys

    if kind == "heartbeat":
        count = data.get("key_count", "?") if isinstance(data, dict) else "?"
        return f"{C.DIM}{vt}  heartbeat ({count} keys){C.RESET}", keys

    if kind == "snapshot":
        return f"{vt}  {C.BOLD}{C.YELLOW}SNAPSHOT{C.RESET} captured", keys

    if kind == "sample_error":
        msg = data if isinstance(data, str) else str(data)
        return f"{vt}  {C.RED}ERROR{C.RESET} {msg}", keys

    # Fallback for unknown kinds.
    return f"{vt}  {kind}: {data}", keys


def render_keys_only(records: list[dict]) -> None:
    """Show only key state changes as a compact table."""
    print(f"{'Virtual Time':<20} {'Keys'}")
    print("-" * 80)
    for rec in records:
        if rec["kind"] != "key_state_change":
            continue
        vt = fmt_virtual(rec["v"])
        keys = rec.get("data", {}).get("keys", [])
        print(f"{vt:<20} {fmt_key_summary(keys)}")


def render_full(records: list[dict], show_heartbeats: bool = False) -> None:
    """Render the full timeline with color."""
    keys: list[dict] | None = None
    for rec in records:
        if not show_heartbeats and rec["kind"] == "heartbeat":
            continue
        line, keys = render_event(rec, keys)
        print(line)


def render_summary(records: list[dict]) -> None:
    """Print a statistical summary of the scenario run."""
    if not records:
        print("No records found.")
        return

    start = end = None
    start_real = end_real = None
    key_changes = 0
    assertions_passed = 0
    assertions_failed = 0
    snapshots = 0
    actions = 0
    errors = 0

    for rec in records:
        if rec["kind"] == "scenario_start":
            start = rec["v"]
            start_real = rec["r"]
        elif rec["kind"] == "scenario_end":
            end = rec["v"]
            end_real = rec["r"]
        elif rec["kind"] == "key_state_change":
            key_changes += 1
        elif rec["kind"] == "assert":
            data = rec.get("data", {})
            if isinstance(data, dict) and data.get("passed"):
                assertions_passed += 1
            else:
                assertions_failed += 1
        elif rec["kind"] == "snapshot":
            snapshots += 1
        elif rec["kind"] == "action":
            actions += 1
        elif rec["kind"] == "sample_error":
            errors += 1

    print(f"{C.BOLD}Scenario Summary{C.RESET}")
    print(f"  Virtual span:     {fmt_virtual(start) if start else '?'} -> {fmt_virtual(end) if end else '?'}")
    if start_real and end_real:
        t0 = datetime.fromisoformat(start_real)
        t1 = datetime.fromisoformat(end_real)
        real_dur = (t1 - t0).total_seconds()
        hours = int(real_dur // 3600)
        mins = int((real_dur % 3600) // 60)
        print(f"  Real duration:    {hours}h{mins:02d}m")
    print(f"  Key state changes: {key_changes}")
    print(f"  Assertions:        {C.GREEN}{assertions_passed} passed{C.RESET}"
          + (f", {C.RED}{assertions_failed} failed{C.RESET}" if assertions_failed else ""))
    print(f"  Snapshots:         {snapshots}")
    print(f"  Actions:           {actions}")
    if errors:
        print(f"  Errors:            {C.RED}{errors}{C.RESET}")


def main() -> None:
    p = argparse.ArgumentParser(description="DNSSEC Time-Warp timeline viewer")
    p.add_argument("timeline", nargs="?", type=Path,
                   default=Path("observations/timeline.jsonl"),
                   help="Path to timeline.jsonl")
    p.add_argument("--keys-only", action="store_true",
                   help="Show only key state change events")
    p.add_argument("--heartbeats", action="store_true",
                   help="Include heartbeat events in output")
    p.add_argument("--summary", action="store_true",
                   help="Show a statistical summary")
    p.add_argument("--json", action="store_true",
                   help="Output raw JSON records (pretty-printed)")
    p.add_argument("--no-color", action="store_true",
                   help="Disable ANSI color output")
    args = p.parse_args()

    if args.no_color or not sys.stdout.isatty():
        _no_color()

    if not args.timeline.exists():
        print(f"No timeline found at {args.timeline}", file=sys.stderr)
        print("Run a scenario first:  make run SCENARIO=scenarios/ksk-rollover.yaml",
              file=sys.stderr)
        sys.exit(1)

    records = load_timeline(args.timeline)
    if not records:
        print("Timeline is empty.", file=sys.stderr)
        sys.exit(1)

    if args.json:
        for rec in records:
            print(json.dumps(rec, indent=2, default=str))
        return

    if args.summary:
        render_summary(records)
        return

    if args.keys_only:
        render_keys_only(records)
        return

    render_full(records, show_heartbeats=args.heartbeats)
    print()
    render_summary(records)


if __name__ == "__main__":
    main()

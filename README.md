# DNSSEC Time-Warp Lab

A harness for watching BIND's `dnssec-policy` state machine progress through
key lifecycle events (KSK/ZSK rollovers, DS publication, RRSIG refresh) at
accelerated but *continuous* time — so you observe timers firing in their
correct relative order, just faster.

## How the time warp works

BIND 9 runs inside a container with `libfaketimeMT.so` injected via
`LD_PRELOAD`. Libfaketime intercepts every libc time call and returns

    virtual_now = fake_start + (real_now - real_anchor) * speed

The `fake_start`, `real_anchor`, and `speed` live in a single config file
(`runtime/faketime.rc`) that the orchestrator writes and BIND reads
through a bind mount. libfaketime caches the config for 1 real second
(`FAKETIME_CACHE_DURATION=1`), so changes propagate within a second.

The orchestrator also runs a background "ticker" thread that rewrites
`faketime.rc` every 0.5 real seconds, re-anchoring `@TIMESTAMP` at the
current virtual time. This is necessary because libfaketime's
`@TIMESTAMP xSPEED` format treats `@TIMESTAMP` as the virtual time at
library-load moment — short-lived processes (rndc, dig, date) would
otherwise *always* see @TIMESTAMP from the file, never advancing.
Rewriting keeps those processes' clocks in sync with the long-lived
named process.

### BIND must be custom-built

Debian's `bind9` package links against **jemalloc**. jemalloc spawns
internal threads during startup that deadlock against libfaketime's
function interposition (named hangs in `futex_wait` before printing a
banner). BIND upstream has `--without-jemalloc`, so the Dockerfile
compiles BIND from source. Ripping the jemalloc dep out via `patchelf`
does not work — BIND actually calls jemalloc extensions like
`mallocx()` that aren't in glibc.

To swap BIND versions, change `BIND_VERSION` in `docker/bind/Dockerfile`.

### libfaketime must be ≥ v0.9.11

Debian bookworm ships libfaketime 0.9.10, which has a `clock_gettime()`
reentrancy bug triggered during BIND's early startup. The Dockerfile
builds v0.9.11+ from source.

## Architecture

```
host: orchestrator.py  ──writes──►  runtime/faketime.rc
      (real time)                      │
         │                             │ (bind mount)
         │                             ▼
         │              container: named + libfaketimeMT
         │                        (virtual time)
         │                             ▲
         │ docker compose exec         │
         └── rndc, dig (with          ─┘
             LD_PRELOAD set —
             also virtual time,
             for TSIG clock sync)
```

The orchestrator runs at real time. It invokes `rndc` and `dig` via
`docker compose exec` *inside* the container, with `LD_PRELOAD` set, so
they see the same virtual time BIND does. This is required for TSIG —
if rndc signed with real time while BIND validated against virtual time,
every command would fail "clock skew". Socket timeouts under libfaketime
scale with the speed multiplier, so the orchestrator passes generous
timeouts.

## Usage

```
make up                             # build + start BIND
make run SCENARIO=scenarios/ksk-rollover.yaml
make logs                           # tail dnssec log
make down
```

Live output during a run shows every key-state transition keyed by virtual
time. Full timeline lands in `observations/timeline.jsonl` for post-hoc
analysis.

## Scenario DSL

```yaml
zone: example.test
start: 2026-01-01T00:00:00
speed: 60                   # virtual seconds per real second
observe:
  every: 30m                # virtual — how often to sample state

steps:
  - after: 3d               # wait 3 virtual days
    do: ds_published        # then call rndc dnssec -checkds published
  - after: 1h
    speed: 5                # slow down to watch closely
    assert: two_ksks_present
  - at: 2026-02-15T00:00:00 # absolute virtual time
    snapshot: true
```

Available `do:` actions: `ds_published`, `ds_withdrawn`, `rollover_ksk`,
`rollover_zsk`, `reload`. Available `assert:` checks: `zone_signed`,
`ksk_active`, `zsk_active`, `two_ksks_present`, `two_zsks_present`,
`one_ksk_present`, `cds_present`. Add your own in `orchestrator.py`.

## Included scenarios

| Scenario | File | What it shows | Real time @ default speed |
|---|---|---|---|
| KSK rollover | `scenarios/ksk-rollover.yaml` | Full KSK lifecycle via `dnssec-policy`, DS publication | ~5.6h @ x60 |
| ZSK rollover | `scenarios/zsk-rollover.yaml` | Automatic ZSK rotation (no DS involvement) | ~8.5h @ x120 |
| Manual rollover | `scenarios/manual-rollover.yaml` | Operator-triggered KSK roll, DS publish/withdraw | ~3.5h @ x100 |

## Analyzing results

After a run completes, `observations/timeline.jsonl` contains every
observation keyed by both virtual and real time.

```
make timeline           # full event log with color
make timeline-keys      # just key state transitions
make timeline-summary   # statistical summary
python3 tools/timeline.py --json   # raw JSON for scripting
```

## Gotchas

- **Multiplier ceiling.** Above ~x200 BIND's signing pipeline can't keep
  up with its own scheduled work. Stay in x10–x200 for realistic behavior.
- **Key generation timestamps.** Keys must be generated *inside* the
  faked environment so their mtimes match virtual time. `dnssec-policy`
  handles this automatically on first startup.
- **rndc/dig go through `docker compose exec`**, not over TCP from the
  host. They need libfaketime loaded (for TSIG clock sync), which means
  their socket timeouts scale — the orchestrator passes generous values.
- **Monotonic clock is NOT faked** (`DONT_FAKE_MONOTONIC=1`). Leaving
  it real avoids several libfaketime bugs (the 0.9.10 recursion, some
  jemalloc interaction paths). BIND's timer queue uses CLOCK_MONOTONIC
  and will run at real speed, while its wall-clock decisions run at
  virtual speed — that's exactly what we want for dnssec-policy, whose
  scheduling is purely wall-clock based.
- **Log timestamps are virtual.** `named.run` will show virtual dates.
  Cross-reference with `timeline.jsonl` which has both virtual and real.

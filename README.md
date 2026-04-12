# DNSSEC Time-Warp Lab

A harness for watching BIND's `dnssec-policy` state machine progress through
key lifecycle events (KSK/ZSK rollovers, DS publication, RRSIG refresh) at
accelerated but *continuous* time — so you observe timers firing in their
correct relative order, just faster.

## How the time warp works

Stock BIND 9 runs inside a container with `libfaketimeMT.so` injected via
`LD_PRELOAD`. Libfaketime intercepts every libc time call and returns

    virtual_now = fake_start + (real_now - real_anchor) * speed

The `fake_start`, `real_anchor`, and `speed` live in a single config file
(`runtime/faketime.rc`) that both the orchestrator (writer) and BIND
(reader, via libfaketime) see through a bind mount. `FAKETIME_NO_CACHE=1`
makes libfaketime re-read that file on every call, so changes propagate
immediately.

**Nothing about BIND is patched.** Swap in any BIND version by changing
the base image in `docker/bind/Dockerfile`.

## Architecture

```
host: orchestrator.py  ──writes──►  runtime/faketime.rc
      (real time)                      │
         │                             │ (bind mount)
         │ rndc / dig / log-tail       ▼
         └───────────────────►  container: named + libfaketimeMT
                                (virtual time)
```

The orchestrator runs at real time — we deliberately do NOT preload
libfaketime into `rndc` or `dig`, because their socket timeouts would
then scale with the multiplier and misbehave.

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
`reload`. Available `assert:` checks: `zone_signed`, `ksk_active`,
`two_ksks_present`. Add your own in `orchestrator.py`.

## Gotchas

- **Multiplier ceiling.** Above ~x200 BIND's signing pipeline can't keep
  up with its own scheduled work. Stay in x10–x200 for realistic behavior.
- **Key generation timestamps.** Keys must be generated *inside* the
  faked environment so their mtimes match virtual time. `dnssec-policy`
  handles this automatically on first startup.
- **rndc clients run at real speed.** Never `LD_PRELOAD` libfaketime into
  rndc or dig — their timeouts will scale and break.
- **Monotonic clock.** libfaketime fakes `CLOCK_MONOTONIC` by default
  (`DONT_FAKE_MONOTONIC=0`). If you disable that, BIND's timer queue can
  drift apart from its realtime decisions.
- **Log timestamps are virtual.** `named.run` will show virtual dates.
  Cross-reference with `timeline.jsonl` which has both virtual and real.

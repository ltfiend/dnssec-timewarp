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
(`runtime/clock/faketime.rc`) that the orchestrator writes and BIND reads
through a bind mount. libfaketime caches the config for 1 real second
(`FAKETIME_CACHE_DURATION=1`), so changes propagate within a second.

The mount is a **directory** (`./runtime/clock` → `/opt/lab`), not a
single-file mount. Single-file bind mounts in Docker pin the
container's view to the inode at container-start time — any update on
the host that changes the inode (atomic rename) or even some in-place
modification patterns can fail to reach the container, and BIND would
silently keep reading the original `make init` placeholder.

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

## Prerequisites

- **Docker** (Engine ≥ 20.10) with the `compose` plugin — `docker compose`
  must work.
- **Python 3.10+** on the host with PyYAML:
  `pip install -r requirements.txt` (or `pip install pyyaml`).
- **BIND utilities on the host** are NOT required. `rndc` and `dig` run
  inside the container; the host only runs `orchestrator.py`.
- **Network access during build.** The Dockerfile downloads BIND source
  from `downloads.isc.org` and clones libfaketime from GitHub. If your
  environment blocks outbound HTTPS, mirror these locally and rewrite
  the URLs in `docker/bind/Dockerfile`.
- **Disk.** The first-stage build image is ~1 GB (full BIND build deps).
  The final runtime image is ~200 MB.

## First-time build and smoke test

The commands below reproduce the exact run that was verified working.
Each step explains what should happen so you can tell where it fails.

```bash
# 1. Create runtime directories and a placeholder faketime.rc + rndc.key.
#    `make init` writes runtime/faketime.rc, and pulls a throwaway Debian
#    container just to run `rndc-confgen` so the key format matches what
#    the in-container rndc expects.
make init

# 2. Build the container image. This is the long step (3-8 minutes on a
#    warm system). It compiles BIND 9.18.47 from source with
#    --without-jemalloc (to avoid the jemalloc/libfaketime deadlock), then
#    compiles libfaketime v0.9.11 from source (Debian's 0.9.10 has a
#    clock_gettime() reentrancy bug). Multi-stage, so the final image
#    doesn't carry the build toolchain.
make build        # or: docker compose build

# 3. Start BIND. With just `make up` this combines init + build + up;
#    since you already did init/build, `docker compose up -d` is enough.
docker compose up -d

# 4. Verify named actually started under libfaketime. You should see a
#    BIND banner with virtual-time timestamps (2025/2026 dates), key
#    generation messages for KSK and ZSK, and "all zones loaded".
docker compose logs | tail -30

# 5. Run the smoke test (~30 real seconds at x60). This validates the
#    full pipeline: time warp, TSIG via docker-exec'd rndc, dnssec-policy
#    key generation, and scenario assertions.
python3 orchestrator/orchestrator.py scenarios/smoke.yaml

# 6. Inspect results.
python3 tools/timeline.py --summary
```

Expected smoke-test output (last line of each step should match shape):

```
[virtual 2026-01-01 00:00:07] scenario_start
[virtual 2026-01-01 00:05:13] key_state_change
[virtual 2026-01-01 00:05:25] assert   zone_signed  passed
[virtual 2026-01-01 00:15:39] assert   ksk_active   passed
[virtual 2026-01-01 00:25:52] assert   zsk_active   passed
[virtual 2026-01-01 00:31:12] snapshot
[virtual 2026-01-01 00:31:12] scenario_end
```

If all three assertions pass, the lab is fully working. You can then run
the longer scenarios below.

## Running scenarios

```
make run SCENARIO=scenarios/ksk-rollover.yaml
make logs                           # tail dnssec log (virtual timestamps)
make down
```

Live output during a run shows every key-state transition keyed by virtual
time. Full timeline lands in `observations/timeline.jsonl` for post-hoc
analysis.

## Resetting BIND state between runs

BIND writes zone and key state under `runtime/bind-data/` (owned by the
container's `bind` user, uid 107 in the image). Because those files are
owned inside the container, you can't `rm -rf runtime/` from the host
without sudo. Use a throwaway container instead:

```bash
docker compose down
docker run --rm -v "$PWD/runtime:/runtime" debian:bookworm-slim \
    sh -c 'rm -rf /runtime/bind-data/* /runtime/bind-logs/*'
echo "@2026-01-01 00:00:00 x1" > runtime/faketime.rc
rm -f observations/timeline.jsonl
docker compose up -d
```

`make clean` will try to remove `runtime/` with a plain `rm -rf` and will
fail with `Permission denied` on the container-owned files. Either edit
the Makefile to run the removal inside a container, or use `sudo rm -rf
runtime observations` as an escape hatch.

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

## Troubleshooting

Specific failure modes that were encountered and diagnosed while bringing
the lab up. If you see one of these, here's what it means.

**Build fails on `./configure --without-jemalloc`: "configure: error:
<some library> not found".**
One of BIND's build deps is missing. The Dockerfile installs the full
set for 9.18.47. If you bumped `BIND_VERSION`, newer BINDs may need
additional libraries — check the build-deps list in stage 1 of
`docker/bind/Dockerfile` against the upstream BIND release notes.

**Build fails downloading BIND: "HTTP/2 404" or "SSL error".**
ISC sometimes restructures `downloads.isc.org` paths. Check whether
`https://downloads.isc.org/isc/bind9/${BIND_VERSION}/bind-${BIND_VERSION}.tar.xz`
actually exists for your chosen version. Released versions are stable;
dev/snapshot versions move around.

**`libfaketime: Unexpected recursive calls to clock_gettime() without
proper initialization.`**
You're on libfaketime 0.9.10. Make sure `LIBFAKETIME_REF` in the
Dockerfile is `v0.9.11` or later and that the container built the
from-source version (check `ls /opt/faketime/lib/faketime/` in the
image should show two `.so.1` files).

**`named` container starts but produces no output, and `dig` times out.**
Almost always the jemalloc deadlock: named is hung in `futex_wait`
before printing its banner. Verify you built *with* `--without-jemalloc`:
`docker run --rm dnssec-timewarp-bind-auth:latest /usr/sbin/named -V |
head -3` should show the configure line including `'--without-jemalloc'`.

**`rndc: connection to remote host closed ... clocks are not
synchronized`** *(or named log shows `invalid command from … : expired`
or `: clock skew`)*.
TSIG skew. Three possible causes:

1. **You're running rndc from the host over TCP** — don't. Use
   `make rndc ARGS='…'` or `docker compose exec`. Host rndc has no
   libfaketime, so it signs with real time (~2026-04) while BIND
   validates against virtual time (~2026-01).

2. **Stale bind mount.** If you have an older checkout where
   `docker-compose.yml` mounts `./runtime/faketime.rc` as a single
   file (instead of `./runtime/clock` as a directory), the
   container's view of the file is pinned to the inode at
   container-start time and the orchestrator's writes never reach
   BIND. Diagnostic:
   ```
   echo "host inode:      $(stat -c '%i' runtime/clock/faketime.rc)"
   echo "container inode: $(docker compose exec -T bind-auth stat -c '%i' /opt/lab/faketime.rc)"
   ```
   These MUST match. If they don't, `docker compose down` and bring
   the container back up — and make sure you're on the directory-
   mount version of the compose file.

3. **Orchestrator ticker isn't running.** `runtime/clock/faketime.rc`
   should have a sub-second-old mtime while the orchestrator is
   running: `stat -c '%Y %n' runtime/clock/faketime.rc; date +%s`.

**`named.conf:41: undefined category: 'dnssec-policy'`.**
You're on BIND < 9.19. The `dnssec-policy` log category was split out
in 9.19. On 9.18, those messages land in the `dnssec` category, which
the current `named.conf` already routes correctly.

**`zone 'example.test': empty 'parental-agents' entry`.**
BIND 9.18 rejects an empty `parental-agents { };` block as a syntax
error. Omit the block entirely — BIND treats the absence as "no
parental agents configured", which is what we want for manual
`rndc dnssec -checkds` control.

**`rm: cannot remove 'runtime/bind-data/...': Permission denied`.**
Those files are owned by the container's `bind` user. Use the
throwaway-container recipe in "Resetting BIND state" above.

**`dig: src/unix/udp.c:292: uv__udp_recvmsg: Assertion ... failed`.**
A host-side libuv/dig bug, not related to this lab. Retry; it's
transient.

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

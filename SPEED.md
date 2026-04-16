# Simulation Speed and Fidelity

How fast can the DNSSEC Time-Warp Lab run, and what breaks as you push
the multiplier? Analysis of the real-time windows that constrain
reliable operation.

## Why heartbeats drop at high speeds

There's a cascade of real-time windows that each get shorter as you
raise speed. TSIG's fudge window (5 virtual minutes, RFC 2845 default —
BIND doesn't expose a knob for rndc) divided by speed gives you the
real-time budget for any one rndc round-trip to stay in sync:

| speed | TSIG budget (real) | ticker interval   | libfaketime cache | docker exec overhead | total jitter   |
|-------|--------------------|-------------------|-------------------|----------------------|----------------|
| x60   | 5.0 s              | 0.5 s → 30 v-sec  | 1.0 s → 60 v-sec  | ~0.2 s → 12 v-sec    | ~1.7 v-min     |
| x100  | 3.0 s              | 0.5 s → 50 v-sec  | 1.0 s → 100 v-sec | ~0.2 s → 20 v-sec    | ~2.8 v-min     |
| x200  | 1.5 s              | 0.5 s → 100 s     | 1.0 s → 200 s     | ~0.2 s → 40 s        | ~5.7 v-min     |
| x600  | 0.5 s              | 0.5 s → 300 s     | 1.0 s → 600 s     | ~0.2 s → 120 s       | **~17 v-min**  |

At x600, worst-case jitter already exceeds the 5-minute fudge, so
drops aren't surprises — they're baked in. The ticker interval alone
(0.5 s → 5 virtual minutes) is right at the fudge. Docker exec
latency and libfaketime cache add more.

## The hard ceiling

At roughly **x300**, `ticker + cache + exec` fits inside TSIG fudge
most of the time. Above that, you get occasional rejections. Above
~x500, you get frequent rejections. Above ~x1000, rndc is effectively
non-functional — but dig still works (no TSIG).

Things that break at high speeds, in order of when they hit:

1. **TSIG rejections** (starts ~x200, frequent ~x500) — what the
   heartbeat errors look like.
2. **Missed-event observability** — at x600, `observe.every: 15m` is
   1.5 s real. A 3-day scenario finishes in 7 real minutes with 288
   samples. If observe-every drops below 1 s real, sampling saturates
   the container with docker-exec calls.
3. **BIND timer queue lag** (starts ~x500) — BIND uses CLOCK_MONOTONIC
   (real) for when to fire timers, then checks wall-clock (virtual)
   for whether it should act. At x600+, virtual time can race past a
   scheduled event's wall-clock before the real timer fires.
   dnssec-policy re-checks periodically so it self-heals, but
   fine-grained ordering assertions may observe stale states.
4. **BIND signing pipeline saturation** (~x800+, zone-size
   dependent) — with bigger zones, per-RRset signing can't keep up.
5. **Host-CPU contention** (x1000+) — BIND and the orchestrator
   compete with all the docker-exec invocations; observation fidelity
   degrades.

## Fidelity vs speed — what "high-fidelity" means here

Three separable qualities, which respond differently to acceleration:

1. **Temporal correctness of BIND's own decisions.** dnssec-policy
   schedules are driven by virtual wall-clock. At any speed where
   BIND can physically complete its work (CPU-bound, not time-bound),
   this stays correct — key publication happens at the right
   *virtual* moment. The lab's core premise holds up to ~x1000 for
   small zones.

2. **Observation fidelity — catching every state in order.** This is
   speed × observe-every. At x600 sampling every 15 virtual minutes,
   you observe state at 15-min virtual granularity, but each sample
   takes ~300 ms real. Between samples, virtual time advances 15
   minutes, so any sub-15-min transient is visible. Dial
   `observe.every` down relative to the shortest DNSSEC interval you
   care about — publish-safety/retire-safety in `lab-fast` is PT1H,
   so sampling every 15m catches the edges.

3. **Assertion/action timing fidelity.** When a scenario says
   "after 3d, do ds_published", that's a point-in-time event. It
   fires in real time at `3d/speed`. At x600 that's 7.2 minutes real.
   The ticker's worst case is 0.5 s real = 5 virtual minutes at x600.
   So the actual ds_published call can land up to 5 virtual minutes
   after the scheduled time. If your scenario depends on
   sub-5-minute-virtual ordering at x600, it won't hold.

## The heartbeat errors themselves

These are TSIG failures during the sampling call. The orchestrator
already has `retries=2` on the sample; the heartbeat error is the
retry path declaring failure. Three observations:

- They don't corrupt anything. The state BIND reports IS its state at
  that moment. A missed sample is a gap in the timeline, not a wrong
  entry.
- Sampling failures happen when the ticker hasn't fired in the last
  ~0.5 s AND named's cache is near expiry. Fix pressure: shorter
  ticker interval, longer libfaketime cache, or both.
- Retries don't help much at x600 because the drift keeps growing
  during the retry sleep.

## Knobs that already exist vs would need code

**Already available:**
- `speed:` in the scenario — easiest lever.
- `observe.every:` — reduce sampling pressure.
- Inline `speed:` overrides mid-scenario to slow down during critical
  moments (see `scenarios/ksk-rollover.yaml` for an example).
- Skip sampling entirely — omit `observe`.

**Would require code changes:**
- Tighter ticker interval (below 0.5 s starts to burn real CPU on
  Python thread scheduling; ~100 ms is probably floor).
- Longer `FAKETIME_CACHE_DURATION` plus tighter ticker — decouple
  libfaketime's refresh from the ticker's refresh.
- Persistent rndc connection instead of docker-exec-per-call (major
  refactor; rndc doesn't support connection reuse, would need a
  long-lived helper process).
- Raise BIND's TSIG fudge — not supported via `controls {}`; would
  require patching BIND.
- Skip TSIG on loopback — lab-mode only, security regression.
- Batch status queries (one `rndc dnssec -status` + parse, not
  per-zone repeats).

## Practical bands

| Speed band | Fidelity label                       | Notes                                                                 |
|------------|--------------------------------------|-----------------------------------------------------------------------|
| x1–x10     | Reference                            | Matches real time closely; use for debugging timing-sensitive bugs.   |
| x10–x100   | High fidelity                        | All three fidelity qualities hold. No heartbeat drops.                |
| x100–x300  | High fidelity                        | Still clean. Good default for most research runs.                     |
| x300–x600  | Works for research, expect noise     | Occasional heartbeat drops. Assertion timing may slip by v-minutes.   |
| x600+      | Demo mode                            | State machine advances, samples unreliable, fine timing doesn't hold. |

For a scenario that has a critical assertion depending on a specific
state, drop to x10–x60 with an inline `speed:` for just that window
while running the bulk at higher speed.

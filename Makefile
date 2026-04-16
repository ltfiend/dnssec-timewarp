# One-shot commands for the lab.
# `make up` builds+starts BIND, `make run SCENARIO=...` drives it.

RUNTIME := runtime
SCENARIO ?= scenarios/ksk-rollover.yaml

.PHONY: init build up down run logs shell clean timeline rndc time sync freeze

init:
	mkdir -p $(RUNTIME)/bind-data $(RUNTIME)/bind-data/dynamic-zones $(RUNTIME)/bind-logs $(RUNTIME)/clock observations
	# A placeholder faketime.rc so the container's entrypoint can start;
	# orchestrator will overwrite it with the scenario's start time.
	# The file lives in $(RUNTIME)/clock/ and that whole directory is
	# bind-mounted to /opt/lab/ inside the container — directory mounts
	# survive in-place writes (single-file mounts do not, see compose).
	echo "@2026-01-01 00:00:00 x1" > $(RUNTIME)/clock/faketime.rc
	# rndc key shared between orchestrator (on host) and BIND (in container).
	test -f $(RUNTIME)/rndc.key || \
	    docker run --rm -v $(PWD)/$(RUNTIME):/out debian:bookworm-slim \
	        sh -c "apt-get update >/dev/null && apt-get install -y bind9utils >/dev/null \
	               && rndc-confgen -a -k rndc-key -c /out/rndc.key \
	               && chmod 644 /out/rndc.key"

build:
	docker compose build

up: init build
	docker compose up -d
	@echo "BIND auth:  dig @127.0.0.1 -p 15353 example.test SOA +dnssec"
	@echo "rndc runs INSIDE the container (for TSIG clock sync):"
	@echo "            make rndc ARGS='status'"

down:
	docker compose down

run:
	python3 orchestrator/orchestrator.py $(SCENARIO)

logs:
	tail -F $(RUNTIME)/bind-logs/dnssec.log

shell:
	docker compose exec bind-auth bash

rndc:
	docker compose exec -e LD_PRELOAD=/opt/faketime/lib/faketime/libfaketimeMT.so.1 bind-auth rndc -k /etc/bind/rndc.key $(ARGS)

# Show the rc file state vs named's actual virtual_now, and warn if
# they've drifted apart (which happens after the orchestrator dies and
# named keeps projecting forward at scaled speed while the file freezes).
time:
	@python3 tools/sync_clock.py --check || true

# Pull the rc file forward to match named's current virtual_now.
# Preserves named's accumulated DNSSEC state — use this instead of
# restarting the container whenever you want to recover from drift.
sync:
	@python3 tools/sync_clock.py

# Same as sync, but also drops speed to x1 (real time) afterward so
# named effectively stops marching forward. Use to inspect state.
freeze:
	@python3 tools/sync_clock.py --freeze

timeline:
	python3 tools/timeline.py

timeline-keys:
	python3 tools/timeline.py --keys-only

timeline-summary:
	python3 tools/timeline.py --summary

clean: down
	rm -rf $(RUNTIME) observations

# One-shot commands for the lab.
# `make up` builds+starts BIND, `make run SCENARIO=...` drives it.

RUNTIME := runtime
SCENARIO ?= scenarios/ksk-rollover.yaml

.PHONY: init build up down run logs shell clean timeline

init:
	mkdir -p $(RUNTIME)/bind-data $(RUNTIME)/bind-logs observations
	# A placeholder faketime.rc so the container's entrypoint can start;
	# orchestrator will overwrite it with the scenario's start time.
	echo "@2026-01-01 00:00:00 x1" > $(RUNTIME)/faketime.rc
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
	@echo "rndc:       rndc -s 127.0.0.1 -p 19953 -k $(RUNTIME)/rndc.key status"

down:
	docker compose down

run:
	python3 orchestrator/orchestrator.py $(SCENARIO)

logs:
	tail -F $(RUNTIME)/bind-logs/dnssec.log

shell:
	docker compose exec bind-auth bash

timeline:
	python3 tools/timeline.py

timeline-keys:
	python3 tools/timeline.py --keys-only

timeline-summary:
	python3 tools/timeline.py --summary

clean: down
	rm -rf $(RUNTIME) observations

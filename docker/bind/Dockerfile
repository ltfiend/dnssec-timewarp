# Stock BIND 9 wrapped with libfaketime.
# No BIND patches — all time manipulation happens via LD_PRELOAD.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bind9 bind9utils bind9-dnsutils \
        faketime \
        tini \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The multi-threaded variant is REQUIRED for BIND.
# The single-threaded libfaketime.so.1 will cause crashes under BIND's
# thread pool because it uses non-atomic state.
ENV FAKETIME_LIB=/usr/lib/x86_64-linux-gnu/faketime/libfaketimeMT.so.1

# faketime.rc is written by the orchestrator and bind-mounted in.
# NO_CACHE=1 means libfaketime re-reads the file on every time() call.
# Without this, time changes take up to 10s to propagate.
ENV FAKETIME_TIMESTAMP_FILE=/opt/lab/faketime.rc
ENV FAKETIME_NO_CACHE=1
ENV FAKETIME_FORCE_MONOTONIC_FIX=1
ENV DONT_FAKE_MONOTONIC=0

RUN mkdir -p /opt/lab /var/lib/bind /var/log/bind /run/named \
    && chown -R bind:bind /var/lib/bind /var/log/bind /run/named

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 53/udp 53/tcp 953/tcp

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]

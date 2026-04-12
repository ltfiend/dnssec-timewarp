#!/bin/sh
set -eu

# Wait for the orchestrator to write the initial faketime config.
# Without this file, libfaketime falls back to real time — which would
# silently defeat the whole experiment.
echo "[entrypoint] waiting for $FAKETIME_TIMESTAMP_FILE ..."
while [ ! -s "$FAKETIME_TIMESTAMP_FILE" ]; do
    sleep 0.2
done
echo "[entrypoint] faketime config found:"
cat "$FAKETIME_TIMESTAMP_FILE"

# Make sure BIND can read its working files.
chown -R bind:bind /var/lib/bind /var/log/bind /run/named 2>/dev/null || true

# LD_PRELOAD is exported here (not in Dockerfile ENV) so it ONLY affects
# the named process and its children, not the shell above. If it were
# set in ENV, every `docker exec` into the container would also be
# time-warped, which makes debugging painful.
export LD_PRELOAD="$FAKETIME_LIB"

exec /usr/sbin/named -u bind -g -c /etc/bind/named.conf

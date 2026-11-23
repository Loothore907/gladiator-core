#!/bin/sh
# The image starts as root only to do one thing: make a freshly mounted folder (a host's volume at /data, or a
# results folder at /app/runs) writable by the unprivileged user. Then it drops to that user for good.
# Started as any other user (for example by a cluster that forbids root), it runs as that user unchanged.
set -e
if [ "$(id -u)" = "0" ]; then
    for dir in /data /app/runs; do
        if [ -d "$dir" ]; then chown gladiator "$dir" 2>/dev/null || true; fi
    done
    export HOME=/home/gladiator
    exec setpriv --reuid gladiator --regid gladiator --init-groups gladiator "$@"
fi
exec gladiator "$@"

#!/bin/sh
# Three-minute read-only Mac HPM capture around one A16 boot. No HPM writes.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
task_sample=0
while [ "$task_sample" -lt 90 ]; do
    /bin/date -u '+SNAPSHOT %Y-%m-%dT%H:%M:%SZ'
    "$task_root/build/mac-typec-read"
    task_sample=$((task_sample + 1))
    /bin/sleep 2
done

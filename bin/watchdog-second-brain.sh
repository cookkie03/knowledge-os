#!/usr/bin/env bash
# watchdog-second-brain.sh — tiene sempre acceso il server LightRAG della KB
# second-brain. Pensato per girare sotto launchd (KeepAlive): se il server
# muore (crash, kill, reboot, OOM...) lo fa ripartire entro 30s, in automatico.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAGCLI="$SCRIPT_DIR/ragcli"

LOG_FILE="/tmp/lightrag-second-brain-watchdog-loop.log"

while true; do
    "$RAGCLI" server start second-brain 2>&1 | while IFS= read -r line; do
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] $line"
    done >>"$LOG_FILE"
    sleep 30
done

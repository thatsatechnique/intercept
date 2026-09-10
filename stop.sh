#!/usr/bin/env bash
# INTERCEPT - Stop Script
#
# Stops the INTERCEPT server started by start.sh (gunicorn or Flask dev server).
# Only targets processes launched from this repo directory.
#
# Usage:
#   ./stop.sh          # Stop the server
#   ./stop.sh --all    # Also kill orphaned SDR helper processes
#                      # (rtl_fm, rtl_433, dump1090, multimon-ng, ...)
#
# Use sudo if the server was started with sudo.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KILL_HELPERS=0

case "${1:-}" in
    -a|--all) KILL_HELPERS=1 ;;
    -h|--help)
        sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    "") ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
esac

# SDR decoders the app spawns as subprocesses. They can outlive the server.
SDR_HELPERS="rtl_fm|rtl_433|rtl_power|rtl_tcp|rtlamr|dump1090|multimon-ng|acarsdec|dumpvdl2|AIS-catcher|direwolf|slowrx|satdump|rtl_sdr|hackrf_transfer"

# ── Find server processes started from this directory ────────────────────────
find_server_pids() {
    {
        pgrep -f "gunicorn.*${SCRIPT_DIR}/gunicorn.conf.py" || true
        pgrep -f "python[0-9.]* intercept\.py" || true
    } | sort -u
}

# ── Graceful stop, then force ───────────────────────────────────────────────
stop_pids() {
    local pids="$1" label="$2"
    [[ -z "$pids" ]] && return 0

    echo "[INTERCEPT] Stopping ${label}: $(echo "$pids" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true

    for _ in $(seq 1 20); do
        sleep 0.25
        # shellcheck disable=SC2086
        if ! kill -0 $pids 2>/dev/null; then
            return 0
        fi
    done

    echo "[INTERCEPT] Still running after 5s — sending SIGKILL"
    # shellcheck disable=SC2086
    kill -KILL $pids 2>/dev/null || true
    sleep 0.5
}

SERVER_PIDS="$(find_server_pids)"
if [[ -z "$SERVER_PIDS" ]]; then
    echo "[INTERCEPT] Server is not running"
else
    stop_pids "$SERVER_PIDS" "server"
    echo "[INTERCEPT] Server stopped"
fi

# ── Orphaned SDR helpers ─────────────────────────────────────────────────────
HELPER_PIDS="$(pgrep -x -d $'\n' "$SDR_HELPERS" 2>/dev/null || pgrep -d $'\n' -f "^(/[^ ]*/)?(${SDR_HELPERS})( |$)" || true)"

if [[ -n "$HELPER_PIDS" ]]; then
    if [[ "$KILL_HELPERS" -eq 1 ]]; then
        stop_pids "$HELPER_PIDS" "SDR helper processes"
        echo "[INTERCEPT] SDR helpers stopped"
    else
        echo ""
        echo "[INTERCEPT] SDR helper processes still running (may hold the dongle):"
        # shellcheck disable=SC2086
        ps -o pid=,comm= -p $(echo "$HELPER_PIDS" | tr '\n' ',' | sed 's/,$//') | sed 's/^/    /'
        echo "[INTERCEPT] Run ./stop.sh --all to stop them too"
    fi
fi

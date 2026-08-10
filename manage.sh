#!/usr/bin/env bash

set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PID_FILE="$APP_DIR/grok2api.pid"
LOG_FILE="$APP_DIR/grok2api.log"

read_pid() {
    if [[ -f "$PID_FILE" ]]; then
        tr -d '[:space:]' < "$PID_FILE"
    fi
}

is_running() {
    local pid
    pid="$(read_pid)"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    ps -p "$pid" -o args= 2>/dev/null | grep -F -- "$APP_DIR/app.py" >/dev/null
}

clear_stale_pid() {
    if [[ -f "$PID_FILE" ]] && ! is_running; then
        rm -f "$PID_FILE"
    fi
}

start() {
    clear_stale_pid
    if is_running; then
        echo "grok2api is already running (PID $(read_pid))"
        return 0
    fi

    if [[ ! -f "$APP_DIR/app.py" ]]; then
        echo "app.py was not found in $APP_DIR" >&2
        return 1
    fi
    if [[ ! -f "$APP_DIR/config.json" ]]; then
        echo "config.json was not found in $APP_DIR" >&2
        return 1
    fi
    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        echo "Python executable was not found: $PYTHON_BIN" >&2
        return 1
    fi

    cd "$APP_DIR" || return 1
    nohup "$PYTHON_BIN" "$APP_DIR/app.py" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 1

    if is_running; then
        echo "grok2api started (PID $(read_pid))"
        echo "log: $LOG_FILE"
        return 0
    fi

    echo "grok2api failed to start; check $LOG_FILE" >&2
    rm -f "$PID_FILE"
    return 1
}

stop() {
    clear_stale_pid
    if ! is_running; then
        echo "grok2api is not running"
        rm -f "$PID_FILE"
        return 0
    fi

    local pid
    pid="$(read_pid)"
    kill "$pid"

    for _ in {1..10}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "grok2api stopped"
            return 0
        fi
        sleep 1
    done

    echo "grok2api did not stop within 10 seconds (PID $pid)" >&2
    return 1
}

status() {
    clear_stale_pid
    if is_running; then
        echo "grok2api is running (PID $(read_pid))"
        return 0
    fi

    echo "grok2api is stopped"
    return 3
}

logs() {
    touch "$LOG_FILE"
    tail -f "$LOG_FILE"
}

case "${1:-}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop || exit $?
        start
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}" >&2
        exit 2
        ;;
esac

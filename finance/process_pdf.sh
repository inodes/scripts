#!/usr/bin/env bash
# process_pdf.sh — Generic Provider-Driven PDF Processor for Hazel / Folder Actions / CLI
#
# Exit Codes:
#   0 = PDF matched a registered provider and was processed
#   1 = PDF did not match any registered provider (unmatched / fall-through)
#
# Logging:
#   - Emits compact single-line structured log entries (--hazel) to stdout so Hazel
#     captures them in ~/Library/Logs/Hazel/Hazel.log
#   - Appends timestamped entries to ~/Library/Logs/Hazel/process_pdf.log

set -uo pipefail

if (( $# == 0 )); then
    echo "[process_pdf] ERROR status=missing_input_argument" >&2
    exit 2
fi

FINANCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$FINANCE_DIR/.." && pwd)"

PYTHON_BIN="$FINANCE_DIR/.direnv/python-3.11/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$ROOT_DIR/.direnv/python-3.11/bin/python3"
fi
if [[ ! -x "$PYTHON_BIN" ]] && command -v direnv >/dev/null 2>&1; then
    direnv allow "$ROOT_DIR" >/dev/null 2>&1 || true
    PYTHON_BIN="$(direnv exec "$ROOT_DIR" which python3 2>/dev/null || true)"
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3)"
fi

LOG_DIR="$HOME/Library/Logs/Hazel"
LOG_FILE="$LOG_DIR/process_pdf.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true

TS="$(date '+%Y-%m-%d %H:%M:%S')"
OUTPUT="$("$PYTHON_BIN" "$FINANCE_DIR/sync_statements.py" \
    --provider auto \
    --import \
    --delete-duplicates \
    --hazel \
    "$@" 2>&1)"
RC=$?

while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    echo "$line"
    echo "$TS $line" >> "$LOG_FILE" 2>/dev/null || true
done <<< "$OUTPUT"

# In Hazel Action mode, unmatched/skipped files (RC=1) are normal, not errors.
# Only propagate real fatal errors (RC >= 2 or unhandled crashes).
if (( RC == 1 )); then
    exit 0
fi

exit $RC

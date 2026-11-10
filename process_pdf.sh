#!/usr/bin/env bash
# process_pdf.sh — generic PDF dispatcher for Hazel / Folder Actions / CLI.
#
# Usage:
#   process_pdf.sh [--dry-run] FILE.pdf [FILE.pdf ...]
#
# This script knows nothing about any particular kind of document. For each PDF that is
# ready, it offers the file to every section's handler in turn until one claims it:
#
#   <section>/pdf_handler.sh [--dry-run] FILE.pdf        (e.g. finance/pdf_handler.sh)
#
# Handler contract (exit code):
#   0   handled: the handler recognised the file and dealt with it (stop here)
#   1   not mine: leave the file untouched and try the next handler
#   2+  failure: stop and report an error
# Handlers print single-line "[tag] ..." records on stdout; they are logged verbatim.
# A file no handler claims is left exactly where it is.
# Add a new section by creating <section>/pdf_handler.sh; no change is needed here.
#
# Exit codes (Hazel runs this as a "Run shell script" ACTION, where non-zero = error):
#   0   every file was handled or unclaimed (unclaimed files are normal, not errors)
#   2+  real failure (bad usage, handler crash, lock timeout)
#   Set PROCESS_PDF_STRICT=1 to get exit 1 when no handler claimed a file
#   (only for use as a Hazel "Passes shell script" condition).
#
# Environment:
#   PROCESS_PDF_LOG_DIR    log folder (default ~/Library/Logs/Hazel); log file is process_pdf.log
#   PROCESS_PDF_HANDLERS   space-separated handler paths, to override discovery/order
#   PROCESS_PDF_STRICT     see above
#
# Compatible with the stock macOS bash 3.2.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${PROCESS_PDF_LOG_DIR:-$HOME/Library/Logs/Hazel}"
LOG_FILE="$LOG_DIR/process_pdf.log"
LOG_MAX_BYTES=5242880   # rotate at 5 MB
LOCK_DIR="${TMPDIR:-/tmp}/process_pdf.lock"
LOCK_STALE_SECS=600

mkdir -p "$LOG_DIR" 2>/dev/null || true

log() {  # log LINE -> stdout (Hazel captures it) + timestamped log file
    echo "$1"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE" 2>/dev/null || true
}

rotate_log() {
    [[ -f "$LOG_FILE" ]] || return 0
    local size
    size=$(wc -c < "$LOG_FILE" 2>/dev/null | tr -d ' ')
    if [[ -n "$size" ]] && (( size > LOG_MAX_BYTES )); then
        mv -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
    fi
}

file_size() { wc -c < "$1" 2>/dev/null | tr -d ' '; }

# A file is ready when it is a real, non-empty PDF whose size has stopped changing
# (guards against half-downloaded files and iCloud placeholders).
skip_reason() {
    local f="$1" base
    base="$(basename "$f")"
    [[ -e "$f" ]] || { echo "missing"; return; }
    [[ -f "$f" ]] || { echo "not_a_regular_file"; return; }
    case "$base" in
        .*) echo "hidden_or_icloud_placeholder"; return ;;
    esac
    case "$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')" in
        *.pdf) ;;
        *) echo "not_a_pdf"; return ;;
    esac
    case "$f" in
        */Duplicates/*|*/Archive/*|*/.Trash/*) echo "inside_protected_folder"; return ;;
    esac
    local s1 s2
    s1="$(file_size "$f")"
    [[ -n "$s1" && "$s1" != "0" ]] || { echo "empty_or_still_downloading"; return; }
    sleep 1
    s2="$(file_size "$f")"
    [[ "$s1" == "$s2" ]] || { echo "still_being_written"; return; }
    echo ""
}

# Handlers: every <section>/pdf_handler.sh one level below the repo root, in name order.
discover_handlers() {
    if [[ -n "${PROCESS_PDF_HANDLERS:-}" ]]; then
        # shellcheck disable=SC2086
        printf '%s\n' $PROCESS_PDF_HANDLERS
        return
    fi
    local h
    for h in "$ROOT_DIR"/*/pdf_handler.sh; do
        [[ -x "$h" ]] && printf '%s\n' "$h"
    done
}

# Serialise runs: Hazel can fire several at once, and concurrent handlers could race
# on destination names or shared indexes.
acquire_lock() {
    local waited=0 age now mtime
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        now=$(date +%s)
        mtime=$(stat -f %m "$LOCK_DIR" 2>/dev/null || stat -c %Y "$LOCK_DIR" 2>/dev/null || echo "$now")
        age=$(( now - mtime ))
        if (( age > LOCK_STALE_SECS )); then
            rmdir "$LOCK_DIR" 2>/dev/null || true
            continue
        fi
        if (( waited >= 300 )); then
            log "[process_pdf] ERROR status=lock_timeout lock=\"$LOCK_DIR\""
            exit 3
        fi
        sleep 1
        waited=$(( waited + 1 ))
    done
    trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
}

DRY_RUN=0
FILES=()
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=1 ;;
        -h|--help) sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) FILES+=("$arg") ;;
    esac
done

rotate_log

if (( ${#FILES[@]} == 0 )); then
    log "[process_pdf] ERROR status=missing_input_argument"
    exit 2
fi

READY=()
for f in "${FILES[@]}"; do
    reason="$(skip_reason "$f")"
    if [[ -n "$reason" ]]; then
        log "[process_pdf] SKIP status=not_ready file=\"$(basename "$f")\" reason=$reason"
    else
        READY+=("$f")
    fi
done

if (( ${#READY[@]} == 0 )); then
    [[ "${PROCESS_PDF_STRICT:-0}" == "1" ]] && exit 1
    exit 0
fi

HANDLERS=()
while IFS= read -r h; do
    [[ -n "$h" ]] && HANDLERS+=("$h")
done < <(discover_handlers)
if (( ${#HANDLERS[@]} == 0 )); then
    log "[process_pdf] ERROR status=no_handlers_found root=\"$ROOT_DIR\""
    exit 2
fi

HARGS=()
(( DRY_RUN == 1 )) && HARGS+=(--dry-run)

acquire_lock

UNCLAIMED=0
FAILED=0
for f in "${READY[@]}"; do
    claimed=0
    for h in "${HANDLERS[@]}"; do
        section="$(basename "$(dirname "$h")")"
        OUTPUT="$("$h" ${HARGS[@]+"${HARGS[@]}"} "$f" 2>&1)"
        RC=$?
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            log "$line"
        done <<< "$OUTPUT"
        if (( RC == 0 )); then
            claimed=1
            break
        elif (( RC >= 2 )); then
            log "[process_pdf] ERROR status=handler_failed handler=$section rc=$RC file=\"$(basename "$f")\""
            FAILED=$RC
            claimed=1   # do not offer a file to further handlers after a failure
            break
        fi
    done
    if (( claimed == 0 )); then
        UNCLAIMED=$(( UNCLAIMED + 1 ))
        log "[process_pdf] UNCLAIMED file=\"$(basename "$f")\" handlers=${#HANDLERS[@]}"
    fi
done

(( FAILED >= 2 )) && exit "$FAILED"
if (( UNCLAIMED > 0 )) && [[ "${PROCESS_PDF_STRICT:-0}" == "1" ]]; then
    exit 1
fi
exit 0

#!/usr/bin/env bash
# finance/pdf_handler.sh — the Finance section's handler for the generic ../process_pdf.sh.
#
# Usage: pdf_handler.sh [--dry-run] FILE.pdf
#
# Offers the PDF to the registered statement/payslip providers
# (sync_statements.py --provider auto --import --delete-duplicates --hazel).
# Exit: 0 = handled (imported, or filed as a duplicate), 1 = not a finance document, 2+ = failure.
set -uo pipefail

FINANCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$FINANCE_DIR/.." && pwd)"

find_python() {
    local c
    for c in "$FINANCE_DIR/.direnv/python-3.11/bin/python3" "$ROOT_DIR/.direnv/python-3.11/bin/python3"; do
        [[ -x "$c" ]] && { echo "$c"; return 0; }
    done
    if command -v direnv >/dev/null 2>&1; then
        c="$(direnv exec "$ROOT_DIR" which python3 2>/dev/null || true)"
        [[ -n "$c" && -x "$c" ]] && { echo "$c"; return 0; }
    fi
    c="$(command -v python3 2>/dev/null || true)"
    [[ -n "$c" ]] && { echo "$c"; return 0; }
    return 1
}

ARGS=(--provider auto --import --delete-duplicates --hazel)
FILES=()
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) ARGS+=(--dry-run) ;;
        *) FILES+=("$arg") ;;
    esac
done
if (( ${#FILES[@]} == 0 )); then
    echo "[finance] ERROR status=missing_input_argument"
    exit 2
fi

PYTHON_BIN="$(find_python)" || { echo "[finance] ERROR status=python_not_found"; exit 2; }
exec "$PYTHON_BIN" "$FINANCE_DIR/sync_statements.py" "${ARGS[@]}" "${FILES[@]}"

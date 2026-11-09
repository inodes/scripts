#!/usr/bin/env bash
# Compatibility forwarder -> finance/process_pdf.sh
set -uo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$ROOT_DIR/finance/process_pdf.sh" "$@"

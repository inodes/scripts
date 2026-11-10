#!/bin/bash
# Makes every Python/shell script in the repo user-executable (u+x), without
# touching anything else. Skips virtualenvs and caches. Safe to run any time.
#   ./fix_exec_bits.sh                 # fix and list what changed
#   ./fix_exec_bits.sh --check         # list only, exit 1 if any are missing u+x
#   ./fix_exec_bits.sh --install-hook  # (re)create .git/hooks/pre-commit: exec bits + PII guard (hooks are not versioned)
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  cat <<'EOF'
NAME
       fix_exec_bits.sh - ensure all Python and shell scripts in the repository have u+x

SYNOPSIS
       ./fix_exec_bits.sh [--check | --install-hook | -h | --help]

DESCRIPTION
       Scans the repository for *.py and *.sh files (excluding .git, .direnv, .venv,
       and __pycache__) and ensures each has the user-executable bit (u+x) set.

OPTIONS
       --check
              Report files missing u+x and exit 1 if any are found, without modifying files.

       --install-hook
              Install .git/hooks/pre-commit so staged *.py and *.sh files automatically
              receive chmod u+x and git update-index --chmod=+x.

EXIT STATUS
       0      All scripts have u+x (or were fixed / hook installed).
       1      One or more scripts are missing u+x when run with --check.
EOF
  exit 0
fi
if [ "${1:-}" = "--install-hook" ]; then
  # Remove first: if the hook is a symlink, writing through it would overwrite its target.
  rm -f "$ROOT/.git/hooks/pre-commit"
  cat > "$ROOT/.git/hooks/pre-commit" <<'HOOK'
#!/bin/bash
# 1. Every staged .py/.sh must be user-executable, and git must record that.
# 2. Nothing staged may contain personal data (finance/pii_guard.py blocks the commit).
root="$(git rev-parse --show-toplevel)"
git diff --cached --name-only --diff-filter=AM -- '*.py' '*.sh' | while IFS= read -r f; do
  [ -f "$root/$f" ] || continue
  chmod u+x "$root/$f"
  git update-index --chmod=+x -- "$f"
done
exec python3 "$root/finance/pii_guard.py"
HOOK
  chmod u+x "$ROOT/.git/hooks/pre-commit" && echo "installed .git/hooks/pre-commit"
  exit 0
fi
MODE=fix; [ "${1:-}" = "--check" ] && MODE=check
missing=0
while IFS= read -r f; do
  if [ ! -x "$f" ]; then
    missing=$((missing + 1))
    if [ "$MODE" = fix ]; then chmod u+x "$f" && echo "chmod u+x ${f#$ROOT/}"; else echo "missing u+x: ${f#$ROOT/}"; fi
  fi
done < <(find "$ROOT" \( -name .git -o -name .direnv -o -name .venv -o -name venv -o -name node_modules -o -name __pycache__ \) -prune \
         -o -type f \( -name '*.py' -o -name '*.sh' \) -print)
[ "$MODE" = check ] && [ "$missing" -gt 0 ] && exit 1
exit 0

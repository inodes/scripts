#!/usr/bin/env python3
"""Pre-commit PII guard.

Scans files that are about to be committed (staged, or all non-ignored with --all) for:
  * every long digit string / identifier found in the user's private config.json
    (accounts + "personal" sections), so real values can never reach the repo;
  * generic patterns: email addresses and 8-16 digit numbers that are not obviously fake
    (all one digit, or 12345678-style placeholders).

The denylist is derived at run time from the external config.json; nothing sensitive is stored here.
Exit code 1 blocks the commit. Install as a hook:
    ./fix_exec_bits.sh --install-hook   (never symlink the hook to this file)
"""
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

ALLOWED_EMAILS = ("you@gmail.com", "noreply@anthropic.com", "example.com", "git@github.com")
FAKE_NUMBERS = {"12345678", "87654321", "123456789", "00000000", "0000000000"}
EXTRA_WORDS = os.path.join(os.path.dirname(HERE), ".pii_denylist.local")  # optional, gitignored words list


def _is_date(n: str) -> bool:
    """8 digits that form a real calendar date (YYYYMMDD or DDMMYYYY) are dates, not identifiers."""
    if len(n) != 8:
        return False
    for fmt in ("%Y%m%d", "%d%m%Y"):
        try:
            d = datetime.datetime.strptime(n, fmt)
            if 1990 <= d.year <= 2099:
                return True
        except ValueError:
            pass
    return False


def _load_private_values():
    values = set()
    try:
        from statements import config
        cfg = config.load()
    except Exception:
        return values

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(k, str) and re.fullmatch(r"\d{6,}", k):
                    values.add(k)
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            values.update(re.findall(r"\d{6,}", x))
            values.update(re.findall(r"\b[A-Z0-9]{10,}\b", x))
    for sec in ("accounts", "personal"):
        walk(cfg.get(sec))
    if os.path.isfile(EXTRA_WORDS):
        with open(EXTRA_WORDS, encoding="utf-8") as f:
            values.update(w.strip() for w in f if w.strip() and not w.startswith("#"))
    return {v for v in values if len(v) >= 5 and not re.fullmatch(r"(\d)\1+", v)}


def _files(all_files):
    cmd = ["git", "ls-files", "-o", "-m", "--exclude-standard"] if all_files else ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    out = subprocess.check_output(cmd, cwd=ROOT, text=True).split("\n")
    if all_files:
        out += subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).split("\n")
    return sorted({f for f in out if f and os.path.isfile(os.path.join(ROOT, f)) and not f.endswith((".pyc", ".png"))})


def main(argv):
    deny = _load_private_values()
    problems = []
    for rel in _files("--all" in argv):
        try:
            text = open(os.path.join(ROOT, rel), encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        low = text.lower()
        for v in deny:
            if v.lower() in low:
                problems.append(f"{rel}: contains a value from private config.json ({v[:3]}…)")
        for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
            if not any(a in m.group(0) for a in ALLOWED_EMAILS):
                problems.append(f"{rel}: email address {m.group(0)}")
        for m in re.finditer(r"(?<![\d.$,-])\d{8,16}(?![\d.,])", text):
            n = m.group(0)
            if n in FAKE_NUMBERS or re.fullmatch(r"(\d)\1+", n) or n.startswith("0000") or _is_date(n):
                continue
            problems.append(f"{rel}: suspicious {len(n)}-digit number {n[:2]}…")
    if problems:
        print("PII guard: commit blocked")
        for p in sorted(set(problems)):
            print("  -", p)
        return 1
    print("PII guard: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

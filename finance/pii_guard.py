#!/usr/bin/env python3
"""Pre-commit PII guard.

Scans files that are about to be committed (staged, or all non-ignored with --all) for:
  * every long digit string / identifier found in the user's private config.json
    (accounts + "personal" sections), so real values can never reach the repo;
  * generic patterns: email addresses and 8-16 digit numbers that are not obviously fake
    (all one digit, or 12345678-style placeholders).

  * GPS coordinates within ~20 m of a private place (default: the place named "Home"), read at run
    time from a places JSON ({"<name>": {"lat": ..., "lon": ...}}) named by --places, $PII_PLACES_JSON
    or the "places_json" key of the machine-local bootstrap file ($REPOS_CONFIG, default
    ~/.config/repos/config.json). Names of private places: $PII_PLACE_NAMES (comma list, default "Home");
  * private words (street names, schools, names of people) listed one per line in
    ~/.config/repos/pii_denylist (machine-local, outside every repo) or the repo's .pii_denylist.local.

The denylist is derived at run time from external files; nothing sensitive is stored here.
Scan another repo with --repo PATH (for example from the tesla repo's pre-commit hook).
Choose checks with --only numbers,emails,words,coordinates (default: all). Binary files are skipped.
Known public addresses can be allowed with $PII_ALLOWED_EMAILS (comma list).
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
COORD_TOLERANCE = 0.0002  # degrees, about 20 m

ALLOWED_EMAILS = ("you@gmail.com", "noreply@anthropic.com", "example.com", "git@github.com") + tuple(
    e.strip() for e in os.environ.get("PII_ALLOWED_EMAILS", "").split(",") if e.strip())
CHECKS = ("numbers", "emails", "words", "coordinates")
FAKE_NUMBERS = {"12345678", "87654321", "123456789", "00000000", "0000000000"}
EXTRA_WORDS = os.path.join(os.path.dirname(HERE), ".pii_denylist.local")  # optional, gitignored words list
MACHINE_WORDS = os.path.expanduser("~/.config/repos/pii_denylist")           # optional, outside every repo


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


def _load_words():
    values = set()
    for path in (EXTRA_WORDS, MACHINE_WORDS):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                values.update(w.strip() for w in f if w.strip() and not w.startswith("#"))
    return values


def _load_private_values():
    values = _load_words()  # read first: a missing or broken config.json must not skip the word lists
    try:
        from statements import config
        cfg = config.load()
    except Exception:
        return {v for v in values if len(v) >= 5 and not re.fullmatch(r"(\d)\1+", v)}

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
    return {v for v in values if len(v) >= 5 and not re.fullmatch(r"(\d)\1+", v)}


def _arg(argv, flag):
    """Value after `flag` (or after `flag=`), else None."""
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _places_path(argv):
    path = _arg(argv, "--places") or os.environ.get("PII_PLACES_JSON")
    if not path:
        boot = os.environ.get("REPOS_CONFIG") or os.path.expanduser("~/.config/repos/config.json")
        try:
            with open(boot, encoding="utf-8") as f:
                path = json.load(f).get("places_json")
        except (OSError, ValueError):
            path = None
    return os.path.expanduser(path) if path else None


def _load_private_coords(argv):
    """(lat, lon) of every private place, read at run time; empty when no places file is configured."""
    path = _places_path(argv)
    if not path or not os.path.isfile(path):
        return []
    names = {n.strip().lower() for n in os.environ.get("PII_PLACE_NAMES", "Home").split(",") if n.strip()}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    coords = []
    for key, val in (data.items() if isinstance(data, dict) else []):
        if key.strip().lower() in names and isinstance(val, dict) and "lat" in val and "lon" in val:
            try:
                coords.append((float(val["lat"]), float(val["lon"])))
            except (TypeError, ValueError):
                pass
    return coords


_DECIMAL = re.compile(r"(?<![\d.])-?\d{1,3}\.\d{4,}(?!\d)")


def _near_private_place(text, coords):
    for m in _DECIMAL.finditer(text):
        x = float(m.group(0))
        if any(abs(x - la) <= COORD_TOLERANCE or abs(x - lo) <= COORD_TOLERANCE for la, lo in coords):
            return True
    return False


def _files(all_files):
    cmd = ["git", "ls-files", "-o", "-m", "--exclude-standard"] if all_files else ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    out = subprocess.check_output(cmd, cwd=ROOT, text=True).split("\n")
    if all_files:
        out += subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).split("\n")
    return sorted({f for f in out if f and os.path.isfile(os.path.join(ROOT, f)) and not f.endswith((".pyc", ".png"))})


def main(argv):
    global ROOT
    repo = _arg(argv, "--repo")
    if repo:
        ROOT = os.path.abspath(os.path.expanduser(repo))
    only = _arg(argv, "--only")
    enabled = set(CHECKS) if not only else {c.strip() for c in only.split(",") if c.strip()}
    unknown = enabled - set(CHECKS)
    if unknown:
        print(f"PII guard: unknown check(s) {', '.join(sorted(unknown))}; choose from {', '.join(CHECKS)}", file=sys.stderr)
        return 2
    deny = _load_private_values() if "words" in enabled else set()
    coords = _load_private_coords(argv) if "coordinates" in enabled else []
    if "coordinates" in enabled and not coords:
        print("PII guard warning: no private-place coordinates loaded (set places_json in ~/.config/repos/config.json, "
              "or --places / $PII_PLACES_JSON); the coordinate check did NOT run", file=sys.stderr)
    if "words" in enabled and not any(os.path.isfile(x) for x in (EXTRA_WORDS, MACHINE_WORDS)):
        print("PII guard warning: no word list found (~/.config/repos/pii_denylist); only values from the private "
              "config were checked for words", file=sys.stderr)
    problems = []
    for rel in _files("--all" in argv):
        try:
            with open(os.path.join(ROOT, rel), "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if b"\0" in raw:  # binary file
            continue
        text = raw.decode("utf-8", errors="ignore")
        low = re.sub(r"\s+", " ", text.lower())  # words with spaces still match across line wraps and double spaces
        for v in deny:
            vn = re.sub(r"\s+", " ", v.lower())
            if vn in low:
                at = [i for i, ln in enumerate(text.lower().splitlines(), 1) if vn in re.sub(r"\s+", " ", ln)][:5]
                where = "line " + ", ".join(map(str, at)) if at else "text wrapped across lines"
                problems.append(f"{rel}: contains a private value ({v[:3]}…, {len(v)} chars; {where})")
        if "coordinates" in enabled and coords and _near_private_place(text, coords):
            problems.append(f"{rel}: GPS coordinates within ~20 m of a private place")
        for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.-]+", text) if "emails" in enabled else ():
            addr = m.group(0)
            if not addr.rsplit(".", 1)[-1].isalpha():  # name@1.2.3 is a version, not an address
                continue
            if not any(a in addr for a in ALLOWED_EMAILS):
                problems.append(f"{rel}: email address {addr}")
        for m in re.finditer(r"(?<![\d.$,-])\d{8,16}(?![\d.,])", text) if "numbers" in enabled else ():
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

"""Single source of truth for every file location the toolkit reads or writes.

Nothing else in the codebase should spell out an iCloud path. All locations derive from the
"directories" section of the user's external config.json, overlaid on the defaults below:

    icloud      iCloud Drive root: ~/iCloud (symlink) when present, else the Mobile Documents path
    finance     {icloud}/Finance                 (config.json lives here; no PDFs at this level)
    statements  {finance}/Statements             (one sub-folder per provider; no loose files)
    payslips    {finance}/Payslips               (one sub-folder per employer/provider)
    invoices    {finance}/Invoices_Receipts      (one sub-folder per vendor)
    shares      {finance}/Shares
    reports     {finance}/Reports                (generated HTML)
    pdf         {icloud}/PDF                     (Hazel staging folder)
    unsorted / unknown_type / needs_review   {pdf}/<name>   (triage folders, see pdfflow/)
    landing     ~/Downloads
    data        ~/Library/Application Support/pdf-audit   (SQLite: local only, never iCloud)
    logs        ~/Library/Logs/Hazel

Values may reference other keys with {name}. This module imports nothing from the package,
so providers and config can both depend on it.
"""
import json
import os
from typing import Dict, Optional

ICLOUD_REAL = "~/Library/Mobile Documents/com~apple~CloudDocs"
ICLOUD_LINK = "~/iCloud"  # preferred: a symlink to ICLOUD_REAL (ln -s "$HOME/Library/Mobile Documents/com~apple~CloudDocs" ~/iCloud)
ICLOUD = ICLOUD_LINK if os.path.isdir(os.path.expanduser(ICLOUD_LINK)) else ICLOUD_REAL
PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../finance
REPO_DIR = os.path.dirname(PACKAGE_DIR)

DEFAULT_DIRECTORIES: Dict[str, str] = {
    "icloud": ICLOUD,
    "finance": "{icloud}/Finance",
    "statements": "{finance}/Statements",
    "payslips": "{finance}/Payslips",
    "invoices": "{finance}/Invoices_Receipts",
    "shares": "{finance}/Shares",
    "reports": "{finance}/Reports",
    "pdf": "{icloud}/PDF",
    "unsorted": "{pdf}/unsorted",
    "unknown_type": "{pdf}/unknown_type",
    "needs_review": "{pdf}/needs_review",
    "landing": "~/Downloads",
    "data": "~/Library/Application Support/pdf-audit",
    "logs": "~/Library/Logs/Hazel",
}

# Folders that only hold sub-folders (or config): files must never be written directly into them.
CONTAINER_KEYS = ("icloud", "finance", "statements", "payslips", "invoices", "shares")

_CACHE: Optional[Dict[str, str]] = None


class UnsafeLocation(RuntimeError):
    """Raised when a write/move destination breaks the location rules."""


def _norm(p: str) -> str:
    return os.path.abspath(os.path.expanduser(p))


def config_path() -> str:
    """Resolve the active config.json: $PDF_AUDIT_CONFIG, then iCloud Finance, then a repo-local fallback."""
    env = os.environ.get("PDF_AUDIT_CONFIG")
    if env:
        return _norm(env)
    icloud_cfg = _norm(f"{ICLOUD}/Finance/config.json")
    for cand in (icloud_cfg, _norm(f"{ICLOUD_REAL}/Finance/config.json")):
        if os.path.isfile(cand):
            return cand
    repo_cfg = os.path.join(PACKAGE_DIR, "config.json")
    if os.path.isfile(repo_cfg):
        return repo_cfg
    return icloud_cfg


def _resolve(raw: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}

    def one(key: str, seen=()) -> str:
        if key in out:
            return out[key]
        if key in seen:
            raise ValueError(f"directories: circular reference at {key!r}")
        val = raw[key]
        for ref in raw:
            token = "{" + ref + "}"
            if token in val:
                val = val.replace(token, one(ref, seen + (key,)))
        out[key] = _norm(val)
        if key == "icloud" and out[key] == _norm(ICLOUD_LINK) and not os.path.isdir(out[key]):
            out[key] = _norm(ICLOUD_REAL)  # ~/iCloud symlink missing on this machine: use the real folder
        return out[key]

    for k in raw:
        one(k)
    return out


def directories(refresh: bool = False) -> Dict[str, str]:
    """All named locations as absolute paths (defaults overlaid with config.json "directories")."""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    raw = dict(DEFAULT_DIRECTORIES)
    try:
        with open(config_path(), encoding="utf-8") as f:
            user = (json.load(f) or {}).get("directories") or {}
        raw.update({k: v for k, v in user.items() if isinstance(v, str) and v})
    except (OSError, ValueError):
        pass
    _CACHE = _resolve(raw)
    return _CACHE


def expand(value: str) -> str:
    """Expand {name} tokens (from "directories") and ~ in a config value, e.g. "{payslips}/Acme"."""
    out = value
    for key, path in directories().items():
        out = out.replace("{" + key + "}", path)
    return _norm(out)


def get(name: str) -> str:
    return directories()[name]


def under(name: str, *parts: str) -> str:
    """A path inside a named location, e.g. under("statements", "HSBC")."""
    return os.path.join(get(name), *parts)


def statements(*parts: str) -> str:
    return under("statements", *parts)


def payslips(*parts: str) -> str:
    return under("payslips", *parts)


def is_within(path: str, root: str) -> bool:
    p, r = os.path.realpath(_norm(path)), os.path.realpath(_norm(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def in_icloud(path: str) -> bool:
    real = os.path.realpath(_norm(path))
    return is_within(real, get("icloud")) or "/Library/Mobile Documents/" in real


def show(path: Optional[str]) -> str:
    """Compact display form: ~/iCloud/... or ~/..."""
    if not path:
        return "-"
    home = os.path.expanduser("~")
    for icloud in (get("icloud"), _norm(ICLOUD_LINK), _norm(ICLOUD_REAL)):
        if path.startswith(icloud + os.sep) or path == icloud:
            return "~/iCloud" + path[len(icloud):]
    return "~" + path[len(home):] if path.startswith(home) else path


def check_destination(dest_file: str, kind: str = "document") -> str:
    """Validate where a file is about to be written or moved. Returns the absolute path.

    kind: "document" (PDF or its sidecar), "report" (generated HTML) or "database".
    Rules:
      * nothing is ever written inside the git repository;
      * documents and reports never land loose in a container folder
        (iCloud root, Finance, Statements, Payslips, Shares): they need a named sub-folder;
      * databases never live in iCloud (sync can corrupt SQLite).
    """
    dest = _norm(dest_file)
    parent = os.path.dirname(dest)
    if is_within(dest, REPO_DIR):
        raise UnsafeLocation(f"refusing to write {kind} inside the repository: {show(dest)}")
    if kind == "database":
        if in_icloud(dest) and os.environ.get("PDF_AUDIT_ALLOW_ICLOUD_DB") != "1":
            raise UnsafeLocation(
                f"refusing to keep a database in iCloud ({show(dest)}): sync can corrupt SQLite. "
                'Set "directories": {"data": ...} in config.json to a local folder.'
            )
        return dest
    for key in CONTAINER_KEYS:
        if os.path.realpath(parent) == os.path.realpath(get(key)):
            raise UnsafeLocation(
                f"refusing to place a {kind} loose in {show(get(key))}/ "
                f"({os.path.basename(dest)}): it must go in a named sub-folder"
            )
    return dest

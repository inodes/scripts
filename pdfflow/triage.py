"""Triage engine: decide where a PDF that no section handler filed should wait.

Layout (all under the "pdf" location, see finance/statements/paths.py; names are configurable):

    PDF/unsorted/                 every PDF lands here first
    PDF/<type>/                   recognised kind of document, no dedicated handler yet
    PDF/unknown_type/             readable, but no type recognised
    PDF/needs_review/<reason>/    a handler recognised it but it failed a check
                                  (no_date, unreconciled, format_changed) or it is
                                  unreadable (scanned: needs OCR) or an exact duplicate

Files keep their original names (a clash with different content gets _NN). Nothing is ever
deleted or overwritten, every move is appended to a local manifest, and everything can be
re-run: drop files from unknown_type / needs_review back into unsorted (or use `sweep --resort`).
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time
from typing import Any, Dict, Iterable, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "finance"))

from statements import paths  # noqa: E402
from statements.pdf import PDFTextExtractor  # noqa: E402

from .rules import DEFAULT_REASONS, DEFAULT_TYPES, NAME_RE, RESERVED  # noqa: E402

MIN_TEXT_CHARS = 40
PROBE_PAGES = 3
PROTECTED = ("Duplicates", "Archive", ".Trash")


# ---- config ---------------------------------------------------------------------------------

def load_config() -> Dict[str, Any]:
    try:
        with open(paths.config_path(), encoding="utf-8") as f:
            return (json.load(f) or {}).get("triage") or {}
    except (OSError, ValueError):
        return {}


def type_rules(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Config rules first, then defaults (a config rule of the same name replaces the default)."""
    cfg = load_config() if cfg is None else cfg
    custom = [r for r in (cfg.get("types") or []) if isinstance(r, dict) and r.get("name")]
    names = {r["name"] for r in custom}
    rules = [r for r in custom if not r.get("disabled")]
    rules += [r for r in DEFAULT_TYPES if r["name"] not in names]
    for r in rules:
        n = r["name"]
        if not NAME_RE.match(n) or n in RESERVED:
            raise ValueError(f"triage type name {n!r} must be lower_snake_case and not reserved")
    return rules


def reason_folder(reason: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = load_config() if cfg is None else cfg
    mapping = dict(DEFAULT_REASONS)
    mapping.update(cfg.get("reasons") or {})
    name = mapping.get(reason, reason)
    name = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    return name or "other"


# ---- locations ------------------------------------------------------------------------------

def unsorted_dir() -> str:
    return paths.get("unsorted")


def unknown_dir() -> str:
    return paths.get("unknown_type")


def review_dir(reason: Optional[str] = None) -> str:
    base = paths.get("needs_review")
    return os.path.join(base, reason) if reason else base


def type_dir(name: str) -> str:
    return os.path.join(paths.get("pdf"), name)


def _triage_roots(cfg) -> List[str]:
    roots = [unsorted_dir(), unknown_dir(), review_dir()]
    roots += [type_dir(r["name"]) for r in type_rules(cfg)]
    return roots


# ---- reading & classifying --------------------------------------------------------------------

def read_text(path: str) -> str:
    try:
        return PDFTextExtractor.extract_text(path, max_pages=PROBE_PAGES) or ""
    except Exception:
        return ""


def classify(text: str, filename: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """First matching rule's folder name, or None."""
    for r in type_rules(cfg):
        rx_t, rx_f = r.get("text"), r.get("filename")
        if rx_t and text:
            hits = {(m.group(0).lower()) for m in re.finditer(rx_t, text, re.I)}
            if len(hits) >= int(r.get("min_hits", 1)):
                return r["name"]
        if rx_f and re.search(rx_f, filename, re.I):
            return r["name"]
    return None


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_duplicate(path: str, cfg) -> Optional[str]:
    """An identical file (byte-for-byte) already waiting in a triage folder, if any."""
    size = os.path.getsize(path)
    me = os.path.realpath(path)
    digest = None
    for root in _triage_roots(cfg):
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d not in PROTECTED]
            for n in fn:
                p = os.path.join(dp, n)
                if os.path.realpath(p) == me or not n.lower().endswith(".pdf"):
                    continue
                # Two identical files waiting side by side: only the later-named one is the duplicate,
                # otherwise each would call the other the original and both would be held.
                if os.path.dirname(os.path.realpath(p)) == os.path.dirname(me) and n >= os.path.basename(me):
                    continue
                try:
                    if os.path.getsize(p) != size:
                        continue
                    digest = digest or sha256(path)
                    if sha256(p) == digest:
                        return p
                except OSError:
                    continue
    return None


def _free_name(dest_dir: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    cand, n = os.path.join(dest_dir, name), 0
    while os.path.exists(cand):
        n += 1
        cand = os.path.join(dest_dir, f"{base}_{n:02d}{ext}")
    return cand


# ---- placement --------------------------------------------------------------------------------

def decide(path: str, held_reason: Optional[str] = None, cfg=None) -> Dict[str, Any]:
    """Pure decision (no moves): {"dir", "bucket", "type", "reason", "detail"}."""
    cfg = load_config() if cfg is None else cfg
    name = os.path.basename(path)
    if held_reason:
        r = reason_folder(held_reason, cfg)
        return {"dir": review_dir(r), "bucket": "needs_review", "reason": r, "type": None}
    text = read_text(path)
    if len(text.strip()) < MIN_TEXT_CHARS:
        return {"dir": review_dir("unreadable"), "bucket": "needs_review", "reason": "unreadable", "type": None}
    dup = find_duplicate(path, cfg)
    if dup:
        return {"dir": review_dir("duplicate"), "bucket": "needs_review", "reason": "duplicate",
                "type": None, "detail": f"identical to {paths.show(dup)}"}
    t = classify(text, name, cfg)
    if t:
        return {"dir": type_dir(t), "bucket": "type", "type": t, "reason": None}
    return {"dir": unknown_dir(), "bucket": "unknown_type", "type": None, "reason": None}


def manifest_path() -> str:
    return os.path.join(paths.get("logs"), "triage_manifest.jsonl")


def place(path: str, held_reason: Optional[str] = None, dry_run: bool = True, cfg=None) -> Dict[str, Any]:
    """Decide and (unless dry_run) move one PDF. Returns a record with an "action" key."""
    cfg = load_config() if cfg is None else cfg
    path = os.path.abspath(path)
    rec: Dict[str, Any] = {"file": os.path.basename(path), "src": path}
    if not paths.is_within(path, paths.get("pdf")):
        rec["action"] = "outside_pdf_root"
        return rec
    if any(f"/{p}/" in path for p in PROTECTED):
        rec["action"] = "protected_folder"
        return rec
    d = decide(path, held_reason, cfg)
    rec.update({k: v for k, v in d.items() if k != "dir"})
    if os.path.realpath(os.path.dirname(path)) == os.path.realpath(d["dir"]):
        rec["action"] = "unchanged"
        rec["dest"] = path
        return rec
    dest = _free_name(d["dir"], os.path.basename(path))
    paths.check_destination(dest)
    rec["dest"] = dest
    if dry_run:
        rec["action"] = "would_move"
        return rec
    os.makedirs(d["dir"], exist_ok=True)
    shutil.move(path, dest)
    rec["action"] = "moved"
    try:
        os.makedirs(os.path.dirname(manifest_path()), exist_ok=True)
        with open(manifest_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "src": paths.show(path), "dest": paths.show(dest),
                "bucket": d["bucket"], "type": d.get("type"), "reason": d.get("reason"),
            }) + "\n")
    except OSError:
        pass
    return rec


def format_record(rec: Dict[str, Any]) -> str:
    where = paths.show(os.path.dirname(rec["dest"])) if rec.get("dest") else "-"
    tag = {"moved": "FILED", "would_move": "WOULD_FILE", "unchanged": "UNCHANGED"}.get(rec["action"], "SKIP")
    bits = [f"[triage] {tag}", f'file="{rec["file"]}"']
    if rec["action"] in ("moved", "would_move", "unchanged"):
        bits.append(f"to={where}")
        if rec.get("type"):
            bits.append(f"type={rec['type']}")
        if rec.get("reason"):
            bits.append(f"reason={rec['reason']}")
        if rec.get("detail"):
            bits.append(f'detail="{rec["detail"]}"')
    else:
        bits.append(f"reason={rec['action']}")
    return " ".join(bits)


# ---- status -----------------------------------------------------------------------------------

def _pdfs(folder: str) -> List[str]:
    out = []
    for dp, dn, fn in os.walk(folder):
        dn[:] = [d for d in dn if d not in PROTECTED and not d.startswith(".")]
        out += [os.path.join(dp, n) for n in fn if n.lower().endswith(".pdf") and not n.startswith(".")]
    return out


def _bucket(label: str, files: List[str], stale_days: Optional[int]) -> Dict[str, Any]:
    oldest, oldest_age = None, 0
    now = time.time()
    for f in files:
        try:
            age = (now - os.stat(f).st_ctime) / 86400
        except OSError:
            continue
        if oldest is None or age > oldest_age:
            oldest, oldest_age = os.path.basename(f), int(age)
    return {"folder": label, "count": len(files), "oldest": oldest, "oldest_days": oldest_age if oldest else None,
            "stale": stale_days is not None and bool(oldest) and oldest_age >= stale_days}


def summary(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Counts and oldest waiting file per triage folder, plus legacy folders not part of triage."""
    cfg = load_config() if cfg is None else cfg
    stale = int(cfg.get("stale_days", 14))
    pdf_root = paths.get("pdf")
    rows: List[Dict[str, Any]] = [_bucket("unsorted", _pdfs(unsorted_dir()), stale)]
    rd = review_dir()
    if os.path.isdir(rd):
        for sub in sorted(os.listdir(rd)):
            if os.path.isdir(os.path.join(rd, sub)) and not sub.startswith("."):
                rows.append(_bucket(f"needs_review/{sub}", _pdfs(os.path.join(rd, sub)), stale))
    rows.append(_bucket("unknown_type", _pdfs(unknown_dir()), None))
    type_names = [r["name"] for r in type_rules(cfg)]
    for n in type_names:
        if os.path.isdir(type_dir(n)):
            rows.append(_bucket(n, _pdfs(type_dir(n)), None))
    known = {"unsorted", "unknown_type", "needs_review", *type_names, *PROTECTED}
    legacy = []
    if os.path.isdir(pdf_root):
        loose = [os.path.join(pdf_root, n) for n in os.listdir(pdf_root) if n.lower().endswith(".pdf")]
        if loose:
            legacy.append(_bucket("(loose in PDF/)", loose, None))
        for n in sorted(os.listdir(pdf_root)):
            p = os.path.join(pdf_root, n)
            if os.path.isdir(p) and n not in known and not n.startswith("."):
                legacy.append(_bucket(n, _pdfs(p), None))
    waiting = sum(r["count"] for r in rows if r["folder"] == "unsorted" or r["folder"].startswith("needs_review"))
    return {"triage": rows, "legacy": [b for b in legacy if b["count"]], "needs_attention": waiting,
            "stale_days": stale}


def print_status(s: Dict[str, Any]) -> None:
    def line(b):
        old = f'{b["oldest_days"]}d  {b["oldest"][:48]}' if b["oldest"] else "-"
        return f'{b["folder"]:34s} {b["count"]:6d}  {old}' + ("  << STALE" if b["stale"] else "")
    print(f'{"folder":34s} {"files":>6s}  oldest waiting')
    for b in s["triage"]:
        print(line(b))
    if s["legacy"]:
        print("\nlegacy folders (not part of triage yet):")
        for b in s["legacy"]:
            print(line(b))
    print(f'\nneeds attention (unsorted + needs_review): {s["needs_attention"]}   stale after {s["stale_days"]} days')


def candidates(resort: bool = False, extra_dirs: Iterable[str] = ()) -> List[str]:
    folders = [unsorted_dir()] + list(extra_dirs)
    if resort:
        folders += [unknown_dir(), review_dir()]
    seen, out = set(), []
    for fo in folders:
        for p in sorted(_pdfs(fo)):
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out

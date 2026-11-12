#!/usr/bin/env python3
"""storage_audit: read-only audit of files scattered across drives.

Subcommands
  scan    walk each configured root into a local SQLite catalogue
  dupes   find duplicate files (size -> partial hash -> optional full hash)
  lrcat   compare a Lightroom catalogue with photos actually on disk
  report  write a self-contained index.html (overview, drill-down, duplicates, Lightroom)

Nothing here modifies, moves or deletes your files. Settings live in an external
iCloud config.json (see config.example.json); the database should live on a local disk,
not in iCloud. Standard library only.
"""
import argparse, hashlib, html, json, os, sqlite3, sys, time
from collections import defaultdict
from datetime import datetime, timezone

KINDS = {
    "photo": "jpg jpeg heic png tif tiff gif webp dng cr2 cr3 nef arw orf rw2 raf psd",
    "video": "mov mp4 m4v avi mkv wmv mts m2ts mpg mpeg 3gp lrv thm",
    "audio": "mp3 m4a aac wav flac aif aiff",
    "document": "pdf doc docx xls xlsx ppt pptx pages numbers key txt rtf csv md",
    "archive": "zip rar 7z gz tar dmg iso",
    "app/system": "app pkg exe dll plist ds_store apdisk db sqlite",
    "lightroom": "lrcat lrdata lrprev lrmprev lrfprev lrcat-data",
}
EXT_KIND = {e: k for k, v in KINDS.items() for e in v.split()}
PHOTO_EXT = set(KINDS["photo"].split())
DEFAULT_EXCLUDE = ["@Recycle", "@Recently-Snapshot", ".Trash", ".Trashes", ".Spotlight-V100",
                   ".fseventsd", ".TemporaryItems", ".timemachine", ".DocumentRevisions-V100",
                   ".MobileBackups", ".PKInstallSandboxManager", ".PKInstallSandboxManager-SystemSoftware",
                   "System Volume Information", "$RECYCLE.BIN"]


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)


def default_config_path():
    """Where config.json lives, with no path spelled out here.

    Order: $STORAGE_AUDIT_CONFIG; then a git-ignored config.json beside this script
    (either full settings, or a pointer {"config_file": "<real config>"}); then the
    iCloud folder that mirrors this repo (<icloud>/<repo name>/<section>/config.json,
    e.g. repos/scripts/storage -> iCloud/scripts/storage).
    The iCloud root comes from finance/statements/paths.py, the one place that defines it.
    """
    env = os.environ.get("STORAGE_AUDIT_CONFIG")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    mirror = os.path.join(os.path.basename(REPO_DIR), os.path.basename(HERE), "config.json")
    roots = []
    try:
        sys.path.insert(0, os.path.join(REPO_DIR, "finance"))
        from statements import paths  # standalone module; single source of the iCloud root
        roots = [paths.ICLOUD, paths.ICLOUD_REAL]
    except ImportError:
        pass
    finally:
        sys.path.pop(0)
    local = os.path.join(HERE, "config.json")
    cands = [local] + [os.path.abspath(os.path.expanduser(os.path.join(r, mirror))) for r in roots]
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[1] if len(cands) > 1 else local


def load_config(path, _via=None):
    path = os.path.expanduser(path)
    if not os.path.exists(path) and _via:
        sys.exit(f"Config not found: {path}\n(pointed to by {_via})")
    if not os.path.exists(path):
        here = os.path.dirname(os.path.abspath(__file__))
        sys.exit(f"Config not found: {path}\nCreate it from the template:\n"
                 f"  mkdir -p \"{os.path.dirname(path)}\" && "
                 f"cp \"{here}/config.example.json\" \"{path}\"\n"
                 "then edit the roots to match your mounted volumes.")
    with open(path) as f:
        cfg = json.load(f)
    if "config_file" in cfg:  # local pointer file -> the real config (one hop only)
        if _via:
            sys.exit(f"{path}: config_file pointers cannot be chained")
        target = os.path.expanduser(cfg["config_file"])
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(path), target)
        return load_config(target, _via=path)
    cfg["db_path"] = os.path.expanduser(cfg.get("db_path", "~/Library/Application Support/storage_audit/catalogue.sqlite"))
    # reports default to a folder beside config.json (iCloud) so no location is baked into code
    cfg["report_dir"] = os.path.expanduser(cfg.get("report_dir") or os.path.join(os.path.dirname(path), "reports"))
    cfg["roots"] = {k: os.path.expanduser(v) for k, v in cfg["roots"].items()}
    cfg.setdefault("exclude_names", DEFAULT_EXCLUDE)
    cfg.setdefault("min_dupe_bytes", 1_000_000)
    return cfg


def db(cfg):
    os.makedirs(os.path.dirname(cfg["db_path"]), exist_ok=True)
    con = sqlite3.connect(cfg["db_path"])
    con.executescript("""
    CREATE TABLE IF NOT EXISTS files(
      root TEXT, rel TEXT, size INTEGER, mtime REAL, ext TEXT, kind TEXT,
      top TEXT, phash TEXT, fhash TEXT, PRIMARY KEY(root, rel));
    CREATE INDEX IF NOT EXISTS i_size ON files(size);
    CREATE INDEX IF NOT EXISTS i_kind ON files(root, kind);
    CREATE TABLE IF NOT EXISTS scans(root TEXT PRIMARY KEY, finished TEXT, nfiles INTEGER, nbytes INTEGER);
    CREATE TABLE IF NOT EXISTS lr(path TEXT PRIMARY KEY, catalogue TEXT);
    """)
    return con


def cmd_scan(cfg, only):
    con = db(cfg)
    skip = set(cfg["exclude_names"])
    for label, root in cfg["roots"].items():
        if only and label not in only:
            continue
        if not os.path.isdir(root):
            print(f"[skip] {label}: {root} not mounted")
            continue
        print(f"[scan] {label} ({root})")
        con.execute("DELETE FROM files WHERE root=?", (label,))
        rows, n, total = [], 0, 0
        unreadable = []
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError as e:
                unreadable.append((d, e.strerror))  # summarised after the scan, not printed one by one
                continue
            with it:
                for e in it:
                    if e.name in skip:
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            rel = os.path.relpath(e.path, root)
                            ext = os.path.splitext(e.name)[1].lower().lstrip(".") or "(none)"
                            top = rel.split(os.sep)[0] if os.sep in rel else "(root)"
                            rows.append((label, rel, st.st_size, st.st_mtime, ext,
                                         EXT_KIND.get(ext, "other"), top, None, None))
                            n += 1; total += st.st_size
                    except OSError:
                        pass
            if len(rows) >= 5000:
                con.executemany("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)", rows)
                rows = []
                print(f"  {n:,} files, {total/1e9:,.1f} GB", end="\r")
        con.executemany("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT OR REPLACE INTO scans VALUES(?,?,?,?)",
                    (label, datetime.now(timezone.utc).strftime("%Y-%m-%d"), n, total))
        con.commit()
        print(f"  done: {n:,} files, {total/1e9:,.1f} GB")
        if unreadable:
            print(f"  {len(unreadable):,} folder(s) could not be read (not counted); first few:")
            for d, why in unreadable[:5]:
                print(f"    {os.path.relpath(d, root)}: {why}")


def partial_hash(path, size, chunk=65536):
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(chunk))
        if size > chunk * 2:
            f.seek(size - chunk)
            h.update(f.read(chunk))
    return h.hexdigest()


def full_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def cmd_dupes(cfg, full, only_kind):
    """Group by size, then confirm by partial hash; --full verifies by SHA-256."""
    con = db(cfg)
    q = "SELECT size FROM files WHERE size>=? GROUP BY size HAVING COUNT(*)>1"
    sizes = [r[0] for r in con.execute(q, (cfg["min_dupe_bytes"],))]
    print(f"{len(sizes):,} sizes shared by 2+ files (>= {cfg['min_dupe_bytes']:,} bytes)")
    for i, s in enumerate(sizes, 1):
        rows = con.execute("SELECT root, rel, kind FROM files WHERE size=?", (s,)).fetchall()
        for root, rel, kind in rows:
            if only_kind and kind != only_kind:
                continue
            p = os.path.join(cfg["roots"][root], rel)
            try:
                ph = partial_hash(p, s)
                fh = full_hash(p) if full else None
            except OSError:
                continue
            con.execute("UPDATE files SET phash=?, fhash=COALESCE(?,fhash) WHERE root=? AND rel=?",
                        (ph, fh, root, rel))
        if i % 50 == 0:
            con.commit(); print(f"  {i}/{len(sizes)}", end="\r")
    con.commit()
    print("hashing complete; run `report` to see duplicate groups")


def cmd_lrcat(cfg, catalogue):
    """Record every file path known to a Lightroom catalogue (opened read-only)."""
    con = db(cfg)
    uri = "file:" + os.path.abspath(os.path.expanduser(catalogue)) + "?mode=ro&immutable=1"
    lr = sqlite3.connect(uri, uri=True)
    q = """SELECT r.absolutePath || f.pathFromRoot || fi.baseName || '.' || fi.extension
           FROM AgLibraryFile fi JOIN AgLibraryFolder f ON fi.folder=f.id_local
           JOIN AgLibraryRootFolder r ON f.rootFolder=r.id_local"""
    name = os.path.basename(catalogue)
    con.execute("DELETE FROM lr WHERE catalogue=?", (name,))
    n = 0
    for (p,) in lr.execute(q):
        con.execute("INSERT OR REPLACE INTO lr VALUES(?,?)", (p, name)); n += 1
    con.commit()
    print(f"{name}: {n:,} files referenced")


def fmt(b):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or u == "TB":
            return f"{b:,.1f} {u}" if u != "B" else f"{b:,} B"
        b /= 1024


def table(headers, rows):
    h = "".join(f"<th>{html.escape(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{h}</tr>{body}</table>"


def cmd_report(cfg):
    """Write index.html (overview, drill-down, duplicates, Lightroom) to report_dir."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import storage_audit_ui
    print("report:", storage_audit_ui.write_index(cfg))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("roots", nargs="*", help="limit to these root labels")
    d = sub.add_parser("dupes"); d.add_argument("--full", action="store_true"); d.add_argument("--kind")
    l = sub.add_parser("lrcat"); l.add_argument("catalogue")
    sub.add_parser("report")
    a = ap.parse_args()
    cfg = load_config(a.config or default_config_path())
    {"scan": lambda: cmd_scan(cfg, set(a.roots)), "dupes": lambda: cmd_dupes(cfg, a.full, a.kind),
     "lrcat": lambda: cmd_lrcat(cfg, a.catalogue), "report": lambda: cmd_report(cfg)}[a.cmd]()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""storage_audit: read-only audit of files scattered across drives.

Subcommands
  scan    walk each configured root into a local SQLite catalogue
  dupes   find duplicate files (size -> partial hash -> optional full hash)
  lrcat   compare a Lightroom catalogue with photos actually on disk
  report  write a self-contained HTML report (sizes, kinds, folders, duplicates)

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
DEFAULT_EXCLUDE = ["@Recycle", "@Recently-Snapshot", ".Trash", ".Spotlight-V100",
                   ".fseventsd", ".TemporaryItems", ".timemachine"]


def load_config(path):
    with open(os.path.expanduser(path)) as f:
        cfg = json.load(f)
    cfg["db_path"] = os.path.expanduser(cfg["db_path"])
    cfg["report_dir"] = os.path.expanduser(cfg["report_dir"])
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
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError as e:
                print(f"  ! {d}: {e.strerror}")
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
    con = db(cfg)
    os.makedirs(cfg["report_dir"], exist_ok=True)
    out = []
    out.append("<h2>Volumes</h2>" + table(["Root", "Files", "Size", "Scanned"],
        [(r, f"{n:,}", fmt(b), d) for r, d, n, b in con.execute(
            "SELECT root, finished, nfiles, nbytes FROM scans ORDER BY nbytes DESC")]))
    out.append("<h2>What is on each root (by kind)</h2>" + table(["Root", "Kind", "Files", "Size"],
        [(r, k, f"{n:,}", fmt(b)) for r, k, n, b in con.execute(
            "SELECT root, kind, COUNT(*), SUM(size) FROM files GROUP BY root, kind ORDER BY root, SUM(size) DESC")]))
    out.append("<h2>Largest top-level folders</h2>" + table(["Root", "Folder", "Files", "Size", "Newest file"],
        [(r, t, f"{n:,}", fmt(b), datetime.fromtimestamp(m).strftime("%Y-%m-%d")) for r, t, n, b, m in con.execute(
            "SELECT root, top, COUNT(*), SUM(size), MAX(mtime) FROM files GROUP BY root, top ORDER BY SUM(size) DESC LIMIT 60")]))
    out.append("<h2>Stale folders (nothing modified in 3+ years, 1 GB+)</h2>" + table(["Root", "Folder", "Size", "Newest file"],
        [(r, t, fmt(b), datetime.fromtimestamp(m).strftime("%Y-%m-%d")) for r, t, b, m in con.execute(
            "SELECT root, top, SUM(size), MAX(mtime) FROM files GROUP BY root, top "
            "HAVING SUM(size)>1e9 AND MAX(mtime)<strftime('%s','now','-3 years') ORDER BY SUM(size) DESC LIMIT 40")]))
    key = "COALESCE(fhash, phash)"
    groups = con.execute(f"""SELECT {key} k, size, COUNT(*) c, COUNT(DISTINCT root) vols FROM files
        WHERE {key} IS NOT NULL GROUP BY k, size HAVING c>1 ORDER BY (c-1)*size DESC""").fetchall()
    waste = sum((c - 1) * s for _, s, c, _ in groups)
    verified = "SHA-256 verified" if con.execute("SELECT 1 FROM files WHERE fhash IS NOT NULL LIMIT 1").fetchone() else "partial-hash match (run dupes --full to confirm)"
    out.append(f"<h2>Duplicates</h2><p>{len(groups):,} groups, {fmt(waste)} reclaimable ({verified}). "
               f"Top 200 shown; 'Volumes' &gt; 1 means copies on different drives.</p>")
    rows = []
    for k, s, c, vols in groups[:200]:
        locs = con.execute(f"SELECT root, rel FROM files WHERE {key}=? AND size=? LIMIT 4", (k, s)).fetchall()
        rows.append((fmt(s), c, vols, " | ".join(f"{r}:{p}" for r, p in locs)))
    out.append(table(["Size", "Copies", "Volumes", "Locations (first 4)"], rows))
    if con.execute("SELECT 1 FROM lr LIMIT 1").fetchone():
        # map catalogue paths to roots by prefix, then list on-disk photos not catalogued
        lrset = {p for (p,) in con.execute("SELECT path FROM lr")}
        miss = []
        for root, rel, size in con.execute("SELECT root, rel, size FROM files WHERE kind='photo' AND ext!='png'"):
            if os.path.join(cfg["roots"][root], rel) not in lrset:
                miss.append((root, rel, size))
        by_top = defaultdict(lambda: [0, 0])
        for root, rel, size in miss:
            t = (root, rel.split(os.sep)[0]); by_top[t][0] += 1; by_top[t][1] += size
        out.append(f"<h2>Photos on disk but not in any scanned Lightroom catalogue</h2><p>{len(miss):,} files.</p>" +
                   table(["Root", "Folder", "Files", "Size"],
                         [(r, t, f"{v[0]:,}", fmt(v[1])) for (r, t), v in sorted(by_top.items(), key=lambda x: -x[1][1])[:60]]))
    page = ("<!doctype html><meta charset=utf-8><title>Storage audit</title>"
            "<style>body{font:14px system-ui;margin:2rem;max-width:1100px}table{border-collapse:collapse;margin:1rem 0;width:100%}"
            "td,th{border-bottom:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}th{background:#f4f4f4}</style>"
            f"<h1>Storage audit</h1><p>Generated {datetime.now().strftime('%Y-%m-%d')}</p>" + "".join(out))
    path = os.path.join(cfg["report_dir"], "storage-audit.html")
    with open(path, "w") as f:
        f.write(page)
    print("report:", path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="~/Library/Mobile Documents/com~apple~CloudDocs/Storage/config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("roots", nargs="*", help="limit to these root labels")
    d = sub.add_parser("dupes"); d.add_argument("--full", action="store_true"); d.add_argument("--kind")
    l = sub.add_parser("lrcat"); l.add_argument("catalogue")
    sub.add_parser("report")
    a = ap.parse_args()
    cfg = load_config(a.config)
    {"scan": lambda: cmd_scan(cfg, set(a.roots)), "dupes": lambda: cmd_dupes(cfg, a.full, a.kind),
     "lrcat": lambda: cmd_lrcat(cfg, a.catalogue), "report": lambda: cmd_report(cfg)}[a.cmd]()


if __name__ == "__main__":
    main()

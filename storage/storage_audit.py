#!/usr/bin/env python3
"""storage_audit: read-only audit of files scattered across drives.

Subcommands
  scan    walk each configured root into a local SQLite catalogue
  dupes   find duplicate files (size -> partial hash -> optional full hash)
  lrcat   read Lightroom catalogues (--all: every one found) to compare with photos on disk
  aperture read Aperture libraries (projects, folders, masters; works although Aperture is discontinued)
  index   (re)build the folder-totals index that makes browsing instant (scan does this itself)
  report  write a self-contained index.html (overview, drill-down, duplicates, Lightroom)

Nothing here modifies, moves or deletes your files. Settings live in an external
iCloud config.json (see config.example.json); the database should live on a local disk,
not in iCloud. Standard library only.
"""
import argparse, hashlib, html, json, os, sqlite3, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import quote

# Progress lines for the UI. Only emitted when the UI starts a job (STORAGE_AUDIT_PROGRESS=1), so the
# command line stays quiet. Format: "@@PROGRESS {json}" on its own line.
_PROG = {"on": os.environ.get("STORAGE_AUDIT_PROGRESS") == "1", "t": 0.0, "step": None, "steps": None}


def set_step(i, n):
    _PROG["step"], _PROG["steps"] = i, n


def progress(label, done=0, total=0, detail="", approx=False, force=False):
    if not _PROG["on"]:
        return
    now = time.monotonic()
    if not force and now - _PROG["t"] < 0.4:
        return
    _PROG["t"] = now
    print("@@PROGRESS " + json.dumps({"label": label, "done": done, "total": total, "detail": detail[-90:],
                                      "approx": approx, "step": _PROG["step"], "steps": _PROG["steps"]}), flush=True)

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
    # WAL: readers (the UI) and the writer (a scan) no longer block each other; wait up to a minute for a lock
    con = sqlite3.connect(cfg["db_path"], timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS files(
      root TEXT, rel TEXT, size INTEGER, mtime REAL, ext TEXT, kind TEXT,
      top TEXT, phash TEXT, fhash TEXT, PRIMARY KEY(root, rel));
    CREATE INDEX IF NOT EXISTS i_size ON files(size);
    CREATE INDEX IF NOT EXISTS i_kind ON files(root, kind);
    CREATE TABLE IF NOT EXISTS scans(root TEXT PRIMARY KEY, finished TEXT, nfiles INTEGER, nbytes INTEGER);
    CREATE TABLE IF NOT EXISTS lr(path TEXT PRIMARY KEY, catalogue TEXT);  -- legacy, superseded by lrfiles
    -- one row per (catalogue, photo path) so several catalogues can reference the same photo
    CREATE TABLE IF NOT EXISTS lrfiles(catalogue TEXT, path TEXT, name_lc TEXT, size INTEGER,
      PRIMARY KEY(catalogue, path)) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS i_lrname ON lrfiles(name_lc, size);
    -- Aperture libraries (read-only copy of what Library.apdb says)
    CREATE TABLE IF NOT EXISTS aplibs(lib TEXT PRIMARY KEY, root TEXT, rel TEXT, masters INTEGER, managed INTEGER,
      referenced INTEGER, absent INTEGER, trashed INTEGER, min_date REAL, max_date REAL, loaded TEXT);
    CREATE TABLE IF NOT EXISTS apfolders(lib TEXT, uuid TEXT, parent TEXT, name TEXT, ftype INTEGER, versions INTEGER,
      min_date REAL, max_date REAL, PRIMARY KEY(lib, uuid)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS apmasters(lib TEXT, uuid TEXT, project TEXT, name TEXT, orig TEXT, name_lc TEXT,
      orig_lc TEXT, path TEXT, size INTEGER, date REAL, is_ref INTEGER, missing INTEGER, trashed INTEGER,
      PRIMARY KEY(lib, uuid)) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS i_apm_proj ON apmasters(lib, project);
    CREATE TABLE IF NOT EXISTS lrcats(catalogue TEXT PRIMARY KEY, files INTEGER, in_scanned INTEGER,
      missing INTEGER, loaded TEXT);
    -- browse index: folder totals precomputed once so drill-down is a lookup, not an aggregate
    CREATE TABLE IF NOT EXISTS dirs(root TEXT, parent TEXT, name TEXT, files INTEGER, bytes INTEGER,
      newest REAL, mix TEXT, PRIMARY KEY(root, parent, name)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS kinds(root TEXT, kind TEXT, files INTEGER, bytes INTEGER, PRIMARY KEY(root, kind));
    CREATE TABLE IF NOT EXISTS finds(root TEXT, kind TEXT, rel TEXT, size INTEGER, mtime REAL,
      PRIMARY KEY(root, kind, rel)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS dirs_built(root TEXT PRIMARY KEY, nfiles INTEGER, nbytes INTEGER, built TEXT);
    """)
    return con


def cmd_scan(cfg, only):
    con = db(cfg)
    skip = set(cfg["exclude_names"])
    todo = [l for l, r in cfg["roots"].items() if (not only or l in only) and os.path.isdir(r)]
    for label, root in cfg["roots"].items():
        if only and label not in only:
            continue
        if not os.path.isdir(root):
            print(f"[skip] {label}: {root} not mounted")
            continue
        print(f"[scan] {label} ({root})")
        set_step(todo.index(label) + 1, len(todo))
        prev = con.execute("SELECT nfiles FROM scans WHERE root=?", (label,)).fetchone()
        expect = prev[0] if prev else 0  # last scan's file count, so the bar can show an estimate
        # an interrupted scan must not look finished: drop the old summary row, keep nothing half-counted
        con.execute("DELETE FROM scans WHERE root=?", (label,))
        con.execute("DELETE FROM files WHERE root=?", (label,))
        con.commit()
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
            progress(f"Scanning {label}", n, expect, os.path.relpath(d, root), approx=True)
            if len(rows) >= 5000:
                con.executemany("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)", rows)
                con.commit()  # progress survives a crash; the root only counts as scanned once `scans` is written
                rows = []
                print(f"  {n:,} files, {total/1e9:,.1f} GB", end="\r")
        con.executemany("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT OR REPLACE INTO scans VALUES(?,?,?,?)",
                    (label, datetime.now(timezone.utc).strftime("%Y-%m-%d"), n, total))
        con.commit()
        print(f"  done: {n:,} files, {total/1e9:,.1f} GB")
        build_index(con, label)
        if unreadable:
            print(f"  {len(unreadable):,} folder(s) could not be read (not counted); first few:")
            for d, why in unreadable[:5]:
                print(f"    {os.path.relpath(d, root)}: {why}")


def build_index(con, root):
    """Precompute folder totals (with a per-file-type mix) and per-type totals for one scanned root."""
    print(f"[index] {root}")
    # lets "files directly inside folder X" be an index lookup (dirname = rel minus its last component)
    con.execute("CREATE INDEX IF NOT EXISTS i_dirpart ON files(root, rtrim(rel, replace(rel, '/', '')))")
    direct, kinds = {}, defaultdict(lambda: [0, 0])
    nfiles = nbytes = 0
    expect = (con.execute("SELECT nfiles FROM scans WHERE root=?", (root,)).fetchone() or [0])[0]
    finds = []
    for rel, size, mtime, kind, ext in con.execute("SELECT rel, size, mtime, kind, ext FROM files WHERE root=?", (root,)):
        if ext == "lrcat":
            finds.append((root, "lrcat", rel, size, mtime))
        elif rel.endswith("Database/apdb/Library.apdb"):
            finds.append((root, "aperture", rel[:-len("/Database/apdb/Library.apdb")], size, mtime))
        d = rel.rpartition("/")[0]
        a = direct.get(d)
        if a is None:
            a = direct[d] = [0, 0, 0.0, {}]
        a[0] += 1; a[1] += size
        if mtime > a[2]:
            a[2] = mtime
        km = a[3].get(kind)
        if km is None:
            a[3][kind] = [1, size]
        else:
            km[0] += 1; km[1] += size
        k = kinds[kind]; k[0] += 1; k[1] += size
        nfiles += 1; nbytes += size
        if nfiles % 20000 == 0:
            progress(f"Indexing {root}", nfiles, expect, "reading file list")
    progress(f"Indexing {root}", 0, 0, "rolling folder totals up", force=True)
    # roll folder totals (and their type mix) up to parents, deepest first
    tot = {d: v for d, v in direct.items() if d}
    levels = defaultdict(set)
    for d in tot:
        levels[d.count("/") + 1].add(d)
    for depth in range(max(levels, default=0), 1, -1):
        for d in levels[depth]:
            n, b, m, mix = tot[d]
            parent = d.rpartition("/")[0]
            p = tot.get(parent)
            if p is None:
                p = tot[parent] = [0, 0, 0.0, {}]; levels[depth - 1].add(parent)
            p[0] += n; p[1] += b
            if m > p[2]:
                p[2] = m
            for k, (kn, kb) in mix.items():
                pk = p[3].get(k)
                if pk is None:
                    p[3][k] = [kn, kb]
                else:
                    pk[0] += kn; pk[1] += kb
    # files sitting directly in the root contribute to no folder row; everything else is covered
    rows = [(root, *d.rpartition("/")[::2], v[0], v[1], v[2], json.dumps(v[3], separators=(",", ":")))
            for d, v in tot.items()]
    progress(f"Indexing {root}", 0, 0, f"writing {len(rows):,} folders", force=True)
    con.execute("DELETE FROM dirs WHERE root=?", (root,))
    con.execute("DELETE FROM kinds WHERE root=?", (root,))
    con.execute("DELETE FROM finds WHERE root=?", (root,))
    con.executemany("INSERT OR REPLACE INTO finds VALUES(?,?,?,?,?)", finds)
    con.executemany("INSERT INTO dirs VALUES(?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO kinds VALUES(?,?,?,?)", [(root, k, v[0], v[1]) for k, v in kinds.items()])
    con.execute("INSERT OR REPLACE INTO dirs_built VALUES(?,?,?,?)",
                (root, nfiles, nbytes, datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    con.commit()
    print(f"  indexed {len(rows):,} folders")


def cmd_index(cfg, only):
    con = db(cfg)
    todo = [r for (r,) in con.execute("SELECT root FROM scans ORDER BY root").fetchall() if not only or r in only]
    for i, root in enumerate(todo, 1):
        set_step(i, len(todo))
        build_index(con, root)


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
        progress("Finding duplicates (" + ("full SHA-256" if full else "partial hash") + ")", i, len(sizes), f"files of {s:,} bytes")
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


def is_backup(rel):
    return "backup" in rel.lower()


def found_catalogues(con, cfg):
    """Every .lrcat seen by the scans: [(full path, root, rel, size, mtime)]."""
    out = []
    for root, rel, size, mtime in con.execute("SELECT root, rel, size, mtime FROM files WHERE ext='lrcat'"):
        if root in cfg["roots"]:
            out.append((os.path.join(cfg["roots"][root], rel), root, rel, size, mtime))
    return out


def cmd_lrcat(cfg, catalogues, read_all=False):
    """Record every photo path a Lightroom catalogue references (catalogues are opened read-only)."""
    con = db(cfg)
    paths = [os.path.abspath(os.path.expanduser(c)) for c in catalogues]
    if read_all:
        paths += [p for p, _, rel, _, _ in found_catalogues(con, cfg) if not is_backup(rel)]
    if not paths:
        sys.exit("No catalogues given (pass .lrcat paths, or --all after a scan).")
    roots = [(os.path.join(os.path.expanduser(p), ""), label) for label, p in cfg["roots"].items()]
    q = """SELECT r.absolutePath || f.pathFromRoot || fi.baseName || '.' || fi.extension
           FROM AgLibraryFile fi JOIN AgLibraryFolder f ON fi.folder=f.id_local
           JOIN AgLibraryRootFolder r ON f.rootFolder=r.id_local"""
    cats = list(dict.fromkeys(paths))
    for ci, cat in enumerate(cats, 1):
        set_step(ci, len(cats))
        progress("Reading Lightroom catalogue", 0, 0, os.path.basename(cat), force=True)
        if not (cat.endswith(".lrcat") and os.path.isfile(cat)):
            print(f"[skip] {cat}: not a readable .lrcat file")
            continue
        print(f"[lrcat] {cat}")
        try:
            lr = sqlite3.connect("file:" + quote(cat) + "?mode=ro&immutable=1", uri=True)
            photo_paths = [p for (p,) in lr.execute(q) if p]
            lr.close()
        except sqlite3.DatabaseError as e:
            print(f"  ! could not read: {e}")
            continue
        con.execute("DELETE FROM lrfiles WHERE catalogue=?", (cat,))
        in_scanned = missing = 0
        rows = []
        for pi, p in enumerate(photo_paths):  # how many sit under a scanned root, and how many of those are gone from disk
            size = None
            if pi % 2000 == 0:
                progress("Matching catalogue photos to scanned files", pi, len(photo_paths), os.path.basename(cat))
            for prefix, label in roots:
                if p.startswith(prefix):
                    in_scanned += 1
                    hit = con.execute("SELECT size FROM files WHERE root=? AND rel=?", (label, p[len(prefix):])).fetchone()
                    if hit:
                        size = hit[0]
                    else:
                        missing += 1
                    break
            rows.append((cat, p, os.path.basename(p).lower(), size))
        con.executemany("INSERT OR IGNORE INTO lrfiles VALUES(?,?,?,?)", rows)
        con.execute("INSERT OR REPLACE INTO lrcats VALUES(?,?,?,?,?)",
                    (cat, len(photo_paths), in_scanned, missing, datetime.now(timezone.utc).strftime("%Y-%m-%d")))
        con.commit()
        print(f"  {len(photo_paths):,} photos referenced; {in_scanned:,} on scanned drives, {missing:,} of those missing from disk")


APERTURE_EPOCH = 978_307_200  # Aperture dates count seconds from 2001-01-01


def found_aperture(con, cfg):
    """Every Aperture library the scans saw, identified by its Library.apdb: [(full path, root, rel)]."""
    tail = "/Database/apdb/Library.apdb"
    out = []
    for root, rel in con.execute("SELECT root, rel FROM files WHERE rel LIKE ?", ("%" + tail,)):
        if root in cfg["roots"] and rel.endswith(tail):
            lib_rel = rel[:-len(tail)]
            out.append((os.path.join(cfg["roots"][root], lib_rel), root, lib_rel))
    return out


def cmd_aperture(cfg, libraries, read_all=False):
    """Read Aperture libraries (read-only): projects, folders and every master photo record."""
    con = db(cfg)
    wanted = {}
    for p in libraries:
        wanted[os.path.abspath(os.path.expanduser(p))] = (None, None)
    if read_all or not libraries:
        for full, root, lib_rel in found_aperture(con, cfg):
            wanted[full] = (root, lib_rel)
    if not wanted:
        sys.exit("No Aperture libraries found (scan first, or pass .aplibrary paths).")
    roots = [(os.path.join(os.path.expanduser(p), ""), label) for label, p in cfg["roots"].items()]
    for li, (lib, (root, lib_rel)) in enumerate(wanted.items(), 1):
        set_step(li, len(wanted))
        progress("Reading Aperture library", 0, 0, os.path.basename(lib), force=True)
        apdb = os.path.join(lib, "Database", "apdb", "Library.apdb")
        if not os.path.isfile(apdb):
            print(f"[skip] {lib}: no Database/apdb/Library.apdb")
            continue
        if root is None:  # given as a path: work out which scanned root it sits under, if any
            for prefix, label in roots:
                if (lib + "/").startswith(prefix):
                    root, lib_rel = label, (lib + "/")[len(prefix):].rstrip("/")
                    break
        print(f"[aperture] {lib}")
        try:
            ap = sqlite3.connect("file:" + quote(apdb) + "?mode=ro&immutable=1", uri=True)
            masters = ap.execute("SELECT uuid, projectUuid, fileName, originalFileName, imagePath, fileSize, imageDate,"
                                 " fileIsReference, isMissing, isInTrash FROM RKMaster").fetchall()
            folders = ap.execute("SELECT uuid, parentFolderUuid, name, folderType, versionCount, minImageDate, maxImageDate"
                                 " FROM RKFolder WHERE isInTrash=0").fetchall()
            ap.close()
        except sqlite3.DatabaseError as e:
            print(f"  ! could not read: {e}")
            continue
        con.execute("DELETE FROM apmasters WHERE lib=?", (lib,))
        con.execute("DELETE FROM apfolders WHERE lib=?", (lib,))
        absent = managed = referenced = trashed = 0
        rows, dates = [], []
        for mi, (uuid, proj, fname, oname, ipath, fsize, idate, is_ref, missing, intrash) in enumerate(masters):
            if mi % 2000 == 0:
                progress("Checking Aperture masters on disk", mi, len(masters), os.path.basename(lib))
            date = (idate + APERTURE_EPOCH) if isinstance(idate, (int, float)) else None
            if date:
                dates.append(date)
            if is_ref:
                referenced += 1
            else:
                managed += 1
                if root is not None and ipath and not con.execute(
                        "SELECT 1 FROM files WHERE root=? AND rel=?", (root, f"{lib_rel}/Masters/{ipath}")).fetchone():
                    absent += 1
            trashed += 1 if intrash else 0
            rows.append((lib, uuid, proj, fname, oname, (fname or "").lower(), (oname or fname or "").lower(),
                         ipath, fsize, date, is_ref, missing, intrash))
        con.executemany("INSERT OR REPLACE INTO apmasters VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT OR REPLACE INTO apfolders VALUES(?,?,?,?,?,?,?,?)",
                        [(lib, u, par, name, ft, vc, (mn + APERTURE_EPOCH) if mn else None,
                          (mx + APERTURE_EPOCH) if mx else None) for u, par, name, ft, vc, mn, mx in folders])
        con.execute("INSERT OR REPLACE INTO aplibs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (lib, root, lib_rel, len(masters), managed, referenced, absent, trashed,
                     min(dates) if dates else None, max(dates) if dates else None,
                     datetime.now(timezone.utc).strftime("%Y-%m-%d")))
        con.commit()
        print(f"  {len(masters):,} masters ({managed:,} managed, {referenced:,} referenced); "
              f"{absent:,} managed masters not found on scanned drives; {len(folders):,} folders/projects")


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
    l = sub.add_parser("lrcat", help="read Lightroom catalogue(s) read-only"); l.add_argument("catalogues", nargs="*")
    l.add_argument("--all", action="store_true", help="also read every non-backup .lrcat found by the scans")
    ap_ = sub.add_parser("aperture", help="read Aperture libraries (.aplibrary) read-only")
    ap_.add_argument("libraries", nargs="*", help="library paths; none = every library found by the scans")
    ap_.add_argument("--all", action="store_true")
    sub.add_parser("report")
    ix = sub.add_parser("index", help="(re)build the browse index for scanned roots"); ix.add_argument("roots", nargs="*")
    a = ap.parse_args()
    cfg = load_config(a.config or default_config_path())
    try:
        _dispatch(a, cfg)
    except sqlite3.OperationalError as e:
        if "locked" not in str(e):
            raise
        sys.exit("The catalogue is locked by another process (often the UI, or a second scan).\n"
                 f"See what has it open:  lsof \"{cfg['db_path']}\"\nStop it, then run this again.")


def _dispatch(a, cfg):
    {"scan": lambda: cmd_scan(cfg, set(a.roots)), "dupes": lambda: cmd_dupes(cfg, a.full, a.kind),
     "lrcat": lambda: cmd_lrcat(cfg, a.catalogues, a.all), "report": lambda: cmd_report(cfg),
     "index": lambda: cmd_index(cfg, set(a.roots)),
     "aperture": lambda: cmd_aperture(cfg, a.libraries, a.all)}[a.cmd]()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""storage_audit_ui: visual front end for storage_audit.py (local web app).

Starts a small server on 127.0.0.1 and opens your browser. It reads the same
SQLite catalogue and config.json as the CLI and can run scan / dupes / lrcat
from buttons. Read-only: it never moves, renames or deletes your files. The same page can be written
as a static index.html (`storage_audit.py report`).

    storage/storage_audit_ui.py [--config PATH] [--port 8765] [--no-browser]

If the port is busy it tries the next 20 (unless --port is given) and prints the URL.

Standard library only.
"""
import argparse, errno, json, os, sqlite3, subprocess, sys, threading, webbrowser
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import storage_audit as sa  # noqa: E402

CFG = None
CONFIG_PATH = None
JOB = {"running": False, "title": "", "log": [], "code": None, "progress": None}
JOB_LOCK = threading.Lock()


def query(sql, args=(), optional=False):
    """Read-only query (never creates tables, so it cannot collide with a running scan).
    optional=True returns [] when a table does not exist yet (older catalogue, index not built)."""
    path = CFG["db_path"]
    if not os.path.exists(path):
        return []
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        if optional and "no such table" in str(e):
            return []
        raise
    finally:
        con.close()


def day(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""


def is_indexed(root):
    """True when the browse index exists and matches the latest scan of this root."""
    r = query("SELECT d.nfiles, d.nbytes, s.nfiles, s.nbytes FROM dirs_built d JOIN scans s ON s.root=d.root "
              "WHERE d.root=?", (root,), optional=True)
    return bool(r) and r[0][0] == r[0][2] and r[0][1] == r[0][3]


def api_summary():
    scans = query("SELECT root, finished, nfiles, nbytes FROM scans ORDER BY nbytes DESC", optional=True)
    indexed = {r: is_indexed(r) for r, *_ in scans}
    by_root = defaultdict(list)
    for r, k, n, b in query("SELECT root, kind, files, bytes FROM kinds", optional=True):
        if indexed.get(r):
            by_root[r].append({"kind": k, "files": n, "bytes": b})
    slow = [r for r in indexed if not indexed[r]]  # not indexed yet: aggregate the slow way
    for r in slow:
        for k, n, b in query("SELECT kind, COUNT(*), SUM(size) FROM files WHERE root=? GROUP BY kind", (r,)):
            by_root[r].append({"kind": k, "files": n, "bytes": b})
    for v in by_root.values():
        v.sort(key=lambda x: -x["bytes"])
    unscanned = [r for r in CFG["roots"] if r not in indexed]
    stored = {r: (t, f, u, d) for r, t, f, u, d in query("SELECT root, total, free, used, seen FROM space", optional=True)}
    space, groups = {}, {}

    def sig(total, free):  # network shares on one NAS volume are separate mounts but report identical capacity
        return (total, round(free / 1e9))

    for r in list(indexed) + unscanned:
        live = sa.space_of(CFG["roots"][r]) if r in CFG["roots"] and os.path.isdir(CFG["roots"][r]) else None
        if live:
            space[r] = {"total": live["total"], "free": live["free"], "used": live["used"], "live": True}
            g = groups.setdefault(sig(live["total"], live["free"]), {"sp": space[r], "roots": [], "mounts": []})
            g["roots"].append(r)
        elif r in stored:
            t, f, u, d = stored[r]
            space[r] = {"total": t, "free": f, "used": u, "live": False, "seen": d}
            g = groups.setdefault(("old", t, u), {"sp": space[r], "roots": [], "mounts": []})
            g["roots"].append(r)
    # every mounted drive, configured or not, so you can see where a move could go
    extra = [("Macintosh HD (internal)", "/")]
    try:
        extra += [(n, os.path.join("/Volumes", n)) for n in sorted(os.listdir("/Volumes"))
                  if not os.path.islink(os.path.join("/Volumes", n)) and os.path.isdir(os.path.join("/Volumes", n))]
    except OSError:
        pass
    for name, path in extra:
        live = sa.space_of(path)
        if live:
            g = groups.setdefault(sig(live["total"], live["free"]),
                                  {"sp": {"total": live["total"], "free": live["free"], "used": live["used"], "live": True},
                                   "roots": [], "mounts": []})
            g["mounts"].append(name)
    volumes = []
    for g in groups.values():
        names = g["roots"] + [m for m in g["mounts"] if m not in g["roots"]]
        volumes.append({"roots": names, "configured": bool(g["roots"]), "total": g["sp"]["total"], "free": g["sp"]["free"],
                        "used": g["sp"]["used"], "live": g["sp"]["live"], "seen": g["sp"].get("seen")})
    volumes.sort(key=lambda v: -v["free"])
    return {
        "space": space, "volumes": volumes, "overlaps": sa.root_overlaps(CFG),
        "roots": [{"root": r, "scanned": d, "files": n, "bytes": b, "kinds": by_root[r], "indexed": indexed[r],
                   "mounted": os.path.isdir(CFG["roots"].get(r, ""))} for r, d, n, b in scans],
        "unscanned": [{"root": r, "mounted": os.path.isdir(CFG["roots"][r])} for r in unscanned],
        "lightroom": [r[0] for r in query("SELECT DISTINCT catalogue FROM lr", optional=True)],
    }


def api_tree(root, path, kind="", limit=500):
    """Children of a folder, biggest first. Every folder is reachable; limit=0 returns them all.
    kind filters to one file type (folder sizes then count only that type)."""
    p = path.strip("/")
    prefix = (p + "/") if p else ""
    if is_indexed(root):
        kids, ndirs = [], 0
        for name, n, b, m, mix in query("SELECT name, files, bytes, newest, mix FROM dirs WHERE root=? AND parent=?", (root, p)):
            mixd = json.loads(mix) if mix else {}
            if kind:
                if kind not in mixd:
                    continue
                n, b = mixd[kind]
            ndirs += 1
            kids.append({"name": name, "dir": True, "files": n, "bytes": b, "newest": day(m), "mix": mixd})
        where, args = "root=? AND rtrim(rel, replace(rel, '/', ''))=?", [root, prefix]
        if kind:
            where += " AND kind=?"; args.append(kind)
        nfiles = query(f"SELECT COUNT(*) FROM files WHERE {where}", args)[0][0]
        lim = f" LIMIT {int(limit)}" if limit else ""
        for name, size, m, k in query(f"SELECT substr(rel, ?), size, mtime, kind FROM files WHERE {where} "
                                      f"ORDER BY size DESC{lim}", [len(prefix) + 1] + args):
            kids.append({"name": name, "dir": False, "files": 1, "bytes": size, "newest": day(m), "kind": k})
        kids.sort(key=lambda c: -c["bytes"])
        return {"root": root, "path": p, "children": kids[:limit] if limit else kids,
                "total": ndirs + nfiles, "indexed": True}
    # no index yet: aggregate on the fly (slow on big drives; no type filter)
    L = len(prefix)
    lo, hi = prefix, prefix + "\uffff"
    lim = f" LIMIT {int(limit)}" if limit else ""
    rows = query(f"""
      SELECT CASE WHEN instr(substr(rel, ?), '/') > 0
                  THEN substr(rel, ?, instr(substr(rel, ?), '/') - 1) ELSE substr(rel, ?) END AS name,
             instr(substr(rel, ?), '/') > 0 AS isdir, COUNT(*), SUM(size), MAX(mtime)
      FROM files WHERE root=? AND rel >= ? AND rel < ?
      GROUP BY name, isdir ORDER BY SUM(size) DESC{lim}""",
        (L + 1,) * 5 + (root, lo, hi))
    kids = [{"name": n, "dir": bool(d), "files": c, "bytes": b or 0, "newest": day(m)} for n, d, c, b, m in rows]
    return {"root": root, "path": p, "children": kids, "total": len(kids), "indexed": False}


def api_sunburst(root, path="", kind=""):
    """Nested folder sizes from the top of the drive, for the sunburst. Folders under 2% of the
    drive are folded into 'other', except the ones on the current path."""
    if not is_indexed(root):
        return {"indexed": False}
    chain = [x for x in path.strip("/").split("/") if x]
    maxd = min(8, max(3, len(chain) + 2))
    if kind:
        r = query("SELECT bytes FROM kinds WHERE root=? AND kind=?", (root, kind))
    else:
        r = query("SELECT nbytes FROM scans WHERE root=?", (root,))
    total = r[0][0] if r else 0
    thr = total * 0.02

    def rows(parent):
        out = []
        for name, b, mix in query("SELECT name, bytes, mix FROM dirs WHERE root=? AND parent=? ORDER BY bytes DESC",
                                  (root, parent)):
            m = json.loads(mix) if mix else {}
            if kind:
                if kind not in m:
                    continue
                b = m[kind][1]
            out.append((name, b, m))
        out.sort(key=lambda x: -x[1])
        return out

    def build(parent, pbytes, level):
        kept = []
        for name, b, m in rows(parent):
            on = level <= len(chain) and parent == "/".join(chain[:level - 1]) and name == chain[level - 1]
            if on or (b >= thr and len(kept) < 8):
                kept.append((name, b, m))
        kids = []
        for name, b, m in kept:
            p = f"{parent}/{name}" if parent else name
            kids.append({"name": name, "path": p, "bytes": b, "mix": m, "kids": build(p, b, level + 1) if level < maxd else []})
        rest = pbytes - sum(k[1] for k in kept)
        if rest > 0 and kids:
            kids.append({"name": "other", "other": True, "bytes": rest, "kids": []})
        return kids

    return {"indexed": True, "total": total, "depth": maxd,
            "tree": {"name": root, "path": "", "bytes": total, "kids": build("", total, 1)}}


def api_dupes():
    key = "COALESCE(fhash, phash)"
    groups = query(f"""SELECT {key}, size, COUNT(*), COUNT(DISTINCT root) FROM files
        WHERE {key} IS NOT NULL GROUP BY {key}, size HAVING COUNT(*) > 1
        ORDER BY (COUNT(*)-1)*size DESC""")
    verified = bool(query("SELECT 1 FROM files WHERE fhash IS NOT NULL LIMIT 1"))
    out = []
    for k, s, c, v in groups[:300]:
        locs = query(f"SELECT root, rel FROM files WHERE {key}=? AND size=? LIMIT 6", (k, s))
        out.append({"size": s, "copies": c, "volumes": v, "locations": [{"root": r, "rel": p} for r, p in locs]})
    return {"groups": len(groups), "reclaimable": sum((c - 1) * s for _, s, c, _ in groups),
            "verified": verified, "top": out}


def api_orphans(catalogue=""):
    """Photos on the scanned drives that the loaded catalogue(s) do not reference (all loaded, or just one)."""
    rows = query("SELECT path FROM lrfiles" + (" WHERE catalogue=?" if catalogue else ""),
                 (catalogue,) if catalogue else (), optional=True)
    lrset = {p for (p,) in rows}
    if not lrset:
        return {"available": False}
    by_top = defaultdict(lambda: [0, 0])
    total = 0
    for root, rel, size in query("SELECT root, rel, size FROM files WHERE kind='photo' AND ext!='png'"):
        if os.path.join(CFG["roots"][root], rel) not in lrset:
            k = (root, rel.split("/")[0]); by_top[k][0] += 1; by_top[k][1] += size; total += 1
    out = [{"root": r, "folder": t, "files": v[0], "bytes": v[1]}
           for (r, t), v in sorted(by_top.items(), key=lambda x: -x[1][1])[:100]]
    return {"available": True, "total": total, "rows": out, "scope": catalogue or "all loaded catalogues"}


def is_backup(rel):
    return "backup" in rel.lower()


def api_catalogues():
    """Every Lightroom catalogue (.lrcat) the scans found, plus any read from elsewhere."""
    loaded = {c: (f, s, m, d) for c, f, s, m, d in
              query("SELECT catalogue, files, in_scanned, missing, loaded FROM lrcats", optional=True)}
    out, seen = [], set()
    for root, rel, size, mtime in query("SELECT root, rel, size, mtime FROM finds WHERE kind='lrcat'", optional=True):
        if root not in CFG["roots"]:
            continue
        full = os.path.join(CFG["roots"][root], rel)
        seen.add(full)
        ld = loaded.get(full)
        out.append({"path": full, "name": os.path.basename(rel), "where": f"{root}: {os.path.dirname(rel)}", "size": size,
                    "modified": day(mtime), "backup": is_backup(rel), "found": True,
                    **({"loaded": ld[3], "photos": ld[0], "on_scanned": ld[1], "missing": ld[2]} if ld else {})})
    for full, ld in loaded.items():
        if full not in seen:
            out.append({"path": full, "name": os.path.basename(full), "where": os.path.dirname(full), "size": None,
                        "modified": "", "backup": is_backup(full), "found": False,
                        "loaded": ld[3], "photos": ld[0], "on_scanned": ld[1], "missing": ld[2]})
    out.sort(key=lambda c: (c["backup"], c["modified"] == "", c["modified"]), reverse=False)
    out.sort(key=lambda c: (c["backup"], -int(c["modified"].replace("-", "") or 0)))
    return {"catalogues": out, "indexed_roots": [r for (r,) in query("SELECT root FROM dirs_built", optional=True)]}


# ---- Aperture (read-only view of what each Library.apdb says)
def lr_covered(alias="m"):
    """SQL: this master has a same-name, same-size file in a loaded Lightroom catalogue."""
    return (f"(EXISTS(SELECT 1 FROM lrfiles l WHERE l.name_lc={alias}.name_lc AND l.size={alias}.size) OR "
            f"EXISTS(SELECT 1 FROM lrfiles l WHERE l.name_lc={alias}.orig_lc AND l.size={alias}.size))")


def lr_loaded():
    return bool(query("SELECT 1 FROM lrfiles LIMIT 1", optional=True))


def ap_part_bytes(root, lib_rel):
    """Disk usage of a library split by its parts, from the scanned file list."""
    def span(sub):
        pre = lib_rel + "/" + (sub + "/" if sub else "")
        return query("SELECT COALESCE(SUM(size),0) FROM files WHERE root=? AND rel>=? AND rel<?", (root, pre, pre + "\uffff"))[0][0]
    parts = {"masters": span("Masters"), "previews": span("Previews"), "thumbnails": span("Thumbnails"), "database": span("Database")}
    total = span("")
    parts["other"] = max(0, total - sum(parts.values()))
    parts["total"] = total
    return parts


def api_aperture():
    cov = lr_loaded()
    libs = {lib: row for lib, *row in query(
        "SELECT lib, root, rel, masters, managed, referenced, absent, trashed, min_date, max_date, loaded FROM aplibs", optional=True)}
    out = []
    for root, rel in [(r, p) for r, p in query("SELECT root, rel FROM finds WHERE kind='aperture'", optional=True)]:
        if root in CFG["roots"]:
            libs.setdefault(os.path.join(CFG["roots"][root], rel), [root, rel] + [None] * 9)
    for lib, (root, rel, masters, managed, referenced, absent, trashed, mn, mx, loaded) in libs.items():
        item = {"path": lib, "name": os.path.basename(lib), "root": root, "rel": rel, "loaded": loaded,
                "masters": masters, "managed": managed, "referenced": referenced, "absent": absent, "trashed": trashed,
                "from": day(mn), "to": day(mx), "bytes": ap_part_bytes(root, rel) if root in CFG["roots"] else None}
        if masters and cov:
            item["covered"] = query(f"SELECT COUNT(*) FROM apmasters m WHERE lib=? AND {lr_covered()}", (lib,))[0][0]
        out.append(item)
    out.sort(key=lambda x: -(x["bytes"] or {}).get("total", 0))
    return {"libraries": out, "lightroom_loaded": cov}


def ap_folder_tree(lib):
    """{parent uuid or '': [children]} with photo counts rolled up through the hierarchy."""
    cov = lr_loaded()
    folders = query("SELECT uuid, parent, name, ftype, versions, min_date, max_date FROM apfolders WHERE lib=?", (lib,), optional=True)
    ids = {f[0] for f in folders}
    expr = f"SUM(CASE WHEN {lr_covered()} THEN 1 ELSE 0 END)" if cov else "0"
    direct = {p: (n, b, c) for p, n, b, c in query(
        f"SELECT project, COUNT(*), COALESCE(SUM(size),0), {expr} FROM apmasters m WHERE lib=? GROUP BY project", (lib,), optional=True)}
    kids = defaultdict(list)
    for u, par, *_ in folders:
        kids[par if par in ids else ""].append(u)
    info = {u: (par, name, ft, mn, mx) for u, par, name, ft, vc, mn, mx in folders}
    memo = {}

    def roll(u, seen=()):
        if u in memo:
            return memo[u]
        n, b, c = direct.get(u, (0, 0, 0))
        mn, mx = info[u][3], info[u][4]
        for ch in kids.get(u, []):
            if ch in seen:
                continue
            cn, cb, cc, cmn, cmx = roll(ch, seen + (u,))
            n += cn; b += cb; c += cc
            mn = cmn if mn is None or (cmn is not None and cmn < mn) else mn
            mx = cmx if mx is None or (cmx is not None and cmx > mx) else mx
        memo[u] = (n, b, c, mn, mx)
        return memo[u]

    tree = defaultdict(list)
    for par, us in kids.items():
        for u in us:
            n, b, c, mn, mx = roll(u)
            tree[par].append({"uuid": u, "name": info[u][1] or "(untitled)", "masters": n, "direct": direct.get(u, (0,))[0],
                              "bytes": b, "covered": c, "from": day(mn), "to": day(mx), "kids": len(kids.get(u, []))})
        tree[par].sort(key=lambda x: (x["from"] or "9999", x["name"]))
    orphan = [p for p in direct if p and p not in ids]  # masters whose project is gone/trashed
    if orphan:
        n = sum(direct[p][0] for p in orphan); b = sum(direct[p][1] for p in orphan); c = sum(direct[p][2] for p in orphan)
        tree[""].append({"uuid": "~orphans", "name": "(no project)", "masters": n, "direct": n, "bytes": b,
                         "covered": c, "from": "", "to": "", "kids": 0})
    return tree


def api_aperture_tree(lib, parent=""):
    return {"lib": lib, "parent": parent, "children": ap_folder_tree(lib).get(parent, []), "lightroom_loaded": lr_loaded()}


def api_aperture_masters(lib, project, limit=500):
    cov = lr_loaded()
    if project == "~orphans":
        where, args = "lib=? AND project NOT IN (SELECT uuid FROM apfolders WHERE lib=?)", (lib, lib)
    else:
        where, args = "lib=? AND project=?", (lib, project)
    cexpr = lr_covered() if cov else "0"
    total = query(f"SELECT COUNT(*) FROM apmasters m WHERE {where}", args, optional=True)[0][0]
    rows = query(f"SELECT COALESCE(NULLIF(orig,''), name), size, date, is_ref, missing, trashed, {cexpr} FROM apmasters m "
                 f"WHERE {where} ORDER BY date LIMIT {int(limit)}", args, optional=True)
    return {"total": total, "lightroom_loaded": cov, "masters": [
        {"name": n, "size": s or 0, "date": day(d), "referenced": bool(r), "missing": bool(mi), "trashed": bool(t), "in_lightroom": bool(c)}
        for n, s, d, r, mi, t, c in rows]}


def build_tree(depth=4, per_dir=1000):
    """Aggregate folder sizes (with per-type mix) to `depth` levels for the static report."""
    acc = {}
    for root, rel, size, mtime, kind in query("SELECT root, rel, size, mtime, kind FROM files"):
        parts = rel.split("/")
        for i in range(min(len(parts), depth)):
            key = (root, "/".join(parts[:i]), parts[i], i < len(parts) - 1)
            a = acc.get(key)
            if a is None:
                a = acc[key] = [0, 0, 0.0, {}]
            a[0] += 1; a[1] += size
            if mtime > a[2]:
                a[2] = mtime
            km = a[3].get(kind)
            if km is None:
                a[3][kind] = [1, size]
            else:
                km[0] += 1; km[1] += size
    tree = defaultdict(lambda: defaultdict(list))
    for (root, parent, name, isdir), (n, b, m, mix) in acc.items():
        item = {"name": name, "dir": isdir, "files": n, "bytes": b, "newest": day(m)}
        if isdir:
            item["mix"] = mix
        else:
            item["kind"] = next(iter(mix))
        tree[root][parent].append(item)
    for root in tree:
        for parent in tree[root]:
            tree[root][parent].sort(key=lambda c: -c["bytes"])
            tree[root][parent] = tree[root][parent][:per_dir]
    return {"depth": depth, "tree": tree}


def write_index(cfg, depth=None):
    """Write a self-contained index.html (overview, drill-down, duplicates, Lightroom)."""
    global CFG
    CFG = cfg
    depth = depth or int(cfg.get("report_depth", 4))
    data = {"summary": api_summary(), "config": {"roots": cfg["roots"], "report_dir": cfg["report_dir"]},
            "dupes": api_dupes(), "orphans": api_orphans(), "built": datetime.now().strftime("%Y-%m-%d"),
            "catalogues": api_catalogues(), "aperture": api_aperture(),
            "ap_tree": {l["path"]: ap_folder_tree(l["path"]) for l in api_aperture()["libraries"] if l["loaded"]},
            **build_tree(depth)}
    blob = json.dumps(data).replace("</", "<\\/")
    page = PAGE.replace("<script>\n", "<script>\nconst DATA=" + blob + ";\n", 1)
    os.makedirs(cfg["report_dir"], exist_ok=True)
    path = os.path.join(cfg["report_dir"], "index.html")
    with open(path, "w") as f:
        f.write(page)
    return path


def run_job(title, argv):
    with JOB_LOCK:
        if JOB["running"]:
            return False
        JOB.update(running=True, title=title, log=[], code=None, progress=None)

    def work():
        try:
            p = subprocess.Popen([sys.executable, os.path.join(HERE, "storage_audit.py"), "--config", CONFIG_PATH] + argv,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                 env=dict(os.environ, STORAGE_AUDIT_PROGRESS="1"))
            buf = ""
            for ch in iter(lambda: p.stdout.read(1), ""):
                if ch in "\r\n":
                    if buf.startswith("@@PROGRESS "):
                        try:
                            JOB["progress"] = json.loads(buf[len("@@PROGRESS "):])
                        except ValueError:
                            pass
                        buf = ""
                    elif buf:
                        JOB["log"].append(buf[-300:]); buf = ""
                        JOB["log"] = JOB["log"][-400:]
                else:
                    buf += ch
            if buf:
                JOB["log"].append(buf[-300:])
            JOB["code"] = p.wait()
        except Exception as e:  # noqa: BLE001
            JOB["log"].append(f"error: {e}"); JOB["code"] = -1
        finally:
            JOB["progress"] = None
            JOB["running"] = False
    threading.Thread(target=work, daemon=True).start()
    return True


def reveal(body):
    """Open a folder (or select a file) in Finder. Only paths inside a configured root are allowed."""
    if sys.platform != "darwin":
        return "Open in Finder only works when the UI runs on macOS"
    roots = {k: os.path.realpath(v) for k, v in CFG["roots"].items()}
    if body.get("root") in roots:
        target = os.path.join(roots[body["root"]], str(body.get("rel") or ""))
    elif body.get("abs"):
        target = str(body["abs"])
    else:
        return "no location given"
    target = os.path.realpath(target)
    if not any(target == r or target.startswith(r.rstrip("/") + "/") for r in roots.values()):
        return "that location is outside your configured roots"
    while not os.path.exists(target) and target not in roots.values():
        target = os.path.dirname(target)   # deleted or renamed since the scan: open the nearest folder
    if not os.path.exists(target):
        return "drive not mounted"
    subprocess.run(["open", "-R", target] if os.path.isfile(target) else ["open", target], check=False)
    return True


def start_action(body):
    cmd = body.get("cmd")
    if cmd == "scan":
        roots = [r for r in body.get("roots", []) if r in CFG["roots"]]
        return run_job("scan " + (" ".join(roots) or "all roots"), ["scan"] + roots)
    if cmd == "index":
        roots = [r for r in body.get("roots", []) if r in CFG["roots"]]
        return run_job("index " + (" ".join(roots) or "all scanned roots"), ["index"] + roots)
    if cmd == "dupes":
        return run_job("dupes" + (" --full" if body.get("full") else ""), ["dupes"] + (["--full"] if body.get("full") else []))
    if cmd == "lrcat":
        cats = [os.path.expanduser(c) for c in body.get("catalogues", [])]
        bad = [c for c in cats if not (c.endswith(".lrcat") and os.path.isfile(c))]
        if bad or not (cats or body.get("all")):
            return "not a readable .lrcat file: " + (bad[0] if bad else "(none given)")
        return run_job("lrcat " + (os.path.basename(cats[0]) if len(cats) == 1 and not body.get("all") else "all catalogues"),
                       ["lrcat"] + cats + (["--all"] if body.get("all") else []))
    if cmd == "aperture":
        libs = [os.path.expanduser(c) for c in body.get("libraries", [])]
        bad = [c for c in libs if not os.path.isfile(os.path.join(c, "Database", "apdb", "Library.apdb"))]
        if bad:
            return "not an Aperture library: " + bad[0]
        return run_job("aperture " + (os.path.basename(libs[0]) if len(libs) == 1 else "all libraries"), ["aperture"] + libs + ([] if libs else ["--all"]))
    return "unknown action"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        return (self.headers.get("Host", "").split(":")[0]) in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self._send(200, PAGE, "text/html")
            if u.path == "/api/summary":
                return self._send(200, api_summary())
            if u.path == "/api/tree":
                if q.get("root") not in CFG["roots"]:
                    return self._send(400, {"error": "unknown root"})
                return self._send(200, api_tree(q["root"], q.get("path", ""), q.get("kind", ""), int(q.get("limit", 500))))
            if u.path == "/api/sunburst":
                if q.get("root") not in CFG["roots"]:
                    return self._send(400, {"error": "unknown root"})
                return self._send(200, api_sunburst(q["root"], q.get("path", ""), q.get("kind", "")))
            if u.path == "/api/dupes":
                return self._send(200, api_dupes())
            if u.path == "/api/orphans":
                return self._send(200, api_orphans(q.get("catalogue", "")))
            if u.path == "/api/catalogues":
                return self._send(200, api_catalogues())
            if u.path == "/api/aperture":
                return self._send(200, api_aperture())
            if u.path == "/api/aperture/tree":
                return self._send(200, api_aperture_tree(q.get("lib", ""), q.get("parent", "")))
            if u.path == "/api/aperture/masters":
                return self._send(200, api_aperture_masters(q.get("lib", ""), q.get("project", ""), int(q.get("limit", 500))))
            if u.path == "/api/job":
                return self._send(200, {k: JOB[k] for k in ("running", "title", "log", "code", "progress")})
            if u.path == "/api/config":
                return self._send(200, {"roots": CFG["roots"], "report_dir": CFG["report_dir"]})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Audit") != "1":
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/run":
            r = start_action(body)
            if r is True:
                return self._send(200, {"started": True})
            return self._send(409 if r is False else 400, {"error": "a job is already running" if r is False else r})
        if self.path == "/api/reveal":
            r = reveal(body)
            return self._send(200 if r is True else 400, {"ok": True} if r is True else {"error": r})
        if self.path == "/api/report":
            return self._send(200, {"path": write_index(CFG)})
        self._send(404, {"error": "not found"})


PAGE = r"""<!doctype html><html lang="en-AU"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Storage audit</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1c1c1a;--mute:#6b6b66;--line:#e3e3df;--acc:#2f6fed;--bar:#e8edf9;--good:#2f9e44;--bad:#d9480f;
--photo:#2f6fed;--video:#e8590c;--audio:#9c36b5;--document:#2f9e44;--archive:#f08c00;--system:#868e96;--lightroom:#e64980;--other:#ced4da}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecece8;--mute:#9a9a94;--line:#31312e;--acc:#6e9bff;--bar:#2a3550;--good:#51cf66;--bad:#ff8a5c;
--photo:#5c8dff;--video:#ff8a4c;--audio:#c77de0;--document:#51cf66;--archive:#ffb020;--system:#9aa1a8;--lightroom:#f06595;--other:#5a5a56}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;gap:16px;align-items:center;padding:14px 24px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
h1{font-size:16px;margin:0 12px 0 0}nav button{background:none;border:0;padding:8px 12px;color:var(--mute);font:inherit;cursor:pointer;border-bottom:2px solid transparent}
nav button.on{color:var(--ink);border-color:var(--acc)}main,#sunwrap{padding:24px;max-width:1240px;margin:0 auto}#sunwrap{padding-bottom:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:12px}.grid .card{margin:0}
.card.warn{border-color:var(--archive)}.card h3{margin:0 0 2px;font-size:15px}.mute{color:var(--mute)}.big{font-size:22px;font-weight:600}
.stack{display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--line);margin:10px 0}.stack i{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--mute)}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.key{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:0 0 12px}
.chip{display:inline-flex;align-items:center;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:14px;padding:3px 11px 3px 8px;font:inherit;font-size:12.5px;cursor:pointer}
.chip.on{border-color:var(--ink);box-shadow:0 0 0 1px var(--ink)}.chip:hover{background:var(--bar)}
button.btn{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:7px 14px;font:inherit;cursor:pointer}
button.btn.alt{background:var(--card);color:var(--ink);border:1px solid var(--line)}button.btn.sm{padding:3px 10px;font-size:12.5px}button.btn:disabled{opacity:.5;cursor:default}
table{width:100%;border-collapse:collapse}td,th{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mute);font-weight:500;font-size:12px}td.n,th.n{text-align:right;white-space:nowrap}
tr.row{cursor:pointer}tr.row:hover{background:var(--bar)}
.bar{height:8px;background:transparent;border-radius:4px;margin-top:5px}.bar>div{display:flex;height:100%;border-radius:4px;overflow:hidden;min-width:3px}.bar i{display:block;height:100%}
.crumbs{margin:0 0 12px}.crumbs a{color:var(--acc);cursor:pointer}.pill{font-size:11px;border:1px solid var(--line);border-radius:10px;padding:1px 8px;color:var(--mute)}
.tag{font-size:11px;border-radius:9px;padding:1px 8px;margin-left:6px;border:1px solid var(--line);color:var(--mute)}.tag.good{color:var(--good);border-color:var(--good)}.tag.bad{color:var(--bad);border-color:var(--bad)}
pre{background:#111;color:#cfe;padding:12px;border-radius:8px;max-height:340px;overflow:auto;font-size:12px}
.row2{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0}select,input[type=text]{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
.loc{font:12px ui-monospace,monospace;color:var(--mute);cursor:copy;word-break:break-all}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px}
.cov{display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--bad);margin-top:5px;min-width:60px}.cov i{display:block;background:var(--good)}
#prog{position:absolute;left:0;right:0;top:100%;background:var(--card);border-bottom:1px solid var(--line);padding:7px 24px;font-size:12.5px;display:flex;gap:14px;align-items:center;box-shadow:0 2px 6px rgba(0,0,0,.08)}
#prog .pbar{flex:0 0 280px;height:8px;background:var(--line);border-radius:4px;overflow:hidden;position:relative}#prog .pbar i{display:block;height:100%;background:var(--acc);width:0;transition:width .3s}
#prog .pbar.ind i{width:35%;position:absolute;animation:slide 1.2s infinite ease-in-out}#prog.ok .pbar i{background:var(--good);width:100%}#prog.bad .pbar i{background:var(--bad);width:100%}
@keyframes slide{0%{left:-35%}100%{left:100%}}
</style>
<header><h1>Storage audit</h1><nav id="nav"></nav><span style="flex:1"></span><button id="sunbtn" class="btn alt sm" hidden></button><button id="finder" class="btn alt sm" hidden></button><span id="jobstate" class="pill" hidden></span><div id="prog" hidden><div class="pbar"><i></i></div><span id="plabel"></span></div></header>
<div id="sunwrap" hidden></div>
<main id="view"></main>
<script>
const STATIC=typeof DATA!=='undefined';
const KINDS=['photo','video','audio','document','archive','app/system','lightroom','other'];
const KIND_LABEL={photo:'Photo',video:'Video',audio:'Audio',document:'Document',archive:'Archive','app/system':'App / system',lightroom:'Lightroom',other:'Other'};
const COLORS={photo:'--photo',video:'--video',audio:'--audio',document:'--document',archive:'--archive','app/system':'--system',lightroom:'--lightroom',other:'--other'};
const TABS=STATIC?['Overview','Browse','Duplicates','Lightroom','Aperture']:['Overview','Browse','Duplicates','Lightroom','Aperture','Run'];
let sunOpen=pref('sun')!=='0',tab='Overview',S=null,CFGJ=null,browse={root:null,path:'',kind:null,all:false,skip:true,view:pref('view')||'table'},ap={lib:null,trail:[],cur:null},lrScope='';
const $=s=>document.querySelector(s),view=$('#view'),enc=encodeURIComponent;
const fmt=b=>{const u=['B','KB','MB','GB','TB'];let i=0;b=b||0;while(b>=1024&&i<4){b/=1024;i++}return (i?b.toFixed(1):b)+' '+u[i]};
const num=n=>(n||0).toLocaleString('en-AU');
function el(t,a={},...k){const e=document.createElement(t);for(const[x,v]of Object.entries(a)){if(x==='class')e.className=v;else if(x.startsWith('on'))e[x]=v;else e.setAttribute(x,v)}for(const c of k.flat())if(c!==null&&c!==undefined&&c!==false)e.append(c instanceof Node?c:document.createTextNode(c));return e}
/* progress bar: shows the running job (with what it is doing) or any slow request in flight */
const busyMap=new Map();let bid=0,jobProg=null,flash=null;
const busy=l=>{const id=++bid;busyMap.set(id,l);showProg();return()=>{busyMap.delete(id);showProg()}};
const LABELS=[['/api/tree','Loading folder'],['/api/sunburst','Drawing sunburst'],['/api/summary','Loading drive summary'],['/api/catalogues','Loading Lightroom catalogues'],['/api/aperture','Loading Aperture libraries'],['/api/dupes','Loading duplicates'],['/api/orphans','Comparing photos with the catalogue'],['/api/reveal','Opening in Finder'],['/api/report','Writing the HTML report'],['/api/run','Starting job']];
const labelFor=u=>(LABELS.find(([p])=>String(u).startsWith(p))||[0,'Working'])[1];
function showProg(){const b=$('#prog');if(!b)return;const bar=b.querySelector('.pbar'),lab=$('#plabel');b.className='';bar.className='pbar';
  if(jobProg&&jobProg.running){const p=jobProg.progress||{},t=jobProg.title;let txt=t,pct=null;
    if(p.label){const st=p.steps>1?` (${p.step} of ${p.steps})`:'';txt=`${p.label}${st}`+(p.detail?` · ${p.detail}`:'');
      if(p.total&&p.done<=p.total){pct=Math.min(99,100*p.done/p.total);txt+=` · ${num(p.done)} of ${p.approx?'about ':''}${num(p.total)} (${Math.round(pct)}%)`}
      else if(p.done)txt+=` · ${num(p.done)} so far`}
    if(pct===null)bar.classList.add('ind');else bar.firstChild.style.width=pct+'%';lab.textContent=txt;b.hidden=false;return}
  if(busyMap.size){bar.classList.add('ind');lab.textContent=[...busyMap.values()].pop()+'…';b.hidden=false;return}
  if(flash){b.classList.add(flash.bad?'bad':'ok');lab.textContent=flash.msg;b.hidden=false;return}
  b.hidden=true}
const get=u=>{if(!STATIC){const done=u==='/api/job'?()=>{}:busy(labelFor(u));return fetch(u).then(r=>r.json()).finally(done)}
  const x=new URL(u,'http://x'),q=Object.fromEntries(x.searchParams),P=x.pathname,R=v=>Promise.resolve(v);
  if(P==='/api/summary')return R(DATA.summary);if(P==='/api/config')return R(DATA.config);if(P==='/api/dupes')return R(DATA.dupes);
  if(P==='/api/orphans')return R(DATA.orphans);if(P==='/api/catalogues')return R(DATA.catalogues);if(P==='/api/aperture')return R(DATA.aperture);
  if(P==='/api/sunburst')return R(sunStatic(q.root,q.path||'',q.kind||''));
  if(P==='/api/job')return R({running:false,title:'',log:[],code:null});
  if(P==='/api/aperture/tree')return R({children:((DATA.ap_tree||{})[q.lib]||{})[q.parent||'']||[],lightroom_loaded:DATA.aperture.lightroom_loaded});
  if(P==='/api/aperture/masters')return R({total:0,masters:[],static:true});
  if(P==='/api/tree'){const all=(DATA.tree[q.root]||{})[q.path||''];
    if(!all)return R({children:[],total:0,truncated:!!q.path&&q.path.split('/').length>=DATA.depth});
    let c=all;if(q.kind)c=all.filter(i=>i.dir?(i.mix||{})[q.kind]:i.kind===q.kind).map(i=>i.dir?{...i,files:i.mix[q.kind][0],bytes:i.mix[q.kind][1]}:i).sort((a,b)=>b.bytes-a.bytes);
    return R({children:c,total:c.length,indexed:true})}
  return R({})};
const post=(u,b)=>{const done=busy(labelFor(u));return fetch(u,{method:'POST',headers:{'X-Audit':'1','Content-Type':'application/json'},body:JSON.stringify(b)}).then(async r=>({ok:r.ok,...await r.json()})).finally(done)};
const runJob=b=>post('/api/run',b).then(r=>{if(!r.ok)alert(r.error);poll()});
let here=null;
function setHere(h){here=h;const b=$('#finder');b.hidden=!h;if(!h)return;b.textContent=STATIC?'Copy path':'Open in Finder';
  b.title=h.label||'';b.onclick=()=>reveal(h)}
function reveal(h){if(STATIC){const p=h.abs||((CFGJ.roots[h.root]||'')+(h.rel?'/'+h.rel:''));navigator.clipboard&&navigator.clipboard.writeText(p);return}
  post('/api/reveal',h.abs?{abs:h.abs}:{root:h.root,rel:h.rel||''}).then(r=>{if(!r.ok)alert(r.error)})}
function finderBtn(h,txt){return el('button',{class:'btn sm alt',title:STATIC?'Copy full path':'Open in Finder',onclick:e=>{e.stopPropagation();reveal(h)}},txt||(STATIC?'Copy path':'Finder'))}
/* ---- free space: how full the volume is, and where this scan sits within it ---- */
const fullPct=sp=>sp&&sp.total?Math.round(100*sp.used/sp.total):0;
const lowSpace=sp=>sp&&sp.total&&sp.free/sp.total<0.10?'bad':(sp&&sp.total&&sp.free/sp.total<0.20?'warn':'');
function spaceBar(rootRow,sp,compact){if(!sp||!sp.total)return null;const t=sp.total,sc=Math.min(rootRow.bytes||0,sp.used),oth=Math.max(0,sp.used-sc),lv=lowSpace(sp);
  const seg=(w,c,ti)=>el('i',{title:ti,style:`width:${100*w/t}%;background:${c}`});
  return el('div',{},el('div',{class:'stack',style:'margin:6px 0 4px'},seg(sc,'var(--acc)',`This scan: ${fmt(sc)}`),seg(oth,'var(--system)',`Other files on the volume: ${fmt(oth)}`),seg(sp.free,'var(--good)',`Free: ${fmt(sp.free)}`)),
    el('div',{style:'font-size:12.5px'},el('b',{},fmt(sp.free)+' free'),` of ${fmt(sp.total)} · ${fullPct(sp)}% full`,lv?el('span',{class:'tag '+(lv==='bad'?'bad':'')},lv==='bad'?'almost full':'getting full'):null,
      sp.live?null:el('span',{class:'mute'},` · last seen ${sp.seen||'earlier'}`)),
    compact?null:el('div',{class:'legend',style:'margin-top:4px'},el('span',{},el('span',{class:'sw',style:'background:var(--acc)'}),'this scan'),el('span',{},el('span',{class:'sw',style:'background:var(--system)'}),'other files on the volume'),el('span',{},el('span',{class:'sw',style:'background:var(--good)'}),'free')))}
let showFree=pref('free')!=='0',chartScope='this folder';
/* ---- volumes: free space on every mounted drive, plus a move planner ---- */
let plan={bytes:0,label:'',root:''},linkRate=pref('rate')||'110';
const RATES=[['110','Gigabit Ethernet (~110 MB/s)'],['280','2.5 GbE (~280 MB/s)'],['1000','10 GbE (~1,000 MB/s)'],['40','Wi-Fi (~40 MB/s)'],['350','USB 3 drive (~350 MB/s)'],['35','Slow / busy network (~35 MB/s)']];
function dur(sec){if(sec<90)return Math.round(sec)+' s';if(sec<5400)return Math.round(sec/60)+' min';if(sec<172800)return (sec/3600).toFixed(1)+' h';return (sec/86400).toFixed(1)+' days'}
function volumesCard(){const vols=S.volumes||[];if(!vols.length)return null;
  const inp=el('input',{type:'text',size:'7',placeholder:'GB',value:plan.bytes?String(Math.round(plan.bytes/1e9)):'',onchange:e=>{const v=parseFloat(e.target.value);plan={bytes:isFinite(v)&&v>0?v*1e9:0,label:v>0?`${v} GB`:'',root:''};rOverview()}});
  const rate=el('select',{onchange:e=>{linkRate=e.target.value;pref('rate',linkRate);rOverview()}},RATES.map(([v,l])=>el('option',{value:v,...(v===linkRate?{selected:''}:{})},l)));
  const rows=vols.map(v=>{const pct=v.total?Math.round(100*v.used/v.total):0,lv=lowSpace(v);let fit=null,tm=null;
    if(plan.bytes){const same=plan.root&&v.roots.includes(plan.root);
      if(same){fit=el('span',{class:'mute'},'same drive')}
      else{const left=v.free-plan.bytes,tight=left<0.1*v.total;fit=left<0?el('span',{class:'tag bad'},`short by ${fmt(-left)}`):el('span',{class:'tag '+(tight?'':'good'),style:tight?'color:var(--archive);border-color:var(--archive)':''},tight?`fits, leaves ${fmt(left)} (tight)`:`fits, leaves ${fmt(left)}`);
        tm=left<0?null:dur(plan.bytes/(parseFloat(linkRate)*1e6))}}
    const names=v.roots.length>4?v.roots.slice(0,4).join(', ')+` +${v.roots.length-4} more`:v.roots.join(', ');
    return el('tr',{},el('td',{},el('b',{},names),v.configured?null:el('span',{class:'tag'},'not in config'),v.live?null:el('span',{class:'tag'},'not mounted')),
      el('td',{class:'n'},fmt(v.total)),el('td',{class:'n'},el('b',{},fmt(v.free))),
      el('td',{style:'min-width:150px'},el('div',{class:'stack',style:'margin:4px 0'},el('i',{style:`width:${pct}%;background:${lv==='bad'?'var(--bad)':lv==='warn'?'var(--archive)':'var(--acc)'}`})),el('span',{class:'mute'},pct+'% full')),
      plan.bytes?el('td',{},fit):null,plan.bytes?el('td',{class:'n'},tm||''):null)});
  return el('div',{class:'card'},el('div',{class:'row2',style:'margin-top:0'},el('h3',{style:'margin:0'},'Drives and free space'),el('span',{class:'mute'},'Shares on the same NAS volume are grouped.')),
    el('div',{class:'row2'},el('span',{},'Planning a move of'),inp,el('span',{class:'mute'},plan.label&&plan.root?`(${plan.label}: ${fmt(plan.bytes)})`:'GB, or use "Plan a move" in Browse'),el('span',{},'over'),rate),
    el('table',{},el('tr',{},el('th',{},'Drive'),el('th',{class:'n'},'Size'),el('th',{class:'n'},'Free'),el('th',{},'Used'),plan.bytes?el('th',{},'Fits?'):null,plan.bytes?el('th',{class:'n'},'Transfer time'):null),rows),
    plan.bytes?el('p',{class:'mute'},'Times assume a steady transfer. Folders full of small files (photos, caches) usually run 2 to 3 times slower than one big file.'):null)}
/* ---- charts: bars / pie / treemap of the folder you are in, plus a sunburst of where it sits ---- */
const SVGNS='http://www.w3.org/2000/svg';
function sv(t,a={},...k){const e=document.createElementNS(SVGNS,t);for(const[x,v]of Object.entries(a)){if(x.startsWith('on'))e[x]=v;else e.setAttribute(x,v)}for(const c of k.flat())if(c!==null&&c!==undefined&&c!==false)e.append(c instanceof Node?c:document.createTextNode(c));return e}
const kcol=k=>`var(${COLORS[k]||'--other'})`;
/* colour mode: 'item' gives every slice/tile/arc its own hue so neighbours are easy to tell apart; 'type' colours by file type */
let colorMode=pref('colour')||'item';
const hue=i=>(i*137.508)%360;
const icol=i=>i.free?'var(--good)':i.inert?'var(--system)':i.other?'var(--other)':(colorMode==='type'?kcol(i.kind):`hsl(${Math.round(hue(i.idx||0))} 65% 52%)`);
const sunCol=(c,level,t)=>c.other?'var(--other)':(colorMode==='type'?kcol(domKind(c.mix)):`hsl(${Math.round(hue(t))} 62% ${Math.min(70,44+(level-1)*6)}%)`);
const domKind=mix=>{let b=null,m=-1;for(const[k,v]of Object.entries(mix||{}))if(v[1]>m){m=v[1];b=k}return b||'other'};
const pctOf=(b,t)=>t?(100*b/t<0.1?'<0.1':(100*b/t).toFixed(1)):'0';
function pref(k,v){try{if(v===undefined)return localStorage.getItem('sa_'+k);localStorage.setItem('sa_'+k,v)}catch(e){return null}}
function chartItems(d,max){const L=d.children.slice(0,max).map(c=>({name:c.name,dir:c.dir,bytes:c.bytes,files:c.files,mix:c.mix,kind:c.dir?domKind(c.mix):(c.kind||'other')}));
  const rest=d.children.slice(max),rb=rest.reduce((s,c)=>s+c.bytes,0);
  if(rest.length&&rb>0)L.push({name:`other (${num(rest.length)} items)`,other:true,bytes:rb,files:rest.reduce((s,c)=>s+c.files,0),kind:'other'});
  const items=L.filter(i=>i.bytes>0);items.forEach((i,k)=>i.idx=k);chartScope='this folder';
  const R=S&&S.roots.find(r=>r.root===browse.root),sp=S&&S.space&&S.space[browse.root];
  if(showFree&&sp&&sp.total&&R&&!browse.path&&!browse.kind){const sc=items.reduce((a,i)=>a+i.bytes,0),oth=Math.max(0,sp.used-Math.max(sc,R.bytes||0));chartScope='the volume';
    if(oth>0)items.push({name:'Used by other files (not in this scan)',inert:true,bytes:oth,files:0,kind:'other'});
    if(sp.free>0)items.push({name:'Free space',inert:true,free:true,bytes:sp.free,files:0,kind:'other'})}
  return {items,total:items.reduce((s,i)=>s+i.bytes,0)}}
function tip(i,t){return i.inert?`${i.name}\n${fmt(i.bytes)} · ${pctOf(i.bytes,t)}% of the volume`:`${i.name}\n${fmt(i.bytes)} · ${num(i.files)} file${i.files===1?'':'s'} · ${pctOf(i.bytes,t)}% of ${chartScope}`+(i.dir?'':'\n(click to show in Finder)')}
function openItem(i){if(i.other||i.inert)return;const rel=(browse.path?browse.path+'/':'')+i.name;
  if(i.dir){browse.path=rel;browse.all=false;rBrowse()}else if(!STATIC)reveal({root:browse.root,rel})}
function polar(cx,cy,r,a){return [cx+r*Math.sin(a),cy-r*Math.cos(a)]}
function arcPath(cx,cy,r0,r1,a0,a1){if(a1-a0>=2*Math.PI-1e-4)a1=a0+2*Math.PI-1e-4;const big=a1-a0>Math.PI?1:0,p=(r,a)=>polar(cx,cy,r,a).map(v=>v.toFixed(2)).join(' ');
  return `M${p(r1,a0)} A${r1} ${r1} 0 ${big} 1 ${p(r1,a1)} L${p(r0,a1)} A${r0} ${r0} 0 ${big} 0 ${p(r0,a0)} Z`}
function chartBars(d){const {items,total}=chartItems(d,25),max=Math.max(1,...items.map(i=>i.bytes));
  return el('div',{},items.map(i=>el('div',{style:'display:flex;align-items:center;gap:10px;margin:5px 0;'+((i.other||i.inert)?'':'cursor:pointer'),title:tip(i,total),onclick:()=>openItem(i)},
    el('div',{style:'width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'},i.dir?'📁 ':'',i.name),
    el('div',{style:'flex:1'},el('div',{style:`height:14px;border-radius:4px;min-width:3px;width:${100*i.bytes/max}%;background:${icol(i)}`}),i.mix?mixBar(i.mix,100*i.bytes/max):mixBar({[i.kind]:[1,i.bytes]},100*i.bytes/max)),
    el('div',{style:'width:150px;text-align:right;white-space:nowrap'},fmt(i.bytes),el('span',{class:'mute'},' · '+pctOf(i.bytes,total)+'%')))))}
function chartPie(d){const {items,total}=chartItems(d,12),cx=150,cy=150,s=sv('svg',{viewBox:'0 0 300 300',style:'width:300px;height:300px;flex:none'});let a=0;
  items.forEach(i=>{const da=2*Math.PI*i.bytes/total;s.append(sv('path',{d:arcPath(cx,cy,70,142,a,a+da),fill:icol(i),stroke:'var(--card)','stroke-width':2,style:(i.other||i.inert)?'':'cursor:pointer',onclick:()=>openItem(i)},sv('title',{},tip(i,total))));a+=da});
  s.append(sv('text',{x:cx,y:cy-2,'text-anchor':'middle','font-size':17,'font-weight':600,fill:'var(--ink)'},fmt(total)),sv('text',{x:cx,y:cy+16,'text-anchor':'middle','font-size':11,fill:'var(--mute)'},'this folder'));
  return el('div',{style:'display:flex;gap:24px;align-items:center;flex-wrap:wrap'},s,el('table',{style:'flex:1;min-width:280px'},items.map(i=>el('tr',{class:(i.other||i.inert)?'':'row',onclick:()=>openItem(i)},
    el('td',{},el('span',{class:'sw',style:`background:${icol(i)}`}),i.dir?'📁 ':'',i.name),el('td',{class:'n'},fmt(i.bytes)),el('td',{class:'n mute'},pctOf(i.bytes,total)+'%')))))}
function squarify(items,x,y,w,h){const tot=items.reduce((s,i)=>s+i.bytes,0),out=[],rest=items.map(i=>({i,a:i.bytes/tot*w*h}));
  while(rest.length){const side=Math.min(w,h);let row=[],s=0,worst=Infinity;
    const wo=(r,ss)=>{const mx=Math.max(...r.map(q=>q.a)),mn=Math.min(...r.map(q=>q.a));return Math.max(side*side*mx/(ss*ss),ss*ss/(side*side*mn))};
    while(rest.length){const c=rest[0],nr=[...row,c],ns=s+c.a,wn=wo(nr,ns);if(row.length&&wn>worst)break;row=nr;s=ns;worst=wn;rest.shift()}
    if(w>=h){const rw=s/h;let yy=y;for(const r of row){const rh=r.a/rw;out.push({i:r.i,x,y:yy,w:rw,h:rh});yy+=rh}x+=rw;w-=rw}
    else{const rh=s/w;let xx=x;for(const r of row){const rw=r.a/rh;out.push({i:r.i,x:xx,y,w:rw,h:rh});xx+=rw}y+=rh;h-=rh}}
  return out}
function chartTreemap(d){const {items,total}=chartItems(d,60),W=1000,H=520,s=sv('svg',{viewBox:`0 0 ${W} ${H}`,style:'width:100%;height:auto;display:block'});
  squarify(items,0,0,W,H).forEach(r=>{const i=r.i,g=sv('g',{style:(i.other||i.inert)?'':'cursor:pointer',onclick:()=>openItem(i)},sv('title',{},tip(i,total)));
    g.append(sv('rect',{x:r.x,y:r.y,width:Math.max(0,r.w),height:Math.max(0,r.h),fill:icol(i),opacity:.88,stroke:'var(--card)','stroke-width':2}));
    if(i.mix&&r.h>14&&r.w>10){const t=Object.values(i.mix).reduce((q,v)=>q+v[1],0)||1;let xx=r.x;Object.entries(i.mix).sort((p,q)=>q[1][1]-p[1][1]).forEach(([k,v])=>{const ww=r.w*v[1]/t;g.append(sv('rect',{x:xx,y:r.y+r.h-6,width:ww,height:6,fill:kcol(k)}));xx+=ww})}
    if(r.w>90&&r.h>38){const n=Math.max(4,Math.floor(r.w/7.5)),nm=i.name.length>n?i.name.slice(0,n-1)+'…':i.name,st={fill:'#fff',stroke:'rgba(0,0,0,.5)','stroke-width':3,'paint-order':'stroke','font-size':13};
      g.append(sv('text',{...st,x:r.x+7,y:r.y+19,'font-weight':600},(i.dir?'📁 ':'')+nm),sv('text',{...st,x:r.x+7,y:r.y+35,'font-size':11},`${fmt(i.bytes)} · ${pctOf(i.bytes,total)}%`))}
    s.append(g)});return s}
const VIEWS=[['table','Table'],['bars','Bars'],['pie','Pie'],['treemap','Treemap']];
function viewSwitch(){return el('div',{class:'key'},el('span',{class:'mute'},'View:'),VIEWS.map(([v,l])=>el('button',{class:'chip'+(browse.view===v?' on':''),onclick:()=>{browse.view=v;pref('view',v);rBrowse()}},l)),
    el('button',{class:'chip'+(showFree?' on':''),style:'margin-left:14px',title:'Add the volume\'s free space to charts at the top of a drive',onclick:()=>{showFree=!showFree;pref('free',showFree?'1':'0');rBrowse()}},'Show free space'),el('span',{class:'mute',style:'margin-left:14px'},'Colour charts:'),[['item','by folder / item'],['type','by file type']].map(([m,l])=>el('button',{class:'chip'+(colorMode===m?' on':''),onclick:()=>{colorMode=m;pref('colour',m);rBrowse()}},l)))}
/* sunburst: whole drive from the centre outwards; the folder you are in is outlined */
function sunStatic(root,path,kind){const T=DATA.tree[root]||{},chain=path?path.split('/'):[],maxd=Math.min(8,Math.max(3,chain.length+2));
  const rowsOf=p=>(T[p]||[]).filter(c=>c.dir&&(!kind||(c.mix||{})[kind])).map(c=>({name:c.name,bytes:kind?c.mix[kind][1]:c.bytes,mix:c.mix})).sort((a,b)=>b.bytes-a.bytes);
  const total=(T['']||[]).filter(c=>!kind||(c.dir?(c.mix||{})[kind]:c.kind===kind)).reduce((s,c)=>s+(kind&&c.dir?c.mix[kind][1]:c.bytes),0),thr=total*0.02;
  const build=(parent,pb,level)=>{const kept=[];for(const r of rowsOf(parent)){const on=level<=chain.length&&parent===chain.slice(0,level-1).join('/')&&r.name===chain[level-1];if(on||(r.bytes>=thr&&kept.length<8))kept.push(r)}
    const kids=kept.map(r=>{const p=parent?parent+'/'+r.name:r.name;return {name:r.name,path:p,bytes:r.bytes,mix:r.mix,kids:level<maxd?build(p,r.bytes,level+1):[]}});
    const rest=pb-kept.reduce((s,r)=>s+r.bytes,0);if(rest>0&&kids.length)kids.push({name:'other',other:true,bytes:rest,kids:[]});return kids};
  return {indexed:true,total,depth:maxd,tree:{name:root,path:'',bytes:total,kids:build('',total,1)}}}
async function drawSun(){const w=$('#sunwrap');if(tab!=='Browse'||!sunOpen||!browse.root){w.hidden=true;return}
  w.hidden=false;const k=browse.kind||'';
  const D=STATIC?sunStatic(browse.root,browse.path,k):await get(`/api/sunburst?root=${enc(browse.root)}&path=${enc(browse.path)}&kind=${enc(k)}`);
  if(tab!=='Browse')return;
  if(!D.indexed||!D.total){w.replaceChildren(el('div',{class:'card mute'},D.indexed===false?['The sunburst needs the folder index. ',STATIC?null:el('button',{class:'btn sm',onclick:()=>runJob({cmd:'index',roots:[browse.root]})},'Build index for '+browse.root)]:'Nothing to show for this filter.'));return}
  const chain=browse.path?browse.path.split('/'):[],rings=D.depth,R=150,r0=34,cx=160,cy=160,rw=(R-r0)/rings;
  const s=sv('svg',{viewBox:'0 0 320 320',style:'width:300px;height:300px;flex:none'});
  const place=(n,a0,a1,level,ti)=>{if(!n.kids.length)return;let a=a0,k=0;for(const c of n.kids){const t=level===1?k++:ti;const da=(a1-a0)*c.bytes/n.bytes;if(da>0.004){
      const on=!c.other&&level<=chain.length&&c.path===chain.slice(0,level).join('/'),cur=on&&level===chain.length;
      s.append(sv('path',{d:arcPath(cx,cy,r0+(level-1)*rw,r0+level*rw-1,a,a+da),fill:sunCol(c,level,t),opacity:!chain.length||on?1:.5,stroke:cur?'var(--ink)':(on?'var(--ink)':'var(--card)'),'stroke-width':cur?2.5:(on?1.5:.7),style:c.other?'':'cursor:pointer',
        onclick:c.other?null:()=>{browse.path=c.path;browse.all=false;rBrowse()}},sv('title',{},`${c.name}\n${fmt(c.bytes)} · ${pctOf(c.bytes,D.total)}% of ${browse.root}`)));
      place(c,a,a+da,level+1,t)}a+=da}};
  place(D.tree,0,2*Math.PI,1,0);
  s.append(sv('circle',{cx,cy,r:r0-2,fill:'var(--card)',stroke:'var(--line)',style:'cursor:pointer',onclick:()=>{browse.path='';browse.all=false;rBrowse()}},sv('title',{},'Go to the top of '+browse.root)),
    sv('text',{x:cx,y:cy+4,'text-anchor':'middle','font-size':11,fill:'var(--ink)',style:'pointer-events:none'},browse.root.length>9?browse.root.slice(0,8)+'…':browse.root));
  const find=p=>{let n=D.tree;for(const c of (p?p.split('/'):[]).map((_,i,a)=>a.slice(0,i+1).join('/'))){n=(n.kids||[]).find(x=>x.path===c);if(!n)return null}return n};
  const spl=(S.space||{})[browse.root],lines=[spl?el('div',{style:'max-width:420px;margin-bottom:6px'},spaceBar(S.roots.find(r=>r.root===browse.root)||{bytes:0},spl,true)):null,el('div',{},el('b',{},browse.root),' ',fmt(D.total),el('span',{class:'mute'},browse.kind?` (${KIND_LABEL[browse.kind]} only)`:''))];
  chain.forEach((_,i)=>{const p=chain.slice(0,i+1).join('/'),n=find(p);if(n)lines.push(el('div',{style:`padding-left:${(i+1)*12}px;cursor:pointer`,onclick:()=>{browse.path=p;browse.all=false;rBrowse()}},'↳ ',chain[i],' ',el('span',{class:'mute'},`${fmt(n.bytes)} · ${pctOf(n.bytes,D.total)}% of the drive`)))});
  w.replaceChildren(el('div',{class:'card',style:'display:flex;gap:24px;align-items:center;flex-wrap:wrap;margin-bottom:0'},s,
    el('div',{style:'flex:1;min-width:260px'},el('h3',{},'Where you are'),...lines.filter(Boolean),el('p',{class:'mute'},'Centre is the top of the drive; each ring is one folder level deeper. Faded arcs are elsewhere; your path is outlined. Click any arc to jump there.'),colorMode==='item'?el('div',{class:'legend'},D.tree.kids.filter(c=>!c.other).map((c,k)=>el('span',{style:'cursor:pointer',title:'Go to '+c.name,onclick:()=>{browse.path=c.path;browse.all=false;rBrowse()}},el('span',{class:'sw',style:`background:${sunCol(c,1,k)}`}),`${c.name} ${fmt(c.bytes)}`))):legend(Object.entries(D.tree.kids.reduce((m,c)=>{if(c.mix)for(const[k,v]of Object.entries(c.mix))m[k]=(m[k]||0)+v[1];return m},{})).map(([kind,bytes])=>({kind,bytes})).sort((a,b)=>b.bytes-a.bytes))))) }

function sunBtn(){const b=$('#sunbtn');b.hidden=tab!=='Browse';b.textContent='Sunburst '+(sunOpen?'▾':'▸');b.onclick=()=>{sunOpen=!sunOpen;pref('sun',sunOpen?'1':'0');sunBtn();drawSun()}}
function nav(){$('#nav').replaceChildren(...TABS.map(t=>el('button',{class:t===tab?'on':'',onclick:()=>{tab=t;nav();render()}},t)))}
/* ---- colour key: one place defines what each file-type colour means ---- */
function swatch(k){return el('span',{class:'sw',style:`background:var(${COLORS[k]||'--other'})`})}
function key(active,pick){return el('div',{class:'key'},el('span',{class:'mute'},'File types:'),
  KINDS.map(k=>el('button',{class:'chip'+(active===k?' on':''),title:active===k?'Show all types':'Show only '+KIND_LABEL[k],onclick:()=>pick(active===k?null:k)},swatch(k),KIND_LABEL[k])),
  active?el('span',{class:'mute'},`showing only ${KIND_LABEL[active]} (click it again to clear)`):'')}
function kindBar(kinds,total){const s=el('div',{class:'stack'});kinds.forEach(k=>s.append(el('i',{title:`${KIND_LABEL[k.kind]||k.kind}: ${fmt(k.bytes)} (${num(k.files)} files)`,style:`width:${100*k.bytes/(total||1)}%;background:var(${COLORS[k.kind]||'--other'})`})));return s}
function legend(kinds,onpick){return el('div',{class:'legend'},kinds.map(k=>el('span',{style:onpick?'cursor:pointer':'',onclick:onpick?()=>onpick(k.kind):null,title:onpick?'Browse only '+KIND_LABEL[k.kind]:''},swatch(k.kind),`${KIND_LABEL[k.kind]||k.kind} ${fmt(k.bytes)}`)))}
function mixBar(mix,pct){const e=Object.entries(mix||{}).sort((a,b)=>b[1][1]-a[1][1]);const t=e.reduce((s,[,v])=>s+v[1],0)||1;
  return el('div',{class:'bar'},el('div',{style:`width:${pct}%`},e.map(([k,v])=>el('i',{title:`${KIND_LABEL[k]||k}: ${fmt(v[1])} (${num(v[0])} files)`,style:`width:${100*v[1]/t}%;background:var(${COLORS[k]||'--other'})`}))))}
function indexBanner(){const bad=S.roots.filter(r=>r.indexed===false);if(STATIC||!bad.length)return null;
  return el('div',{class:'card warn'},`Folder index not built for ${bad.map(r=>r.root).join(', ')}. Overview and Browse are slow until it is. `,el('button',{class:'btn sm',onclick:()=>runJob({cmd:'index',roots:bad.map(r=>r.root)})},'Build index now'))}
async function render(){
  rBrowse.c=null;setHere(null);sunBtn();if(tab!=='Browse')$('#sunwrap').hidden=true;view.replaceChildren(el('p',{class:'mute'},'Loading…'));
  if(!CFGJ)CFGJ=await get('/api/config');
  S=await get('/api/summary');
  ({Overview:rOverview,Browse:rBrowse,Duplicates:rDupes,Lightroom:rLr,Aperture:rAperture,Run:rRun})[tab]();
}
function rOverview(){
  const tot=S.roots.reduce((a,r)=>a+r.bytes,0),files=S.roots.reduce((a,r)=>a+r.files,0);
  const vols=S.volumes||[],freeAll=vols.reduce((a,v)=>a+v.free,0),totAll=vols.reduce((a,v)=>a+v.total,0),low=vols.filter(v=>v.configured&&v.total&&v.free/v.total<0.15);
  const out=[indexBanner(),el('div',{class:'row2'},el('div',{class:'big'},fmt(tot)),el('span',{class:'mute'},`${num(files)} files across ${S.roots.length} scanned roots`),
      vols.length?el('span',{class:'pill',title:'Free space across distinct volumes'},`${fmt(freeAll)} free of ${fmt(totAll)} on ${vols.length} volume${vols.length===1?'':'s'}`):null),
    (S.overlaps||[]).length?el('div',{class:'card warn'},S.overlaps.map(([a,b])=>el('div',{},'⚠ ',el('b',{},a),' is inside ',el('b',{},b),': its files are counted twice and appear as duplicates. Remove one of them from config.json.'))):null,
    volumesCard(),
    low.length?el('div',{class:'card warn'},low.map(v=>el('div',{},'⚠ ',el('b',{},v.roots.join(' + ')),` is ${Math.round(100*v.used/v.total)}% full with only ${fmt(v.free)} free`+(v.live?'':' (last seen)')))):null,
    key(null,k=>{browse={...browse,root:browse.root||(S.roots[0]||{}).root,path:'',kind:k};tab='Browse';nav();render()})];
  if(!S.roots.length)out.push(el('div',{class:'card'},'Nothing scanned yet. Go to ',el('b',{},'Run'),' and start a scan.'));
  const g=el('div',{class:'grid'});
  S.roots.forEach(r=>g.append(el('div',{class:'card'},el('h3',{},r.root),el('div',{class:'mute'},`${num(r.files)} files · scanned ${r.scanned}${r.mounted?'':' · not mounted now'}`),
    el('div',{class:'big'},fmt(r.bytes)),kindBar(r.kinds,r.bytes),legend(r.kinds,k=>{browse={root:r.root,path:'',kind:k,all:false,skip:true};tab='Browse';nav();render()}),spaceBar(r,(S.space||{})[r.root]),
    el('div',{class:'row2'},el('button',{class:'btn alt',onclick:()=>{browse={root:r.root,path:'',kind:null,all:false,skip:true};tab='Browse';nav();render()}},'Browse folders'),finderBtn({root:r.root,rel:''})))));
  S.unscanned.forEach(r=>g.append(el('div',{class:'card'},el('h3',{},r.root),el('div',{class:'mute'},r.mounted?'Not scanned yet':'Not mounted'),spaceBar({bytes:0},(S.space||{})[r.root]))));
  out.push(g);view.replaceChildren(...out.filter(Boolean))}
/* ---- Browse: every folder is reachable; colour = file type ---- */
async function rBrowse(){
  if(!browse.root)browse.root=(S.roots[0]||{}).root;
  if(!browse.root){view.replaceChildren(el('p',{class:'mute'},'Scan something first.'));return}
  const url=p=>`/api/tree?root=${enc(browse.root)}&path=${enc(p)}&kind=${enc(browse.kind||'')}&limit=${browse.all?0:500}`;
  if(!browse.view)browse.view=pref('view')||'table';
  const ckey=[browse.root,browse.path,browse.kind||'',browse.all,browse.skip].join('|');
  let d,hops=[];
  if(rBrowse.c&&rBrowse.c.keys.includes(ckey)){d=rBrowse.c.d;hops=rBrowse.c.hops;browse.path=rBrowse.c.path}   /* switching view re-uses the data */
  else{view.style.opacity=.5;
    try{d=await get(url(browse.path));
      if(browse.skip&&!browse.kind){while(d.children.length===1&&d.children[0].dir&&hops.length<12){const nx=d.children[0].name,np=(browse.path?browse.path+'/':'')+nx;
        const n2=await get(url(np));if(n2.truncated||!n2.children.length)break;hops.push(nx);browse.path=np;d=n2}}}
    finally{view.style.opacity=1}
    rBrowse.c={keys:[ckey,[browse.root,browse.path,browse.kind||'',browse.all,browse.skip].join('|')],d,hops,path:browse.path}}
  const sel=el('select',{onchange:e=>{browse={...browse,root:e.target.value,path:'',all:false};rBrowse()}},S.roots.map(r=>el('option',{value:r.root,...(r.root===browse.root?{selected:''}:{})},r.root)));
  const skipBox=el('input',{type:'checkbox',...(browse.skip?{checked:''}:{}),onchange:e=>{browse.skip=e.target.checked;rBrowse()}});
  const parts=browse.path?browse.path.split('/'):[];
  const crumbs=el('div',{class:'crumbs'},el('a',{onclick:()=>{browse.path='';browse.all=false;rBrowse()}},browse.root),...parts.map((p,i)=>[' / ',el('a',{onclick:()=>{browse.path=parts.slice(0,i+1).join('/');browse.all=false;rBrowse()}},p)]));
  const max=Math.max(1,...d.children.map(c=>c.bytes));
  const t=el('table',{},el('tr',{},el('th',{},'Name'),el('th',{class:'n'},'Files'),el('th',{class:'n'},browse.kind?`${KIND_LABEL[browse.kind]} size`:'Size'),el('th',{class:'n'},'Newest')),
    d.children.map(c=>el('tr',{class:c.dir?'row':'',onclick:c.dir?()=>{browse.path=(browse.path?browse.path+'/':'')+c.name;browse.all=false;rBrowse()}:null},
      el('td',{},c.dir?'📁 ':[swatch(c.kind||'other'),''],c.name,c.dir?mixBar(c.mix,100*c.bytes/max):mixBar({[c.kind||'other']:[1,c.bytes]},100*c.bytes/max)),
      el('td',{class:'n'},num(c.files)),el('td',{class:'n'},fmt(c.bytes)),el('td',{class:'n mute'},c.newest))));
  setHere({root:browse.root,rel:browse.path,label:browse.root+(browse.path?'/'+browse.path:'')});
  const R=S.roots.find(r=>r.root===browse.root)||{};
  const folderBytes=d.children.reduce((a,c)=>a+c.bytes,0);
  const notes=[d.truncated?el('p',{class:'mute'},`The static report stops at ${DATA.depth} folder levels. Use storage_audit_ui.py for every level.`):null,
    hops.length?el('p',{class:'mute'},`Skipped through single-folder levels: ${hops.join(' / ')}`):null,
    d.total>d.children.length?el('p',{},`Showing the biggest ${num(d.children.length)} of ${num(d.total)} items. `,el('button',{class:'btn sm',onclick:()=>{browse.all=true;rBrowse()}},'Show all')):null,
    !STATIC&&R.indexed===false?el('p',{class:'mute'},'Folder index not built for this drive, so this is slow and cannot filter by type. ',el('button',{class:'btn sm',onclick:()=>runJob({cmd:'index',roots:[browse.root]})},'Build index')):null];
  view.replaceChildren(...[!STATIC&&R.indexed===false?el('div',{class:'card warn'},el('b',{},'Build the folder index to unlock file-type colours, per-folder mix bars and the sunburst. '),'It takes a minute or two per big drive and shows its progress at the top of the page. ',el('button',{class:'btn sm',onclick:()=>runJob({cmd:'index',roots:[browse.root]})},'Build index for '+browse.root)):null,el('div',{class:'row2'},sel,el('label',{},skipBox,' Skip through single-folder chains')),el('div',{style:'max-width:520px'},spaceBar(R,(S.space||{})[browse.root],true)),el('div',{class:'row2'},el('button',{class:'btn sm alt',title:'Check which drive has room for this folder and how long a copy would take',onclick:()=>{plan={bytes:browse.path?folderBytes:(R.bytes||folderBytes),label:browse.root+(browse.path?'/'+browse.path:''),root:browse.root};tab='Overview';nav();render()}},'Plan a move of this folder'))].filter(Boolean),
    viewSwitch(),key(browse.kind,k=>{browse.kind=k;browse.all=false;rBrowse()}),crumbs,...notes.filter(Boolean),el('div',{class:'card'},(()=>{try{return browse.view==='bars'?chartBars(d):browse.view==='pie'?chartPie(d):browse.view==='treemap'?chartTreemap(d):t}catch(e){console.error(e);return el('p',{class:'mute'},'Could not draw this view: '+e.message)}})()));drawSun().catch(e=>console.error(e))}
async function rDupes(){
  const d=await get('/api/dupes');
  const head=el('div',{class:'row2'},el('div',{class:'big'},fmt(d.reclaimable)),el('span',{class:'mute'},`reclaimable across ${num(d.groups)} groups · ${d.verified?'SHA-256 verified':'partial-hash match, run dupes with Full verify to confirm'}`));
  if(!d.groups){view.replaceChildren(head,el('div',{class:'card'},'No duplicate groups yet. On the Run tab, scan then run Find duplicates.'));return}
  const t=el('table',{},el('tr',{},el('th',{class:'n'},'Size'),el('th',{class:'n'},'Copies'),el('th',{class:'n'},'Drives'),el('th',{},'Locations (click to copy a path)')),
    d.top.map(g=>el('tr',{},el('td',{class:'n'},fmt(g.size)),el('td',{class:'n'},g.copies),el('td',{class:'n'},g.volumes),
      el('td',{},g.locations.map(l=>{const full=(CFGJ.roots[l.root]||'')+'/'+l.rel;return el('div',{style:'display:flex;gap:8px;align-items:baseline'},el('span',{class:'loc',title:'Copy full path',onclick:()=>navigator.clipboard&&navigator.clipboard.writeText(full)},`${l.root}: ${l.rel}`),finderBtn({abs:full},STATIC?null:'Show'))})))));
  view.replaceChildren(head,el('div',{class:'card'},t))}
/* ---- Lightroom: every catalogue found, read each one ---- */
async function rLr(){
  const [c,o]=await Promise.all([get('/api/catalogues'),get('/api/orphans?catalogue='+enc(lrScope))]);
  const main=c.catalogues.filter(x=>!x.backup),back=c.catalogues.filter(x=>x.backup);
  const row=x=>el('tr',{},el('td',{},el('b',{},x.name),el('div',{class:'loc'},x.where)),el('td',{class:'n'},x.size==null?'':fmt(x.size)),el('td',{class:'n mute'},x.modified||''),
    el('td',{},x.loaded?[el('span',{class:'tag good'},'read '+x.loaded),el('div',{class:'mute'},`${num(x.photos)} photos · ${num(x.on_scanned)} on scanned drives`),
      x.missing?el('span',{class:'tag bad'},`${num(x.missing)} missing from disk`):null]:el('span',{class:'tag'},'not read yet'),x.found?null:el('span',{class:'tag'},'outside scanned drives')),
    el('td',{style:'white-space:nowrap'},finderBtn({abs:x.path},STATIC?null:'Show'),' ',STATIC?null:el('button',{class:'btn sm alt',onclick:()=>runJob({cmd:'lrcat',catalogues:[x.path]})},x.loaded?'Re-read':'Read')));
  const tbl=rows=>el('table',{},el('tr',{},el('th',{},'Catalogue'),el('th',{class:'n'},'Size'),el('th',{class:'n'},'Modified'),el('th',{},'Status'),el('th',{},'')),rows.map(row));
  const out=[el('div',{class:'row2'},el('div',{class:'big'},num(main.length)),el('span',{class:'mute'},`Lightroom catalogue${main.length===1?'':'s'} found${back.length?` (+${back.length} backups)`:''}`),
    !STATIC&&main.length?el('button',{class:'btn',onclick:()=>runJob({cmd:'lrcat',all:true})},'Read all catalogues'):null)];
  if(!c.catalogues.length)out.push(el('div',{class:'card'},c.indexed_roots.length?'No .lrcat files were found on the scanned drives. Add the folder holding your catalogues (for example your local Lightroom folder) as a root in config.json.':'Scan and index first, then your Lightroom catalogues are listed here.'));
  else{out.push(el('div',{class:'card'},tbl(main)));
    if(back.length)out.push(el('details',{},el('summary',{class:'mute'},`${back.length} catalogue backups`),el('div',{class:'card'},tbl(back))))}
  const loaded=c.catalogues.filter(x=>x.loaded);
  if(!o.available)out.push(el('p',{class:'mute'},'Read a catalogue to see which photos on your drives it does not know about.'));
  else{const scope=el('select',{onchange:e=>{lrScope=e.target.value;rLr()}},el('option',{value:''},'any loaded catalogue'),loaded.map(x=>el('option',{value:x.path,...(x.path===lrScope?{selected:''}:{})},x.name+' · '+x.where)));
    out.push(el('h3',{},'Photos on disk not in the selected catalogue'),el('div',{class:'row2'},el('span',{class:'big'},num(o.total)),el('span',{class:'mute'},'photos not referenced by'),scope),
      el('div',{class:'card'},el('table',{},el('tr',{},el('th',{},'Drive'),el('th',{},'Folder'),el('th',{class:'n'},'Files'),el('th',{class:'n'},'Size')),
        o.rows.map(r=>el('tr',{},el('td',{},r.root),el('td',{},r.folder),el('td',{class:'n'},num(r.files)),el('td',{class:'n'},fmt(r.bytes)))))))}
  view.replaceChildren(...out)}
/* ---- Aperture: browse libraries even though the app is discontinued ---- */
const APC={masters:'--photo',previews:'--archive',thumbnails:'--system',database:'--audio',other:'--other'};
function apBar(b){const t=b.total||1;return el('div',{class:'stack'},['masters','previews','thumbnails','database','other'].map(k=>el('i',{title:`${k}: ${fmt(b[k])}`,style:`width:${100*b[k]/t}%;background:var(${APC[k]})`})))}
function apKey(){return el('div',{class:'key'},el('span',{class:'mute'},'Library contents:'),Object.keys(APC).map(k=>el('span',{class:'chip',style:'cursor:default'},el('span',{class:'sw',style:`background:var(${APC[k]})`}),k[0].toUpperCase()+k.slice(1))))}
function covBar(n,c){return el('div',{class:'cov',title:`${num(c)} of ${num(n)} also in a loaded Lightroom catalogue`},el('i',{style:`width:${n?100*c/n:0}%`}))}
async function rAperture(){
  if(ap.lib)return rApLib();
  const d=await get('/api/aperture'),L=d.libraries;
  const readAll=!STATIC&&L.some(l=>!l.loaded)?el('button',{class:'btn',onclick:()=>runJob({cmd:'aperture'})},'Read all libraries'):null;
  if(!L.length){view.replaceChildren(el('div',{class:'card'},'No Aperture libraries found yet. Scan and index first; libraries are recognised by their Database/apdb/Library.apdb.'));return}
  const tot=L.reduce((a,l)=>a+((l.bytes||{}).total||0),0),masters=L.reduce((a,l)=>a+(l.masters||0),0);
  const cards=L.map(l=>el('div',{class:'card'},el('div',{class:'row2'},el('h3',{style:'margin:0'},l.loaded?el('a',{style:'color:var(--acc);cursor:pointer',onclick:()=>{ap={lib:l.path,name:l.name,trail:[],cur:null};rAperture()}},l.name):l.name),
      finderBtn({abs:l.path},STATIC?null:'Show'),l.loaded?null:el('span',{class:'tag'},'not read yet'),STATIC||l.loaded?null:el('button',{class:'btn sm',onclick:()=>runJob({cmd:'aperture',libraries:[l.path]})},'Read library')),
    el('div',{class:'loc'},(l.root||'')+': '+(l.rel||'')),
    l.bytes?[el('div',{class:'big'},fmt(l.bytes.total)),apBar(l.bytes),el('div',{class:'legend'},['masters','previews','thumbnails','database'].map(k=>el('span',{},el('span',{class:'sw',style:`background:var(${APC[k]})`}),`${k} ${fmt(l.bytes[k])}`)))]:null,
    l.loaded?el('div',{style:'margin-top:8px'},`${num(l.masters)} photos${l.from?` · ${l.from} to ${l.to}`:''}`,
      l.referenced?el('span',{class:'tag'},`${num(l.referenced)} referenced`):null,l.absent?el('span',{class:'tag bad'},`${num(l.absent)} masters not found on disk`):null,l.trashed?el('span',{class:'tag'},`${num(l.trashed)} in trash`):null,
      d.lightroom_loaded&&l.covered!==undefined?[el('div',{class:'mute'},`${num(l.covered)} of ${num(l.masters)} (${Math.round(100*l.covered/(l.masters||1))}%) also in a loaded Lightroom catalogue`),covBar(l.masters,l.covered)]:null):null));
  view.replaceChildren(el('div',{class:'row2'},el('div',{class:'big'},num(L.length)),el('span',{class:'mute'},`Aperture libraries · ${fmt(tot)} on disk${masters?` · ${num(masters)} photos read`:''}`),readAll),
    apKey(),el('div',{class:'grid'},cards),d.lightroom_loaded?null:el('p',{class:'mute'},'Read your Lightroom catalogues (Lightroom tab) to see how much of each library is already in Lightroom.'))}
async function rApLib(){
  const parent=ap.trail.length?ap.trail[ap.trail.length-1].uuid:'';
  const d=await get(`/api/aperture/tree?lib=${enc(ap.lib)}&parent=${enc(parent)}`);
  const crumbs=el('div',{class:'crumbs'},el('a',{onclick:()=>{ap={lib:null,trail:[],cur:null};rAperture()}},'Aperture libraries'),' / ',
    el('a',{onclick:()=>{ap.trail=[];ap.cur=null;rAperture()}},ap.name),...ap.trail.map((t,i)=>[' / ',el('a',{onclick:()=>{ap.trail=ap.trail.slice(0,i+1);ap.cur=t;rAperture()}},t.name)]));
  setHere({abs:ap.lib,label:ap.name});
  const max=Math.max(1,...d.children.map(c=>c.bytes));
  const lrOn=d.lightroom_loaded;
  const kids=d.children.length?el('div',{class:'card'},el('table',{},el('tr',{},el('th',{},'Folder / project'),el('th',{class:'n'},'Photos'),el('th',{class:'n'},'Size'),el('th',{class:'n'},'From'),el('th',{class:'n'},'To'),lrOn?el('th',{},'In Lightroom'):null),
    d.children.map(c=>el('tr',{class:'row',onclick:()=>{ap.trail=[...ap.trail,c];ap.cur=c;rAperture()}},
      el('td',{},(c.kids?'📁 ':'📷 ')+c.name,el('div',{class:'bar'},el('div',{style:`width:${100*c.bytes/max}%`},el('i',{style:'width:100%;background:var(--photo)'})))),
      el('td',{class:'n'},num(c.masters)),el('td',{class:'n'},fmt(c.bytes)),el('td',{class:'n mute'},c.from),el('td',{class:'n mute'},c.to),
      lrOn?el('td',{},`${c.masters?Math.round(100*c.covered/c.masters):0}%`,covBar(c.masters,c.covered)):null)))):null;
  const out=[crumbs,kids];
  if(ap.cur&&ap.cur.direct>0){
    if(STATIC)out.push(el('p',{class:'mute'},'Photo lists are available in the live UI (storage_audit_ui.py).'));
    else{const m=await get(`/api/aperture/masters?lib=${enc(ap.lib)}&project=${enc(ap.cur.uuid)}&limit=500`);
      out.push(el('h3',{},`${num(m.total)} photos directly in ${ap.cur.name}`),el('div',{class:'card'},el('table',{},el('tr',{},el('th',{},'File'),el('th',{class:'n'},'Size'),el('th',{class:'n'},'Date'),el('th',{},'Status')),
        m.masters.map(x=>el('tr',{},el('td',{},x.name),el('td',{class:'n'},fmt(x.size)),el('td',{class:'n mute'},x.date),
          el('td',{},m.lightroom_loaded?el('span',{class:'tag '+(x.in_lightroom?'good':'bad')},x.in_lightroom?'in Lightroom':'not in Lightroom'):null,
            x.referenced?el('span',{class:'tag'},'referenced'):null,x.missing?el('span',{class:'tag bad'},'missing'):null,x.trashed?el('span',{class:'tag'},'trash'):null))))),
        m.total>m.masters.length?el('p',{class:'mute'},`Showing the first ${num(m.masters.length)} by date.`):null)}}
  if(!d.children.length&&!(ap.cur&&ap.cur.direct>0))out.push(el('p',{class:'mute'},'Nothing in here.'));
  view.replaceChildren(...out.filter(Boolean))}
function rRun(){
  const checks=Object.keys(CFGJ.roots).map(r=>el('label',{style:'margin-right:12px'},el('input',{type:'checkbox',value:r,checked:''})," "+r));
  const cat=el('input',{type:'text',size:'44',placeholder:'/path/to/Catalogue.lrcat'});const full=el('input',{type:'checkbox'});const log=el('pre',{id:'log'},'No job run yet.');
  const sel=()=>checks.map(c=>c.querySelector('input')).filter(i=>i.checked).map(i=>i.value);
  view.replaceChildren(...[indexBanner(),
    el('div',{class:'card'},el('h3',{},'1. Scan drives'),el('div',{class:'mute'},'Catalogues every file (read-only) and builds the folder index. Large drives take a while. Stop the UI scan buttons if a command-line scan is running.'),el('div',{class:'row2'},checks),
      el('div',{class:'row2'},el('button',{class:'btn',onclick:()=>runJob({cmd:'scan',roots:sel()})},'Scan selected'),el('button',{class:'btn alt',onclick:()=>runJob({cmd:'index',roots:sel()})},'Rebuild index only'))),
    el('div',{class:'card'},el('h3',{},'2. Lightroom catalogues'),el('div',{class:'mute'},'Reads catalogues found on the scanned drives, or one you name.'),
      el('div',{class:'row2'},el('button',{class:'btn',onclick:()=>runJob({cmd:'lrcat',all:true})},'Read all found catalogues'),cat,el('button',{class:'btn alt',onclick:()=>runJob({cmd:'lrcat',catalogues:[cat.value]})},'Read this one'))),
    el('div',{class:'card'},el('h3',{},'3. Aperture libraries'),el('div',{class:'mute'},'Reads projects, folders and every photo record from each Library.apdb (read-only).'),
      el('div',{class:'row2'},el('button',{class:'btn',onclick:()=>runJob({cmd:'aperture'})},'Read all found libraries'))),
    el('div',{class:'card'},el('h3',{},'4. Find duplicates'),el('div',{class:'mute'},'Compares files of equal size. Full verify reads every candidate in full, which is slow on network drives.'),
      el('div',{class:'row2'},el('label',{},full,' Full verify (SHA-256)'),el('button',{class:'btn',onclick:()=>runJob({cmd:'dupes',full:full.checked})},'Find duplicates'))),
    el('div',{class:'card'},el('h3',{},'5. HTML report'),el('div',{class:'mute'},'Writes the static index.html to '+CFGJ.report_dir),
      el('div',{class:'row2'},el('button',{class:'btn alt',onclick:async()=>{const r=await post('/api/report',{});alert(r.path||r.error)}},'Write report'))),
    el('h3',{},'Job log'),log].filter(Boolean));poll()}
let timer=null;
async function poll(){clearTimeout(timer);const j=await get('/api/job');jobProg=j;const p=$('#jobstate');p.hidden=!j.title;p.textContent=j.running?`running: ${j.title}`:(j.title?`${j.title}: ${j.code===0?'done':'exit '+j.code}`:'');
  const l=$('#log');if(l){l.textContent=j.log.join('\n')||'No job run yet.';l.scrollTop=l.scrollHeight}
  if(j.running){window._wasRunning=true;timer=setTimeout(poll,800)}
  else if(window._wasRunning){window._wasRunning=false;const ok=j.code===0;flash={msg:ok?`✓ ${j.title} finished`:`✗ ${j.title} failed (exit ${j.code}); see the Run tab log`,bad:!ok};setTimeout(()=>{flash=null;showProg()},ok?4000:12000);if(tab!=='Run')render()}
  showProg()}
nav();render();poll();
</script></html>
"""


def main():
    global CFG, CONFIG_PATH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="default: see storage_audit.default_config_path()")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    a.port_explicit = any(x == "--port" or x.startswith("--port=") for x in sys.argv[1:])
    CONFIG_PATH = os.path.expanduser(a.config) if a.config else sa.default_config_path()
    CFG = sa.load_config(CONFIG_PATH)
    srv = None
    # port busy (often an earlier copy of this UI): try the next few, unless one was asked for
    for port in ([a.port] if a.port_explicit else range(a.port, a.port + 20)):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
            print(f"Port {port} is in use" + ("" if a.port_explicit else ", trying the next one"))
    if srv is None:
        sys.exit(f"No free port available. Stop the other process (lsof -nP -iTCP:{a.port} -sTCP:LISTEN) or pass --port N.")
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Storage audit UI: {url}  (Ctrl-C to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()

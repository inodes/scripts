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
JOB = {"running": False, "title": "", "log": [], "code": None}
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
    return {
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
        JOB.update(running=True, title=title, log=[], code=None)

    def work():
        try:
            p = subprocess.Popen([sys.executable, os.path.join(HERE, "storage_audit.py"), "--config", CONFIG_PATH] + argv,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            buf = ""
            for ch in iter(lambda: p.stdout.read(1), ""):
                if ch in "\r\n":
                    if buf:
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
            JOB["running"] = False
    threading.Thread(target=work, daemon=True).start()
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
                return self._send(200, {k: JOB[k] for k in ("running", "title", "log", "code")})
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
nav button.on{color:var(--ink);border-color:var(--acc)}main{padding:24px;max-width:1240px;margin:0 auto}
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
</style>
<header><h1>Storage audit</h1><nav id="nav"></nav><span style="flex:1"></span><span id="jobstate" class="pill" hidden></span></header>
<main id="view"></main>
<script>
const STATIC=typeof DATA!=='undefined';
const KINDS=['photo','video','audio','document','archive','app/system','lightroom','other'];
const KIND_LABEL={photo:'Photo',video:'Video',audio:'Audio',document:'Document',archive:'Archive','app/system':'App / system',lightroom:'Lightroom',other:'Other'};
const COLORS={photo:'--photo',video:'--video',audio:'--audio',document:'--document',archive:'--archive','app/system':'--system',lightroom:'--lightroom',other:'--other'};
const TABS=STATIC?['Overview','Browse','Duplicates','Lightroom','Aperture']:['Overview','Browse','Duplicates','Lightroom','Aperture','Run'];
let tab='Overview',S=null,CFGJ=null,browse={root:null,path:'',kind:null,all:false,skip:true},ap={lib:null,trail:[],cur:null},lrScope='';
const $=s=>document.querySelector(s),view=$('#view'),enc=encodeURIComponent;
const fmt=b=>{const u=['B','KB','MB','GB','TB'];let i=0;b=b||0;while(b>=1024&&i<4){b/=1024;i++}return (i?b.toFixed(1):b)+' '+u[i]};
const num=n=>(n||0).toLocaleString('en-AU');
function el(t,a={},...k){const e=document.createElement(t);for(const[x,v]of Object.entries(a)){if(x==='class')e.className=v;else if(x.startsWith('on'))e[x]=v;else e.setAttribute(x,v)}for(const c of k.flat())if(c!==null&&c!==undefined&&c!==false)e.append(c instanceof Node?c:document.createTextNode(c));return e}
const get=u=>{if(!STATIC)return fetch(u).then(r=>r.json());
  const x=new URL(u,'http://x'),q=Object.fromEntries(x.searchParams),P=x.pathname,R=v=>Promise.resolve(v);
  if(P==='/api/summary')return R(DATA.summary);if(P==='/api/config')return R(DATA.config);if(P==='/api/dupes')return R(DATA.dupes);
  if(P==='/api/orphans')return R(DATA.orphans);if(P==='/api/catalogues')return R(DATA.catalogues);if(P==='/api/aperture')return R(DATA.aperture);
  if(P==='/api/job')return R({running:false,title:'',log:[],code:null});
  if(P==='/api/aperture/tree')return R({children:((DATA.ap_tree||{})[q.lib]||{})[q.parent||'']||[],lightroom_loaded:DATA.aperture.lightroom_loaded});
  if(P==='/api/aperture/masters')return R({total:0,masters:[],static:true});
  if(P==='/api/tree'){const all=(DATA.tree[q.root]||{})[q.path||''];
    if(!all)return R({children:[],total:0,truncated:!!q.path&&q.path.split('/').length>=DATA.depth});
    let c=all;if(q.kind)c=all.filter(i=>i.dir?(i.mix||{})[q.kind]:i.kind===q.kind).map(i=>i.dir?{...i,files:i.mix[q.kind][0],bytes:i.mix[q.kind][1]}:i).sort((a,b)=>b.bytes-a.bytes);
    return R({children:c,total:c.length,indexed:true})}
  return R({})};
const post=(u,b)=>fetch(u,{method:'POST',headers:{'X-Audit':'1','Content-Type':'application/json'},body:JSON.stringify(b)}).then(async r=>({ok:r.ok,...await r.json()}));
const runJob=b=>post('/api/run',b).then(r=>{if(!r.ok)alert(r.error);poll()});
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
  view.replaceChildren(el('p',{class:'mute'},'Loading…'));
  if(!CFGJ)CFGJ=await get('/api/config');
  S=await get('/api/summary');
  ({Overview:rOverview,Browse:rBrowse,Duplicates:rDupes,Lightroom:rLr,Aperture:rAperture,Run:rRun})[tab]();
}
function rOverview(){
  const tot=S.roots.reduce((a,r)=>a+r.bytes,0),files=S.roots.reduce((a,r)=>a+r.files,0);
  const out=[indexBanner(),el('div',{class:'row2'},el('div',{class:'big'},fmt(tot)),el('span',{class:'mute'},`${num(files)} files across ${S.roots.length} scanned roots`)),
    key(null,k=>{browse={...browse,root:browse.root||(S.roots[0]||{}).root,path:'',kind:k};tab='Browse';nav();render()})];
  if(!S.roots.length)out.push(el('div',{class:'card'},'Nothing scanned yet. Go to ',el('b',{},'Run'),' and start a scan.'));
  const g=el('div',{class:'grid'});
  S.roots.forEach(r=>g.append(el('div',{class:'card'},el('h3',{},r.root),el('div',{class:'mute'},`${num(r.files)} files · scanned ${r.scanned}${r.mounted?'':' · not mounted now'}`),
    el('div',{class:'big'},fmt(r.bytes)),kindBar(r.kinds,r.bytes),legend(r.kinds,k=>{browse={root:r.root,path:'',kind:k,all:false,skip:true};tab='Browse';nav();render()}),
    el('div',{class:'row2'},el('button',{class:'btn alt',onclick:()=>{browse={root:r.root,path:'',kind:null,all:false,skip:true};tab='Browse';nav();render()}},'Browse folders')))));
  S.unscanned.forEach(r=>g.append(el('div',{class:'card'},el('h3',{},r.root),el('div',{class:'mute'},r.mounted?'Not scanned yet':'Not mounted'))));
  out.push(g);view.replaceChildren(...out.filter(Boolean))}
/* ---- Browse: every folder is reachable; colour = file type ---- */
async function rBrowse(){
  if(!browse.root)browse.root=(S.roots[0]||{}).root;
  if(!browse.root){view.replaceChildren(el('p',{class:'mute'},'Scan something first.'));return}
  const url=p=>`/api/tree?root=${enc(browse.root)}&path=${enc(p)}&kind=${enc(browse.kind||'')}&limit=${browse.all?0:500}`;
  let d=await get(url(browse.path));
  const hops=[];
  if(browse.skip&&!browse.kind){while(d.children.length===1&&d.children[0].dir&&hops.length<12){const nx=d.children[0].name,np=(browse.path?browse.path+'/':'')+nx;
    const n2=await get(url(np));if(n2.truncated||!n2.children.length)break;hops.push(nx);browse.path=np;d=n2}}
  const sel=el('select',{onchange:e=>{browse={...browse,root:e.target.value,path:'',all:false};rBrowse()}},S.roots.map(r=>el('option',{value:r.root,...(r.root===browse.root?{selected:''}:{})},r.root)));
  const skipBox=el('input',{type:'checkbox',...(browse.skip?{checked:''}:{}),onchange:e=>{browse.skip=e.target.checked;rBrowse()}});
  const parts=browse.path?browse.path.split('/'):[];
  const crumbs=el('div',{class:'crumbs'},el('a',{onclick:()=>{browse.path='';browse.all=false;rBrowse()}},browse.root),...parts.map((p,i)=>[' / ',el('a',{onclick:()=>{browse.path=parts.slice(0,i+1).join('/');browse.all=false;rBrowse()}},p)]));
  const max=Math.max(1,...d.children.map(c=>c.bytes));
  const t=el('table',{},el('tr',{},el('th',{},'Name'),el('th',{class:'n'},'Files'),el('th',{class:'n'},browse.kind?`${KIND_LABEL[browse.kind]} size`:'Size'),el('th',{class:'n'},'Newest')),
    d.children.map(c=>el('tr',{class:c.dir?'row':'',onclick:c.dir?()=>{browse.path=(browse.path?browse.path+'/':'')+c.name;browse.all=false;rBrowse()}:null},
      el('td',{},c.dir?'📁 ':[swatch(c.kind||'other'),''],c.name,c.dir?mixBar(c.mix,100*c.bytes/max):mixBar({[c.kind||'other']:[1,c.bytes]},100*c.bytes/max)),
      el('td',{class:'n'},num(c.files)),el('td',{class:'n'},fmt(c.bytes)),el('td',{class:'n mute'},c.newest))));
  const R=S.roots.find(r=>r.root===browse.root)||{};
  const notes=[d.truncated?el('p',{class:'mute'},`The static report stops at ${DATA.depth} folder levels. Use storage_audit_ui.py for every level.`):null,
    hops.length?el('p',{class:'mute'},`Skipped through single-folder levels: ${hops.join(' / ')}`):null,
    d.total>d.children.length?el('p',{},`Showing the biggest ${num(d.children.length)} of ${num(d.total)} items. `,el('button',{class:'btn sm',onclick:()=>{browse.all=true;rBrowse()}},'Show all')):null,
    !STATIC&&R.indexed===false?el('p',{class:'mute'},'Folder index not built for this drive, so this is slow and cannot filter by type. ',el('button',{class:'btn sm',onclick:()=>runJob({cmd:'index',roots:[browse.root]})},'Build index')):null];
  view.replaceChildren(el('div',{class:'row2'},sel,el('label',{},skipBox,' Skip through single-folder chains')),
    key(browse.kind,k=>{browse.kind=k;browse.all=false;rBrowse()}),crumbs,...notes.filter(Boolean),el('div',{class:'card'},t))}
async function rDupes(){
  const d=await get('/api/dupes');
  const head=el('div',{class:'row2'},el('div',{class:'big'},fmt(d.reclaimable)),el('span',{class:'mute'},`reclaimable across ${num(d.groups)} groups · ${d.verified?'SHA-256 verified':'partial-hash match, run dupes with Full verify to confirm'}`));
  if(!d.groups){view.replaceChildren(head,el('div',{class:'card'},'No duplicate groups yet. On the Run tab, scan then run Find duplicates.'));return}
  const t=el('table',{},el('tr',{},el('th',{class:'n'},'Size'),el('th',{class:'n'},'Copies'),el('th',{class:'n'},'Drives'),el('th',{},'Locations (click to copy a path)')),
    d.top.map(g=>el('tr',{},el('td',{class:'n'},fmt(g.size)),el('td',{class:'n'},g.copies),el('td',{class:'n'},g.volumes),
      el('td',{},g.locations.map(l=>{const full=(CFGJ.roots[l.root]||'')+'/'+l.rel;return el('div',{class:'loc',title:'Copy full path',onclick:()=>navigator.clipboard&&navigator.clipboard.writeText(full)},`${l.root}: ${l.rel}`)})))));
  view.replaceChildren(head,el('div',{class:'card'},t))}
/* ---- Lightroom: every catalogue found, read each one ---- */
async function rLr(){
  const [c,o]=await Promise.all([get('/api/catalogues'),get('/api/orphans?catalogue='+enc(lrScope))]);
  const main=c.catalogues.filter(x=>!x.backup),back=c.catalogues.filter(x=>x.backup);
  const row=x=>el('tr',{},el('td',{},el('b',{},x.name),el('div',{class:'loc'},x.where)),el('td',{class:'n'},x.size==null?'':fmt(x.size)),el('td',{class:'n mute'},x.modified||''),
    el('td',{},x.loaded?[el('span',{class:'tag good'},'read '+x.loaded),el('div',{class:'mute'},`${num(x.photos)} photos · ${num(x.on_scanned)} on scanned drives`),
      x.missing?el('span',{class:'tag bad'},`${num(x.missing)} missing from disk`):null]:el('span',{class:'tag'},'not read yet'),x.found?null:el('span',{class:'tag'},'outside scanned drives')),
    el('td',{},STATIC?null:el('button',{class:'btn sm alt',onclick:()=>runJob({cmd:'lrcat',catalogues:[x.path]})},x.loaded?'Re-read':'Read')));
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
      l.loaded?null:el('span',{class:'tag'},'not read yet'),STATIC||l.loaded?null:el('button',{class:'btn sm',onclick:()=>runJob({cmd:'aperture',libraries:[l.path]})},'Read library')),
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
async function poll(){clearTimeout(timer);const j=await get('/api/job');const p=$('#jobstate');p.hidden=!j.title;p.textContent=j.running?`running: ${j.title}`:(j.title?`${j.title}: ${j.code===0?'done':'exit '+j.code}`:'');
  const l=$('#log');if(l){l.textContent=j.log.join('\n')||'No job run yet.';l.scrollTop=l.scrollHeight}
  if(j.running)timer=setTimeout(poll,1000);else if(window._wasRunning){window._wasRunning=false;if(tab!=='Run')render()}
  window._wasRunning=j.running}
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

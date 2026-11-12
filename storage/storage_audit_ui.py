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
import argparse, errno, json, os, subprocess, sys, threading, webbrowser
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import storage_audit as sa  # noqa: E402

CFG = None
CONFIG_PATH = None
JOB = {"running": False, "title": "", "log": [], "code": None}
JOB_LOCK = threading.Lock()


def query(sql, args=()):
    con = sa.db(CFG)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def api_summary():
    scans = query("SELECT root, finished, nfiles, nbytes FROM scans ORDER BY nbytes DESC")
    kinds = query("SELECT root, kind, COUNT(*), SUM(size) FROM files GROUP BY root, kind")
    by_root = defaultdict(list)
    for r, k, n, b in kinds:
        by_root[r].append({"kind": k, "files": n, "bytes": b})
    for v in by_root.values():
        v.sort(key=lambda x: -x["bytes"])
    unscanned = [r for r in CFG["roots"] if r not in {s[0] for s in scans}]
    return {
        "roots": [{"root": r, "scanned": d, "files": n, "bytes": b, "kinds": by_root[r],
                   "mounted": os.path.isdir(CFG["roots"].get(r, ""))} for r, d, n, b in scans],
        "unscanned": [{"root": r, "mounted": os.path.isdir(CFG["roots"][r])} for r in unscanned],
        "lightroom": [r[0] for r in query("SELECT DISTINCT catalogue FROM lr")],
    }


def api_tree(root, path):
    p = path.strip("/")
    prefix = (p + "/") if p else ""
    L = len(prefix)
    lo, hi = prefix, prefix + "￿"
    rows = query("""
      SELECT CASE WHEN instr(substr(rel, ?), '/') > 0
                  THEN substr(rel, ?, instr(substr(rel, ?), '/') - 1) ELSE substr(rel, ?) END AS name,
             instr(substr(rel, ?), '/') > 0 AS isdir, COUNT(*), SUM(size), MAX(mtime)
      FROM files WHERE root=? AND rel >= ? AND rel < ?
      GROUP BY name, isdir ORDER BY SUM(size) DESC LIMIT 500""",
        (L + 1,) * 5 + (root, lo, hi))
    return {"root": root, "path": p, "children": [
        {"name": n, "dir": bool(d), "files": c, "bytes": b or 0,
         "newest": datetime.fromtimestamp(m).strftime("%Y-%m-%d") if m else ""} for n, d, c, b, m in rows]}


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


def api_orphans():
    lrset = {p for (p,) in query("SELECT path FROM lr")}
    if not lrset:
        return {"available": False}
    by_top = defaultdict(lambda: [0, 0])
    total = 0
    for root, rel, size in query("SELECT root, rel, size FROM files WHERE kind='photo' AND ext!='png'"):
        if os.path.join(CFG["roots"][root], rel) not in lrset:
            k = (root, rel.split("/")[0]); by_top[k][0] += 1; by_top[k][1] += size; total += 1
    rows = [{"root": r, "folder": t, "files": v[0], "bytes": v[1]}
            for (r, t), v in sorted(by_top.items(), key=lambda x: -x[1][1])[:100]]
    return {"available": True, "total": total, "rows": rows}


def build_tree(depth=4, per_dir=200):
    """Aggregate folder sizes to `depth` levels for the static report."""
    acc = defaultdict(lambda: [0, 0, 0.0])
    for root, rel, size, mtime in query("SELECT root, rel, size, mtime FROM files"):
        parts = rel.split("/")
        for i in range(min(len(parts), depth)):
            a = acc[(root, "/".join(parts[:i]), parts[i], i < len(parts) - 1)]
            a[0] += 1; a[1] += size
            if mtime > a[2]:
                a[2] = mtime
    tree = defaultdict(lambda: defaultdict(list))
    for (root, parent, name, isdir), (n, b, m) in acc.items():
        tree[root][parent].append({"name": name, "dir": isdir, "files": n, "bytes": b,
                                   "newest": datetime.fromtimestamp(m).strftime("%Y-%m-%d") if m else ""})
    for root in tree:
        for parent in tree[root]:
            tree[root][parent].sort(key=lambda c: -c["bytes"])
            tree[root][parent] = tree[root][parent][:per_dir]
    return {"depth": depth, "tree": tree}


def write_index(cfg, depth=4):
    """Write a self-contained index.html (overview, drill-down, duplicates, Lightroom)."""
    global CFG
    CFG = cfg
    data = {"summary": api_summary(), "config": {"roots": cfg["roots"], "report_dir": cfg["report_dir"]},
            "dupes": api_dupes(), "orphans": api_orphans(), "built": datetime.now().strftime("%Y-%m-%d"),
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
    if cmd == "dupes":
        return run_job("dupes" + (" --full" if body.get("full") else ""), ["dupes"] + (["--full"] if body.get("full") else []))
    if cmd == "lrcat":
        cat = os.path.expanduser(body.get("catalogue", ""))
        if not (cat.endswith(".lrcat") and os.path.isfile(cat)):
            return "not a readable .lrcat file"
        return run_job("lrcat " + os.path.basename(cat), ["lrcat", cat])
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
                return self._send(200, api_tree(q["root"], q.get("path", "")))
            if u.path == "/api/dupes":
                return self._send(200, api_dupes())
            if u.path == "/api/orphans":
                return self._send(200, api_orphans())
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
:root{--bg:#f6f6f4;--card:#fff;--ink:#1c1c1a;--mute:#6b6b66;--line:#e3e3df;--acc:#2f6fed;--bar:#dfe6f7;
--photo:#2f6fed;--video:#d9642b;--audio:#8a5cc6;--document:#2a9d6f;--archive:#b58a1b;--system:#8c8c86;--lightroom:#c23b73;--other:#b9b9b3}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecece8;--mute:#9a9a94;--line:#31312e;--acc:#6e9bff;--bar:#2a3550}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;gap:16px;align-items:center;padding:14px 24px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
h1{font-size:16px;margin:0 12px 0 0}nav button{background:none;border:0;padding:8px 12px;color:var(--mute);font:inherit;cursor:pointer;border-bottom:2px solid transparent}
nav button.on{color:var(--ink);border-color:var(--acc)}main{padding:24px;max-width:1200px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.card h3{margin:0 0 2px;font-size:15px}.mute{color:var(--mute)}.big{font-size:22px;font-weight:600}
.stack{display:flex;height:12px;border-radius:6px;overflow:hidden;background:var(--line);margin:10px 0}
.legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px;color:var(--mute)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px}
button.btn{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:7px 14px;font:inherit;cursor:pointer}
button.btn.alt{background:var(--card);color:var(--ink);border:1px solid var(--line)}button.btn:disabled{opacity:.5;cursor:default}
table{width:100%;border-collapse:collapse}td,th{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mute);font-weight:500;font-size:12px}td.n,th.n{text-align:right;white-space:nowrap}
tr.row{cursor:pointer}tr.row:hover{background:var(--bar)}.bar{height:6px;background:var(--bar);border-radius:3px;margin-top:4px}.bar i{display:block;height:100%;background:var(--acc);border-radius:3px}
.crumbs{margin:0 0 12px}.crumbs a{color:var(--acc);cursor:pointer}.pill{font-size:11px;border:1px solid var(--line);border-radius:10px;padding:1px 8px;color:var(--mute)}
pre{background:#111;color:#cfe;padding:12px;border-radius:8px;max-height:340px;overflow:auto;font-size:12px}
.row2{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0}select,input[type=text]{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
.loc{font:12px ui-monospace,monospace;color:var(--mute);cursor:copy;word-break:break-all}
</style>
<header><h1>Storage audit</h1><nav id="nav"></nav><span style="flex:1"></span><span id="jobstate" class="pill" hidden></span></header>
<main id="view"></main>
<script>
const COLORS={photo:'--photo',video:'--video',audio:'--audio',document:'--document',archive:'--archive','app/system':'--system',lightroom:'--lightroom',other:'--other'};
const STATIC=typeof DATA!=='undefined';const TABS=STATIC?['Overview','Browse','Duplicates','Lightroom']:['Overview','Browse','Duplicates','Lightroom','Run'];let tab='Overview',S=null,CFGJ=null,browse={root:null,path:''};
const $=s=>document.querySelector(s),view=$('#view');
const fmt=b=>{const u=['B','KB','MB','GB','TB'];let i=0;b=b||0;while(b>=1024&&i<4){b/=1024;i++}return (i?b.toFixed(1):b)+' '+u[i]};
const num=n=>(n||0).toLocaleString('en-AU');
function el(t,a={},...k){const e=document.createElement(t);for(const[x,v]of Object.entries(a)){if(x==='class')e.className=v;else if(x.startsWith('on'))e[x]=v;else e.setAttribute(x,v)}for(const c of k.flat())e.append(c instanceof Node?c:document.createTextNode(c??''));return e}
const get=u=>{if(!STATIC)return fetch(u).then(r=>r.json());
  const x=new URL(u,'http://x'),q=Object.fromEntries(x.searchParams);
  if(x.pathname==='/api/summary')return Promise.resolve(DATA.summary);
  if(x.pathname==='/api/config')return Promise.resolve(DATA.config);
  if(x.pathname==='/api/dupes')return Promise.resolve(DATA.dupes);
  if(x.pathname==='/api/orphans')return Promise.resolve(DATA.orphans);
  if(x.pathname==='/api/job')return Promise.resolve({running:false,title:'',log:[],code:null});
  if(x.pathname==='/api/tree'){const c=(DATA.tree[q.root]||{})[q.path||''];return Promise.resolve({children:c||[],truncated:!c&&q.path&&q.path.split('/').length>=DATA.depth})}
  return Promise.resolve({})};
const post=(u,b)=>fetch(u,{method:'POST',headers:{'X-Audit':'1','Content-Type':'application/json'},body:JSON.stringify(b)}).then(async r=>({ok:r.ok,...await r.json()}));
function nav(){const n=$('#nav');n.replaceChildren(...TABS.map(t=>el('button',{class:t===tab?'on':'',onclick:()=>{tab=t;nav();render()}},t)))}
function kindBar(kinds,total){const s=el('div',{class:'stack'});kinds.forEach(k=>s.append(el('i',{title:k.kind+': '+fmt(k.bytes),style:`width:${100*k.bytes/(total||1)}%;background:var(${COLORS[k.kind]||'--other'})`})));return s}
function legend(kinds){return el('div',{class:'legend'},kinds.slice(0,6).map(k=>el('span',{},el('span',{class:'dot',style:`background:var(${COLORS[k.kind]||'--other'})`}),`${k.kind} ${fmt(k.bytes)}`)))}
async function render(){
  view.replaceChildren(el('p',{class:'mute'},'Loading…'));
  if(!CFGJ)CFGJ=await get('/api/config');
  S=await get('/api/summary');
  ({Overview:rOverview,Browse:rBrowse,Duplicates:rDupes,Lightroom:rLr,Run:rRun})[tab]();
}
function rOverview(){
  const tot=S.roots.reduce((a,r)=>a+r.bytes,0),files=S.roots.reduce((a,r)=>a+r.files,0);
  const out=[el('div',{class:'row2'},el('div',{class:'big'},fmt(tot)),el('span',{class:'mute'},`${num(files)} files across ${S.roots.length} scanned roots`))];
  if(!S.roots.length)out.push(el('div',{class:'card'},'Nothing scanned yet. Go to ',el('b',{},'Run'),' and start a scan.'));
  const g=el('div',{class:'grid'});
  S.roots.forEach(r=>g.append(el('div',{class:'card'},el('h3',{},r.root),el('div',{class:'mute'},`${num(r.files)} files · scanned ${r.scanned}${r.mounted?'':' · not mounted now'}`),
    el('div',{class:'big'},fmt(r.bytes)),kindBar(r.kinds,r.bytes),legend(r.kinds),
    el('div',{class:'row2'},el('button',{class:'btn alt',onclick:()=>{browse={root:r.root,path:''};tab='Browse';nav();render()}},'Browse folders')))));
  S.unscanned.forEach(r=>g.append(el('div',{class:'card'},el('h3',{},r.root),el('div',{class:'mute'},r.mounted?'Not scanned yet':'Not mounted'))));
  out.push(g);view.replaceChildren(...out)}
async function rBrowse(){
  if(!browse.root)browse.root=(S.roots[0]||{}).root;
  if(!browse.root){view.replaceChildren(el('p',{class:'mute'},'Scan something first.'));return}
  const d=await get(`/api/tree?root=${encodeURIComponent(browse.root)}&path=${encodeURIComponent(browse.path)}`);
  // smart drill-down: step straight through folders that only contain one folder
  const hops=[];while(d.children.length===1&&d.children[0].dir&&hops.length<12){const nx=d.children[0].name,np=(browse.path?browse.path+'/':'')+nx;
    const n2=await get(`/api/tree?root=${encodeURIComponent(browse.root)}&path=${encodeURIComponent(np)}`);
    if(n2.truncated||!n2.children.length)break;hops.push(nx);browse.path=np;d.children=n2.children}
  const sel=el('select',{onchange:e=>{browse={root:e.target.value,path:''};rBrowse()}},S.roots.map(r=>el('option',{value:r.root,...(r.root===browse.root?{selected:''}:{})},r.root)));
  const parts=browse.path?browse.path.split('/'):[];
  const crumbs=el('div',{class:'crumbs'},el('a',{onclick:()=>{browse.path='';rBrowse()}},browse.root),...parts.map((p,i)=>[' / ',el('a',{onclick:()=>{browse.path=parts.slice(0,i+1).join('/');rBrowse()}},p)]));
  const max=Math.max(1,...d.children.map(c=>c.bytes));
  const t=el('table',{},el('tr',{},el('th',{},'Name'),el('th',{class:'n'},'Files'),el('th',{class:'n'},'Size'),el('th',{class:'n'},'Newest')),
    d.children.map(c=>el('tr',{class:c.dir?'row':'',onclick:c.dir?()=>{browse.path=(browse.path?browse.path+'/':'')+c.name;rBrowse()}:null},
      el('td',{},(c.dir?'📁 ':'')+c.name,el('div',{class:'bar'},el('i',{style:`width:${100*c.bytes/max}%`}))),el('td',{class:'n'},num(c.files)),el('td',{class:'n'},fmt(c.bytes)),el('td',{class:'n mute'},c.newest))));
  const note=d.truncated?el('p',{class:'mute'},`The static report stops at ${DATA.depth} folder levels. Run storage_audit_ui.py for unlimited drill-down.`):(hops.length?el('p',{class:'mute'},`Skipped through single-folder levels: ${hops.join(' / ')}`):'');
  view.replaceChildren(el('div',{class:'row2'},sel),crumbs,note,el('div',{class:'card'},t))}
async function rDupes(){
  const d=await get('/api/dupes');
  const head=el('div',{class:'row2'},el('div',{class:'big'},fmt(d.reclaimable)),el('span',{class:'mute'},`reclaimable across ${num(d.groups)} groups · ${d.verified?'SHA-256 verified':'partial-hash match, run dupes with Full verify to confirm'}`));
  if(!d.groups){view.replaceChildren(head,el('div',{class:'card'},'No duplicate groups yet. On the Run tab, scan then run Find duplicates.'));return}
  const t=el('table',{},el('tr',{},el('th',{class:'n'},'Size'),el('th',{class:'n'},'Copies'),el('th',{class:'n'},'Drives'),el('th',{},'Locations (click to copy a path)')),
    d.top.map(g=>el('tr',{},el('td',{class:'n'},fmt(g.size)),el('td',{class:'n'},g.copies),el('td',{class:'n'},g.volumes),
      el('td',{},g.locations.map(l=>{const full=(CFGJ.roots[l.root]||'')+'/'+l.rel;return el('div',{class:'loc',title:'Copy full path',onclick:()=>navigator.clipboard&&navigator.clipboard.writeText(full)},`${l.root}: ${l.rel}`)})))));
  view.replaceChildren(head,el('div',{class:'card'},t))}
async function rLr(){
  const d=await get('/api/orphans');
  if(!d.available){view.replaceChildren(el('div',{class:'card'},'No Lightroom catalogue loaded. Use the Run tab to read one (for example ~/Lightroom/Lightroom.lrcat).'));return}
  const t=el('table',{},el('tr',{},el('th',{},'Drive'),el('th',{},'Folder'),el('th',{class:'n'},'Files'),el('th',{class:'n'},'Size')),
    d.rows.map(r=>el('tr',{},el('td',{},r.root),el('td',{},r.folder),el('td',{class:'n'},num(r.files)),el('td',{class:'n'},fmt(r.bytes)))));
  view.replaceChildren(el('div',{class:'row2'},el('div',{class:'big'},num(d.total)),el('span',{class:'mute'},`photos on disk that no loaded catalogue (${S.lightroom.join(', ')}) references`)),el('div',{class:'card'},t))}
function rRun(){
  const checks=Object.keys(CFGJ.roots).map(r=>el('label',{style:'margin-right:12px'},el('input',{type:'checkbox',value:r,checked:''})," "+r));
  const cat=el('input',{type:'text',size:'44',value:'~/Lightroom/Lightroom.lrcat'});
  const full=el('input',{type:'checkbox'});
  const log=el('pre',{id:'log'},'No job run yet.');
  const run=b=>post('/api/run',b).then(r=>{if(!r.ok)alert(r.error);poll()});
  view.replaceChildren(
    el('div',{class:'card'},el('h3',{},'1. Scan drives'),el('div',{class:'mute'},'Catalogues every file (read-only). Large drives take a while.'),el('div',{class:'row2'},checks),
      el('button',{class:'btn',onclick:()=>run({cmd:'scan',roots:checks.map(c=>c.querySelector('input')).filter(i=>i.checked).map(i=>i.value)})},'Scan selected')),
    el('p'),el('div',{class:'card'},el('h3',{},'2. Read a Lightroom catalogue'),el('div',{class:'row2'},cat,el('button',{class:'btn',onclick:()=>run({cmd:'lrcat',catalogue:cat.value})},'Read catalogue'))),
    el('p'),el('div',{class:'card'},el('h3',{},'3. Find duplicates'),el('div',{class:'mute'},'Compares files of equal size. Full verify reads every candidate in full, so it is slow on network drives.'),
      el('div',{class:'row2'},el('label',{},full,' Full verify (SHA-256)'),el('button',{class:'btn',onclick:()=>run({cmd:'dupes',full:full.checked})},'Find duplicates'))),
    el('p'),el('div',{class:'card'},el('h3',{},'4. HTML report'),el('div',{class:'mute'},'Writes the static report to '+CFGJ.report_dir),
      el('div',{class:'row2'},el('button',{class:'btn alt',onclick:async()=>{const r=await post('/api/report',{});alert(r.path||r.error)}},'Write report'))),
    el('p'),el('h3',{},'Job log'),log);poll()}
let timer=null;
async function poll(){clearTimeout(timer);const j=await get('/api/job');const p=$('#jobstate');p.hidden=!j.title;p.textContent=j.running?`running: ${j.title}`:(j.title?`${j.title}: ${j.code===0?'done':'exit '+j.code}`:'');
  const l=$('#log');if(l){l.textContent=j.log.join('\n')||'No job run yet.';l.scrollTop=l.scrollHeight}
  if(j.running)timer=setTimeout(poll,1000);else if(window._wasRunning){window._wasRunning=false;if(tab!=='Run')render()}
  window._wasRunning=j.running}
nav();render();poll();
</script></html>"""


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

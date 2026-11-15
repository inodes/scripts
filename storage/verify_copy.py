#!/usr/bin/env python3
"""verify_copy.py - read-only check that a destination tree holds everything in a source tree, and the latest.

  python3 verify_copy.py SRC DST [--sub NAME ...] [--hash] [--out report.txt]

For every file under SRC (or only the sub-folders named with --sub) it compares with the same relative path under DST:
  missing    in SRC, not in DST
  src-newer  both exist, SRC modified later (DST may be stale)      <- the dangerous one
  size-diff  same modified time but different size (possible truncated copy)
  dst-newer  both exist, DST modified later (fine: DST is the live copy)
  hash-diff  (--hash only) same size and time but different SHA-256
Regenerable Final Cut cache (Thumbnail Media, .fcpcache, .DS_Store, __Sync__, @eaDir ...) is counted separately and
does not fail the check. Nothing is ever written to SRC or DST. Exit status 0 = nothing at risk, 1 = something to look at.
"""
import argparse, hashlib, os, sys, time

SKIP_PARTS = ("Thumbnail Media",)
SKIP_NAMES = {".DS_Store", ".fcpcache", "__Sync__", ".localized", "Thumbs.db"}
SKIP_DIRS = {"@eaDir", "@Recycle", "@Recently-Snapshot", ".Spotlight-V100", ".Trashes", ".fseventsd"}
SLOP = 3  # seconds: SMB/NAS timestamp rounding (FAT-style 2 s)


def regenerable(rel):
    return any(p in rel for p in SKIP_PARTS) or os.path.basename(rel) in SKIP_NAMES


def walk(root):
    stack = [""]
    while stack:
        rel = stack.pop()
        try:
            with os.scandir(os.path.join(root, rel)) as it:
                for e in it:
                    r = os.path.join(rel, e.name) if rel else e.name
                    if e.is_dir(follow_symlinks=False):
                        if e.name not in SKIP_DIRS:
                            stack.append(r)
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        yield r, st.st_size, st.st_mtime
        except OSError as ex:
            print(f"[warn] cannot read {os.path.join(root, rel)}: {ex}", file=sys.stderr)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src"); ap.add_argument("dst")
    ap.add_argument("--sub", nargs="*", default=[""], help="only these sub-folders (e.g. Drone.fcpbundle)")
    ap.add_argument("--hash", action="store_true", help="also SHA-256 every file whose size and time match (slow, reads both copies)")
    ap.add_argument("--out", help="write the findings to this file as well")
    a = ap.parse_args()
    src, dst = os.path.expanduser(a.src), os.path.expanduser(a.dst)
    for p in (src, dst):
        if not os.path.isdir(p):
            sys.exit(f"not a folder: {p}")
    res = {k: [] for k in ("missing", "src-newer", "size-diff", "hash-diff", "dst-newer")}
    ok = ign = nbytes = 0
    t0 = time.time()
    for sub in a.sub:
        for rel, size, mt in walk(os.path.join(src, sub)):
            rel = os.path.join(sub, rel) if sub else rel
            d = os.path.join(dst, rel)
            if regenerable(rel):
                ign += 1
                continue
            try:
                st = os.stat(d)
            except OSError:
                res["missing"].append((rel, size, mt, None, None))
                continue
            row = (rel, size, mt, st.st_size, st.st_mtime)
            if mt > st.st_mtime + SLOP:
                res["src-newer"].append(row)
            elif st.st_mtime > mt + SLOP:
                res["dst-newer"].append(row)   # edited on the destination since: it is the live copy
                ok += 1
            elif size != st.st_size:
                res["size-diff"].append(row)   # same timestamp, different size: suspicious/truncated
            elif a.hash and sha(os.path.join(src, rel)) != sha(d):
                res["hash-diff"].append(row)
            else:
                ok += 1
            nbytes += size
    lines = [f"Source:      {src}", f"Destination: {dst}", f"Checked {ok + sum(len(v) for k, v in res.items() if k != 'dst-newer')} files in {time.time()-t0:.0f}s"
             f" ({'with' if a.hash else 'without'} SHA-256); {ok} fine; {ign} regenerable cache files ignored"]
    ts = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"
    risky = 0
    for k in ("missing", "src-newer", "size-diff", "hash-diff", "dst-newer"):
        v = res[k]
        if k != "dst-newer":
            risky += len(v)
        lines.append(f"\n{k}: {len(v)}" + ("  (destination is the newer/live copy - fine)" if k == "dst-newer" else ""))
        for rel, s, m, ds, dm in sorted(v, key=lambda r: -r[1])[:40]:
            lines.append(f"  {rel}  [src {s:,} B {ts(m)}" + (f" | dst {ds:,} B {ts(dm)}]" if ds is not None else " | dst absent]"))
        if len(v) > 40:
            lines.append(f"  ... and {len(v)-40} more")
    miss_b = sum(r[1] for r in res["missing"])
    lines.append(f"\nVERDICT: {'SAFE - destination has everything and nothing newer is left on the source' if not risky else str(risky) + ' file(s) exist only (or newer) on the source: copy these before retiring it'}"
                 + (f" ({miss_b/1e9:.1f} GB missing)" if res["missing"] else ""))
    out = "\n".join(lines)
    print(out)
    if a.out:
        with open(a.out, "w") as f:
            f.write(out + "\n")
    sys.exit(1 if risky else 0)


if __name__ == "__main__":
    main()

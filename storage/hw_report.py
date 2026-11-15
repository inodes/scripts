#!/usr/bin/env python3
"""Read-only hardware and connection report for a Mac: Thunderbolt/USB tree with link speeds, drives, network
interfaces, and how each network share is mounted (which cable it really uses).

Run it on the Mac itself:  python3 storage/hw_report.py
It only reads (system_profiler, diskutil, ifconfig, networksetup, mount, route). Output goes next to the storage
reports (report_dir in config.json, i.e. iCloud, never git) as hardware/hardware_report.txt plus hardware/raw.json.
Serial numbers, MAC addresses and UUIDs are masked unless you pass --no-redact. Standard library only.
"""
import argparse, json, os, re, subprocess, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import storage_audit as sa  # noqa: E402

MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
UUID = re.compile(r"\b[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\b")
NOISE = re.compile(r"uuid|serial|route_string|switch_uid|domain_uid|_id_key$|location_id|^_properties|spdisplays_ndrv|"
                   r"_spec$|bsd_name$", re.I)
SENSITIVE_KEY = re.compile(r"serial|mac.?address|uuid|uid", re.I)


def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.stdout else (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(could not run {' '.join(cmd)}: {e})"


def sp(dtype):
    out = run(["system_profiler", dtype, "-json", "-detailLevel", "full"], timeout=120)
    try:
        return json.loads(out).get(dtype, [])
    except ValueError:
        return {"error": out[:300]}


def redact_text(s):
    s = MAC.sub("xx:xx:xx:xx:xx:xx", s)
    s = UUID.sub("xxxxxxxx-uuid", s)
    return re.sub(r"(smb:|afp:|nfs:)?//([^@/\s]+)@", lambda m: f"{m.group(1) or ''}//***@", s)


def redact_obj(o):
    if isinstance(o, dict):
        return {k: ("***" if SENSITIVE_KEY.search(k) and isinstance(v, (str, int)) else redact_obj(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [redact_obj(x) for x in o]
    return redact_text(o) if isinstance(o, str) else o


def scalars(d):
    """key: value lines for the plain fields of one record (one nested level flattened)."""
    out = []
    for k, v in d.items():
        if k in ("_name", "_items") or NOISE.search(k):
            continue
        label = k.replace("_key", "").replace("_", " ")
        if isinstance(v, (str, int, float)) and str(v) != "":
            out.append(f"{label}: {v}")
        elif isinstance(v, dict):
            inner = [f"{ik.replace('_key', '').replace('_', ' ')}={iv}" for ik, iv in v.items()
                     if isinstance(iv, (str, int, float)) and not NOISE.search(ik) and str(iv) != ""]
            if inner:
                out.append(f"{label}: " + ", ".join(inner))
        elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            out.append(f"{label}: " + ", ".join(v))
    return out


def tree(items, depth=0, lines=None):
    lines = [] if lines is None else lines
    pad = "  " * depth
    for it in items if isinstance(items, list) else [items]:
        if not isinstance(it, dict):
            continue
        lines.append(f"{pad}- {it.get('_name', '(unnamed)')}")
        for ln in scalars(it):
            lines.append(f"{pad}    {ln}")
        kids = it.get("_items")
        if kids:
            tree(kids, depth + 1, lines)
    return lines


def section(title, body):
    return f"\n{'=' * 78}\n{title}\n{'=' * 78}\n{body.rstrip()}\n"


def network_bits():
    parts = {}
    parts["hardware ports"] = run(["networksetup", "-listallhardwareports"])
    parts["service order (top = preferred)"] = run(["networksetup", "-listnetworkserviceorder"])
    ifc = run(["ifconfig"])
    keep = [ln for ln in ifc.splitlines() if re.match(r"^\w+\d*:", ln) or re.search(r"^\s+(media|status|inet ) ", ln)]
    parts["interfaces (link speed shows as 'media')"] = "\n".join(keep)
    mounts = [ln for ln in run(["mount"]).splitlines() if re.search(r"smbfs|nfs|afp", ln)]
    parts["network shares mounted"] = "\n".join(mounts) or "(none)"
    hosts = sorted({m.group(1) for ln in mounts for m in [re.search(r"//(?:[^@/]+@)?([^/ :]+)", ln)] if m})
    route = []
    for h in hosts:
        r = run(["route", "-n", "get", h], timeout=15)
        iface = re.search(r"interface:\s*(\S+)", r)
        route.append(f"{h}: traffic leaves via {iface.group(1) if iface else '(unknown)'}")
    parts["which interface each share host uses"] = "\n".join(route) or "(no network shares mounted)"
    parts["default route"] = "\n".join(run(["netstat", "-rn", "-f", "inet"]).splitlines()[:12])
    parts["disks (diskutil)"] = run(["diskutil", "list"], timeout=60)
    return parts


def build(redact):
    raw = {t: sp(t) for t in ("SPHardwareDataType", "SPThunderboltDataType", "SPUSBDataType", "SPStorageDataType",
                              "SPNVMeDataType", "SPNetworkDataType", "SPEthernetDataType", "SPDisplaysDataType")}
    out = [f"Hardware report {datetime.now().strftime('%Y-%m-%d %H:%M')}  (masked: {'yes' if redact else 'no'})\n"]
    out.append(section("This Mac", "\n".join(tree(raw["SPHardwareDataType"]))))
    out.append(section("Thunderbolt / USB4 (ports, hubs and what hangs off them)", "\n".join(tree(raw["SPThunderboltDataType"]))))
    out.append(section("USB devices (speed shows what each link negotiated)", "\n".join(tree(raw["SPUSBDataType"]))))
    out.append(section("Volumes", "\n".join(tree(raw["SPStorageDataType"]))))
    out.append(section("NVMe drives", "\n".join(tree(raw["SPNVMeDataType"]))))
    out.append(section("Displays", "\n".join(tree(raw["SPDisplaysDataType"]))))
    out.append(section("Network services", "\n".join(tree(raw["SPNetworkDataType"]))))
    for title, body in network_bits().items():
        out.append(section(title, body))
    text = "".join(out)
    return (redact_text(text), redact_obj(raw)) if redact else (text, raw)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-redact", action="store_true", help="keep serial numbers, MAC addresses and UUIDs")
    ap.add_argument("--stdout", action="store_true", help="print the report instead of saving it")
    a = ap.parse_args()
    if sys.platform != "darwin":
        sys.exit("This reads macOS hardware details: run it on the Mac, not in a Linux shell.")
    text, raw = build(not a.no_redact)
    if a.stdout:
        print(text)
        return
    cfg = sa.load_config(os.path.expanduser(a.config) if a.config else sa.default_config_path())
    outdir = os.path.join(cfg["report_dir"], "hardware")
    os.makedirs(outdir, exist_ok=True)
    for name, data in (("hardware_report.txt", text), ("raw.json", json.dumps(raw, indent=1, default=str))):
        p = os.path.join(outdir, name)
        with open(p, "w") as f:
            f.write(data)
        os.chmod(p, 0o600)
    print(f"Saved {os.path.join(outdir, 'hardware_report.txt')}\nTell Claude it is done and it can read it from there.")


if __name__ == "__main__":
    main()

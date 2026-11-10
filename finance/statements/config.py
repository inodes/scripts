"""Configuration loader — builds defaults dynamically from registered providers."""
import json
import os
from typing import Any, Dict, Optional

from . import paths
from .providers import AVAILABLE_PROVIDERS

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICLOUD = paths.ICLOUD


def build_defaults() -> Dict[str, Any]:
    """Build default configuration dynamically from registered provider classes."""
    providers_cfg: Dict[str, Dict[str, Any]] = {}
    target_dirs: Dict[str, str] = {}
    scan_dirs = []

    for p in AVAILABLE_PROVIDERS:
        if p.name == "generic":
            continue
        providers_cfg[p.name] = p.default_config()
        if p.target_dir:
            target_dirs[p.name] = p.target_dir
        for sd in p.scan_dirs:
            if sd not in scan_dirs:
                scan_dirs.append(sd)

    if not scan_dirs:
        scan_dirs = [paths.get("pdf"), paths.get("landing")]

    return {
        "data_dir": paths.get("data"),
        "scan_dirs": scan_dirs,
        "providers": providers_cfg,
        "target_dirs": target_dirs,
        "skip_dirs": ["old", "Archive", "Duplicates", ".Trash"],
        "recursive": False,
    }


def path() -> str:
    """Resolve active configuration path:
    1. $PDF_AUDIT_CONFIG environment variable if provided
    2. ~/Library/Mobile Documents/com~apple~CloudDocs/Finance/config.json (primary user iCloud config)
    3. HERE/config.json (local repo config fallback, git-ignored)
    4. Defaults to ~/Library/Mobile Documents/com~apple~CloudDocs/Finance/config.json
    """
    return paths.config_path()


def get_category_rules_path() -> Optional[str]:
    """Return the resolved category_rules.json file path if present."""
    if os.environ.get("CATEGORY_RULES_PATH"):
        p = os.path.abspath(os.path.expanduser(os.environ["CATEGORY_RULES_PATH"]))
        if os.path.isfile(p):
            return p
    icloud_rules = paths.under("finance", "category_rules.json")
    if os.path.isfile(icloud_rules):
        return icloud_rules
    repo_rules = os.path.join(HERE, "category_rules.json")
    if os.path.isfile(repo_rules):
        return repo_rules
    return None


def load_records() -> Dict[str, Any]:
    """Your own data (remittances, share history, valuations, ...), kept OUT of config.json.

    Each <records>/<name>.json holds the value of cfg["personal"][<name>]. config.json holds
    settings only and points here through directories.records.
    """
    out: Dict[str, Any] = {}
    folder = paths.get("records")
    if not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        fp = os.path.join(folder, name)
        try:
            with open(fp, encoding="utf-8") as f:
                out[name[:-5]] = json.load(f)
        except (OSError, ValueError) as e:
            raise RuntimeError(f"cannot read data file {paths.show(fp)}: {e}")
    return out


def load() -> Dict[str, Any]:
    defaults = build_defaults()
    cfg = dict(defaults)
    cfg["target_dirs"] = dict(defaults["target_dirs"])
    cfg["providers"] = {k: dict(v) for k, v in defaults["providers"].items()}
    cfg["category_rules"] = {}

    p = path()
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            user_cfg = json.load(f)
        target_dirs = user_cfg.pop("target_dirs", None)
        providers_cfg = user_cfg.pop("providers", None)
        directories = user_cfg.pop("directories", None)
        category_rules = user_cfg.pop("category_rules", None)
        cfg.update(user_cfg)
        if isinstance(target_dirs, dict):
            cfg["target_dirs"].update(target_dirs)
        if isinstance(providers_cfg, dict):
            for pk, pv in providers_cfg.items():
                if isinstance(pv, dict):
                    cfg["providers"].setdefault(pk, {}).update(pv)
        if isinstance(directories, dict) and directories.get("data") and "data_dir" not in user_cfg:
            cfg["data_dir"] = paths.get("data")
        if isinstance(category_rules, dict):
            cfg["category_rules"] = dict(category_rules)

    # Data lives in <records>/*.json; an inline "personal" block in config.json is still honoured
    # (legacy) but the data files win.
    personal = dict(cfg.get("personal") or {})
    personal.update(load_records())
    cfg["personal"] = personal

    rules_file = get_category_rules_path()
    if rules_file and os.path.isfile(rules_file):
        try:
            with open(rules_file, encoding="utf-8") as f:
                rdata = json.load(f)
                file_rules = rdata.get("rules", rdata) if isinstance(rdata, dict) else {}
                if isinstance(file_rules, dict):
                    cfg["category_rules"].update(file_rules)
        except Exception:
            pass

    for pk, pv in cfg["providers"].items():
        if pv.get("target_dir"):
            pv["target_dir"] = paths.expand(pv["target_dir"])
            cfg["target_dirs"][pk] = pv["target_dir"]
        if pv.get("scan_dirs"):
            pv["scan_dirs"] = [paths.expand(x) for x in pv["scan_dirs"]]
        if pv.get("duplicates_dir"):
            pv["duplicates_dir"] = paths.expand(pv["duplicates_dir"])

    cfg["scan_dirs"] = [paths.expand(x) for x in cfg.get("scan_dirs", [])]
    cfg["target_dirs"] = {k: paths.expand(v) for k, v in cfg.get("target_dirs", {}).items()}
    cfg["data_dir"] = os.path.expanduser(cfg["data_dir"])
    cfg["directories"] = dict(paths.directories())
    cfg["db"] = os.path.join(cfg["data_dir"], "pdf_audit.db")
    return cfg


def provider_config(cfg: Dict[str, Any], provider_name: str) -> Dict[str, Any]:
    """Return merged provider configuration for a given provider or doc_type."""
    if not cfg:
        return {}
    provs = cfg.get("providers", {})
    if provider_name in provs:
        return provs[provider_name]
    if provider_name.startswith("hsbc_") and "hsbc" in provs:
        return provs["hsbc"]
    return {}


def show_path(p: str) -> str:
    """Render path compactly using ~/iCloud or ~."""
    return paths.show(p)

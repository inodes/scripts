"""Configuration loader — builds defaults dynamically from registered providers."""
import json
import os
from typing import Any, Dict, Optional

from .providers import AVAILABLE_PROVIDERS

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICLOUD = "~/Library/Mobile Documents/com~apple~CloudDocs"


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
        scan_dirs = [
            ICLOUD + "/Finance/Statements/HSBC",
            ICLOUD + "/PDF",
            "~/Downloads",
        ]

    return {
        "data_dir": "~/Library/Application Support/pdf-audit",
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
    if os.environ.get("PDF_AUDIT_CONFIG"):
        return os.path.abspath(os.path.expanduser(os.environ["PDF_AUDIT_CONFIG"]))
    icloud_cfg = os.path.expanduser(f"{ICLOUD}/Finance/config.json")
    if os.path.isfile(icloud_cfg):
        return icloud_cfg
    repo_cfg = os.path.join(HERE, "config.json")
    if os.path.isfile(repo_cfg):
        return repo_cfg
    return icloud_cfg


def get_category_rules_path() -> Optional[str]:
    """Return the resolved category_rules.json file path if present."""
    if os.environ.get("CATEGORY_RULES_PATH"):
        p = os.path.abspath(os.path.expanduser(os.environ["CATEGORY_RULES_PATH"]))
        if os.path.isfile(p):
            return p
    icloud_rules = os.path.expanduser(f"{ICLOUD}/Finance/category_rules.json")
    if os.path.isfile(icloud_rules):
        return icloud_rules
    repo_rules = os.path.join(HERE, "category_rules.json")
    if os.path.isfile(repo_rules):
        return repo_rules
    return None


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
        if isinstance(directories, dict):
            cfg["directories"] = {k: os.path.expanduser(v) for k, v in directories.items() if v}
        if isinstance(category_rules, dict):
            cfg["category_rules"] = dict(category_rules)

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
            pv["target_dir"] = os.path.expanduser(pv["target_dir"])
            cfg["target_dirs"][pk] = pv["target_dir"]
        if pv.get("scan_dirs"):
            pv["scan_dirs"] = [os.path.expanduser(x) for x in pv["scan_dirs"]]

    cfg["scan_dirs"] = [os.path.expanduser(x) for x in cfg.get("scan_dirs", [])]
    cfg["target_dirs"] = {k: os.path.expanduser(v) for k, v in cfg.get("target_dirs", {}).items()}
    cfg["data_dir"] = os.path.expanduser(cfg["data_dir"])
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
    if not p:
        return "-"
    home = os.path.expanduser("~")
    icloud = os.path.expanduser(ICLOUD)
    if p.startswith(icloud):
        return "~/iCloud" + p[len(icloud):]
    return "~" + p[len(home):] if p.startswith(home) else p

#!/usr/bin/env python3
"""fetch_email_invoices.py - Download invoice & statement PDF attachments from email.

Connects securely via IMAP (e.g. Gmail / Google Workspace with an App Password),
searches for invoice and statement emails by target provider or query, extracts
PDF attachments, deduplicates against the pdf_audit SQLite database, and saves
them to a staging folder (e.g. ~/iCloud/PDF) for Hazel / pdf_audit ingestion.
"""

import argparse
import email
from email.header import decode_header
import hashlib
import imaplib
import json
import os
import re
import ssl
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

# Target provider preset queries (optimised for Gmail X-GM-RAW search)
TARGET_PRESETS: Dict[str, Dict[str, Any]] = {
    "apple": {
        "display_name": "Apple Store & Digital Services",
        "gmail_query": 'from:(apple.com OR itunes.com) (invoice OR receipt OR "your order" OR subscription) has:attachment filename:pdf',
        "imap_criteria": ['OR', 'FROM', 'apple.com', 'FROM', 'itunes.com'],
    },
    "qantas": {
        "display_name": "Qantas Airways & Flights",
        "gmail_query": 'from:qantas (tax OR invoice OR itinerary OR "e-ticket" OR booking OR receipt) has:attachment filename:pdf',
        "imap_criteria": ['FROM', 'qantas'],
    },
    "tesla": {
        "display_name": "Tesla & Supercharging",
        "gmail_query": 'from:tesla.com (invoice OR receipt OR supercharging OR "order confirmation") has:attachment filename:pdf',
        "imap_criteria": ['FROM', 'tesla.com'],
    },
    "commbank": {
        "display_name": "Commonwealth Bank (CBA)",
        "gmail_query": 'from:(commbank.com.au OR netbank.com.au) (statement OR eStatement OR invoice OR notice) has:attachment filename:pdf',
        "imap_criteria": ['OR', 'FROM', 'commbank.com.au', 'FROM', 'netbank.com.au'],
    },
    "hsbc": {
        "display_name": "HSBC Australia",
        "gmail_query": 'from:hsbc.com.au (statement OR eStatement OR advice OR confirmation) has:attachment filename:pdf',
        "imap_criteria": ['FROM', 'hsbc.com.au'],
    },
    "ing": {
        "display_name": "ING Australia",
        "gmail_query": 'from:ing.com.au (statement OR eStatement) has:attachment filename:pdf',
        "imap_criteria": ['FROM', 'ing.com.au'],
    },
    "utilities": {
        "display_name": "Telco & Energy (Aussie Broadband, Origin, AGL, Telstra, Optus)",
        "gmail_query": 'from:(aussiebroadband.com.au OR originenergy.com.au OR agl.com.au OR energyaustralia.com.au OR telstra.com OR optus.com.au) (tax OR invoice OR bill) has:attachment filename:pdf',
        "imap_criteria": ['OR', 'SUBJECT', 'invoice', 'SUBJECT', 'bill'],
    },
    "bunnings": {
        "display_name": "Bunnings Warehouse",
        "gmail_query": 'from:bunnings.com.au (receipt OR invoice OR tax) has:attachment filename:pdf',
        "imap_criteria": ['FROM', 'bunnings.com.au'],
    },
}

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/pdf-audit/email.json")
DEFAULT_TARGET_DIR = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/PDF")


def decode_str(header_val: Any) -> str:
    """Decode an RFC 2047 MIME header value to a clean unicode string."""
    if not header_val:
        return ""
    if isinstance(header_val, bytes):
        return header_val.decode("utf-8", errors="replace")
    decoded_fragments = decode_header(header_val)
    parts = []
    for frag, enc in decoded_fragments:
        if isinstance(frag, bytes):
            enc = enc or "utf-8"
            try:
                parts.append(frag.decode(enc, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(frag.decode("utf-8", errors="replace"))
        else:
            parts.append(str(frag))
    return "".join(parts).strip()


def sanitize_filename(name: str) -> str:
    """Sanitize filename to avoid directory traversal and unsafe characters."""
    clean = re.sub(r'[/\\?%*:|"<>]+', "_", name)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or "attachment.pdf"


def load_known_shas() -> Set[str]:
    """Load known file SHA-256 hashes from the pdf_audit SQLite database if available."""
    db_path = os.path.expanduser("~/Library/Application Support/pdf-audit/pdf_audit.db")
    if not os.path.exists(db_path):
        return set()
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        rows = c.execute("SELECT sha256 FROM files WHERE sha256 IS NOT NULL").fetchall()
        stmt_rows = c.execute("SELECT sha256 FROM statements WHERE sha256 IS NOT NULL").fetchall()
        conn.close()
        return {r[0] for r in rows if r[0]} | {r[0] for r in stmt_rows if r[0]}
    except Exception:
        return set()


def get_keychain_password(service: str = "pdf-audit-gmail", account: Optional[str] = None) -> Optional[str]:
    """Retrieve app password from macOS Keychain."""
    import subprocess
    cmd = ["/usr/bin/security", "find-generic-password", "-s", service, "-w"]
    if account:
        cmd.extend(["-a", account])
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return res.stdout.strip()
    except Exception:
        return None


def set_keychain_password(account: str, password: str, service: str = "pdf-audit-gmail") -> bool:
    """Save app password to macOS Keychain."""
    import subprocess
    cmd = ["/usr/bin/security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", password]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except Exception:
        return False


def load_credentials(config_path: str = DEFAULT_CONFIG_PATH) -> Tuple[Optional[str], Optional[str], str, int, str]:
    """Load IMAP credentials from environment, config file, or macOS Keychain."""
    user = os.environ.get("GMAIL_USER") or os.environ.get("GMAIL_EMAIL") or os.environ.get("IMAP_USER")
    pwd = os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("IMAP_PASSWORD")
    server = os.environ.get("IMAP_SERVER") or "imap.gmail.com"
    port = int(os.environ.get("IMAP_PORT") or 993)
    target_dir = os.environ.get("TARGET_DIR") or DEFAULT_TARGET_DIR

    cfg = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"Warning: could not read {config_path}: {e}", file=sys.stderr)

    user = user or cfg.get("email") or cfg.get("user")
    pwd = pwd or cfg.get("app_password") or cfg.get("password")
    server = cfg.get("imap_server", server)
    port = int(cfg.get("imap_port", port))
    target_dir = os.path.expanduser(cfg.get("target_folder", target_dir))

    # Check Keychain if user is known but password is not
    if user and not pwd:
        pwd = get_keychain_password(account=user)

    return user, pwd, server, port, target_dir


def connect_imap(user: str, pwd: str, server: str, port: int) -> imaplib.IMAP4_SSL:
    """Connect and authenticate to the IMAP server."""
    ctx = ssl.create_default_context()
    mail = imaplib.IMAP4_SSL(server, port, ssl_context=ctx)
    mail.login(user, pwd)
    return mail


def fetch_invoices_for_preset(
    mail: imaplib.IMAP4_SSL,
    preset_key: str,
    target_dir: str,
    limit: int = 50,
    dry_run: bool = False,
    known_shas: Optional[Set[str]] = None,
    custom_query: Optional[str] = None,
    since_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Search for matching emails and extract PDF attachments."""
    if known_shas is None:
        known_shas = set()

    preset = TARGET_PRESETS.get(preset_key, {})
    disp = preset.get("display_name", preset_key)
    print(f"\n🔍 Searching for: \033[1;36m{disp}\033[0m")

    # Select inbox
    mail.select("INBOX", readonly=True)

    # Determine search criteria
    msg_ids: List[bytes] = []
    has_gm_raw = "X-GM-EXT-1" in mail.capabilities

    if custom_query:
        query_str = custom_query
        if since_date:
            query_str += f" after:{since_date}"
        if has_gm_raw:
            typ, data = mail.search(None, "X-GM-RAW", f'"{query_str}"')
            if typ == "OK" and data and data[0]:
                msg_ids = data[0].split()
        else:
            typ, data = mail.search(None, "ALL")
            if typ == "OK" and data and data[0]:
                msg_ids = data[0].split()
    elif has_gm_raw and "gmail_query" in preset:
        query_str = preset["gmail_query"]
        if since_date:
            query_str += f" after:{since_date}"
        typ, data = mail.search(None, "X-GM-RAW", f'"{query_str}"')
        if typ == "OK" and data and data[0]:
            msg_ids = data[0].split()
    else:
        # Standard IMAP search fallback
        criteria = preset.get("imap_criteria", ["ALL"])
        if since_date:
            dt = datetime.strptime(since_date, "%Y-%m-%d")
            criteria.extend(["SINCE", dt.strftime("%d-%b-%Y")])
        typ, data = mail.search(None, *criteria)
        if typ == "OK" and data and data[0]:
            msg_ids = data[0].split()

    print(f"   Found {len(msg_ids)} matching email(s)")
    if not msg_ids:
        return {"total": 0, "downloaded": 0, "skipped": 0, "files": []}

    # Process newest first up to limit
    msg_ids = list(reversed(msg_ids))[:limit]

    downloaded = 0
    skipped_existing = 0
    downloaded_files = []

    for msg_id in msg_ids:
        typ, msg_data = mail.fetch(msg_id, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = decode_str(msg.get("Subject", "(No Subject)"))
        sender = decode_str(msg.get("From", "(Unknown Sender)"))
        date_hdr = msg.get("Date", "")
        dt_tuple = email.utils.parsedate_to_datetime(date_hdr) if date_hdr else None
        date_str = dt_tuple.strftime("%Y-%m-%d") if dt_tuple else "Unknown Date"

        # Walk parts and extract PDFs
        for part in msg.walk():
            fn = part.get_filename()
            if not fn:
                continue
            fn = decode_str(fn)
            content_type = part.get_content_type()
            if not (fn.lower().endswith(".pdf") or content_type == "application/pdf"):
                continue

            payload = part.get_payload(decode=True)
            if not payload or len(payload) < 256:
                continue

            # Check if PDF magic number is present (%PDF-)
            if not payload.startswith(b"%PDF"):
                continue

            sha = hashlib.sha256(payload).hexdigest()
            clean_fn = sanitize_filename(fn)

            # Prepend date if not in filename for clarity
            if dt_tuple and not re.search(r"\d{4}", clean_fn):
                clean_fn = f"{dt_tuple.strftime('%Y%m%d')}_{clean_fn}"

            dest_path = os.path.join(target_dir, clean_fn)

            # Avoid collision or duplicate SHA
            if sha in known_shas:
                skipped_existing += 1
                continue

            if os.path.exists(dest_path):
                # Verify if it's the exact same content
                try:
                    with open(dest_path, "rb") as ef:
                        if hashlib.sha256(ef.read()).hexdigest() == sha:
                            skipped_existing += 1
                            continue
                except Exception:
                    pass
                # Append short sha
                base, ext = os.path.splitext(clean_fn)
                clean_fn = f"{base}_{sha[:8]}{ext}"
                dest_path = os.path.join(target_dir, clean_fn)

            if dry_run:
                print(f"   [DRY-RUN] Would download: \033[1;32m{clean_fn}\033[0m ({len(payload):,} bytes)")
                print(f"             Date: {date_str} | Subject: {subject[:60]} | From: {sender[:50]}")
                downloaded += 1
                downloaded_files.append(dest_path)
            else:
                os.makedirs(target_dir, exist_ok=True)
                with open(dest_path, "wb") as out_f:
                    out_f.write(payload)

                # Set mtime to email date if available
                if dt_tuple:
                    ts = dt_tuple.timestamp()
                    os.utime(dest_path, (ts, ts))

                print(f"   ✓ Downloaded: \033[1;32m{clean_fn}\033[0m ({len(payload):,} bytes)")
                print(f"     Date: {date_str} | From: {sender[:50]} | Subject: {subject[:50]}")
                downloaded += 1
                downloaded_files.append(dest_path)
                known_shas.add(sha)

    return {
        "total": len(msg_ids),
        "downloaded": downloaded,
        "skipped": skipped_existing,
        "files": downloaded_files,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Search email (Gmail / IMAP) for invoices and statements, and download PDF attachments."
    )
    parser.add_argument(
        "preset",
        nargs="?",
        default="all",
        choices=list(TARGET_PRESETS.keys()) + ["all"],
        help="Target provider preset to search (default: 'all')",
    )
    parser.add_argument("--query", "-q", help="Custom Gmail/IMAP search query (e.g. 'from:optus.com.au bill')")
    parser.add_argument("--since", help="Search emails since date (YYYY-MM-DD, e.g. 2025-01-01)")
    parser.add_argument("--limit", "-n", type=int, default=30, help="Max emails to inspect per provider (default: 30)")
    parser.add_argument(
        "--target-dir",
        "-o",
        default=DEFAULT_TARGET_DIR,
        help=f"Target staging directory for attachments (default: {DEFAULT_TARGET_DIR})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview matches without downloading")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to email config JSON")
    parser.add_argument("--setup", action="store_true", help="Interactive setup to configure credentials")
    parser.add_argument("--sync", action="store_true", help="Automatically trigger 'pdf_audit.py sync' after download")

    args = parser.parse_args()

    # Interactive setup
    if args.setup:
        print("\n\033[1;36m=== Email Invoices Downloader Setup ===\033[0m")
        print("Set up a Gmail / Google Workspace App Password:")
        print("1. Go to https://myaccount.google.com/apppasswords")
        print("2. Create an App Password for 'Antigravity Statements'")
        print("3. Enter your details below:\n")
        email_addr = input("Gmail address: ").strip()
        import getpass
        app_pw = getpass.getpass("App Password (16 characters, e.g. xxxx xxxx xxxx xxxx): ").strip()

        save_choice = input("Store in macOS Keychain? [Y/n]: ").strip().lower()
        if save_choice != "n":
            if set_keychain_password(email_addr, app_pw):
                print("✓ Successfully saved App Password in macOS Keychain.")
            else:
                print("Warning: could not save to Keychain, saving to config file.")

        cfg_dir = os.path.dirname(args.config)
        os.makedirs(cfg_dir, exist_ok=True)
        cfg_data = {
            "email": email_addr,
            "imap_server": "imap.gmail.com",
            "imap_port": 993,
            "target_folder": args.target_dir,
        }
        if save_choice == "n":
            cfg_data["app_password"] = app_pw

        with open(args.config, "w", encoding="utf-8") as f:
            json.dump(cfg_data, f, indent=2)
        os.chmod(args.config, 0o600)
        print(f"✓ Configuration written to: {args.config}")
        return 0

    user, pwd, server, port, target_dir = load_credentials(args.config)

    if not user or not pwd:
        print("\n\033[1;31mError: No email credentials found.\033[0m", file=sys.stderr)
        print("Run setup to configure credentials securely:", file=sys.stderr)
        print(f"  python3 {sys.argv[0]} --setup", file=sys.stderr)
        print("\nOr provide via environment variables:", file=sys.stderr)
        print("  export GMAIL_USER='you@gmail.com'", file=sys.stderr)
        print("  export GMAIL_APP_PASSWORD='xxxx xxxx xxxx xxxx'", file=sys.stderr)
        return 1

    print(f"\033[1;32mConnected to email:\033[0m {user} on {server}:{port}")
    print(f"\033[1;32mTarget folder:\033[0m {target_dir}")
    if args.dry_run:
        print("\033[1;33mMODE: DRY RUN (no files will be written)\033[0m")

    # Load known SHAs from database to prevent re-downloads
    known_shas = load_known_shas()
    print(f"Loaded {len(known_shas):,} known document hashes from SQLite store.")

    try:
        mail = connect_imap(user, pwd, server, port)
    except Exception as e:
        print(f"\n\033[1;31mIMAP Connection Failed:\033[0m {e}", file=sys.stderr)
        print("Note: If using Gmail, ensure 2-Step Verification is on and use an App Password.", file=sys.stderr)
        return 1

    targets = list(TARGET_PRESETS.keys()) if args.preset == "all" else [args.preset]
    total_downloaded = 0
    total_skipped = 0
    all_files = []

    try:
        if args.query:
            res = fetch_invoices_for_preset(
                mail,
                "custom",
                target_dir,
                limit=args.limit,
                dry_run=args.dry_run,
                known_shas=known_shas,
                custom_query=args.query,
                since_date=args.since,
            )
            total_downloaded += res["downloaded"]
            total_skipped += res["skipped"]
            all_files.extend(res["files"])
        else:
            for t_key in targets:
                res = fetch_invoices_for_preset(
                    mail,
                    t_key,
                    target_dir,
                    limit=args.limit,
                    dry_run=args.dry_run,
                    known_shas=known_shas,
                    since_date=args.since,
                )
                total_downloaded += res["downloaded"]
                total_skipped += res["skipped"]
                all_files.extend(res["files"])
    finally:
        try:
            mail.close()
            mail.logout()
        except Exception:
            pass

    print("\n" + "=" * 60)
    print(f"Finished! Total files downloaded: \033[1;32m{total_downloaded}\033[0m")
    print(f"Total already stored / skipped:   \033[1;34m{total_skipped}\033[0m")

    if total_downloaded > 0 and args.sync and not args.dry_run:
        print("\nTriggering 'pdf_audit.py sync' to ingest downloaded files...")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        pdf_audit_bin = os.path.join(script_dir, "pdf_audit.py")
        if os.path.exists(pdf_audit_bin):
            os.system(f"python3 {pdf_audit_bin} sync")

    return 0


if __name__ == "__main__":
    sys.exit(main())

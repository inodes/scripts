"""Ingest, Rename, and Import PDF Statements into the SQLite Store."""
import calendar
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Optional

from . import paths, store
from .pdf import PDFTextExtractor, file_sha256, normalized_pdf_sha256
from .providers import detect_and_extract, detect_provider


class Progress:
    def __init__(self, total: int, label: str = "Reading PDFs"):
        self.total = max(total, 1)
        self.label = label
        self.n = 0
        self.tty = sys.stderr.isatty()

    def step(self, what: str = ""):
        self.n += 1
        if not self.tty:
            return
        w = 28
        k = int(w * self.n / self.total)
        sys.stderr.write(
            f"\r{self.label} [{'#' * k}{'.' * (w - k)}] {self.n}/{self.total} {what[:40]}\033[K"
        )
        sys.stderr.flush()

    def done(self):
        if self.tty:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()



def hold_reason(data: Dict[str, Any]) -> Optional[str]:
    """Why a recognised document must NOT be renamed or moved (None = safe to file).

    A provider match alone is not proof: the document must also yield a date and reconcile.
    Override for manual runs with PDF_AUDIT_ALLOW_UNRECONCILED=1.
    """
    if os.environ.get("PDF_AUDIT_ALLOW_UNRECONCILED") == "1":
        return None
    if not (data.get("period_end") or data.get("issue_date")):
        return "no_document_date"
    if not data.get("reconciled"):
        return "not_reconciled"
    return None

def walk_pdfs(
    dirs: Iterable[str], recursive: bool = False, skip_dirs: Iterable[str] = ()
) -> List[str]:
    seen = set()
    out = []
    skip_set = set(skip_dirs or ())
    for d in dirs:
        p_exp = os.path.expanduser(d)
        if os.path.isfile(p_exp) and p_exp.lower().endswith(".pdf"):
            rp = os.path.realpath(p_exp)
            if rp not in seen:
                seen.add(rp)
                out.append(rp)
            continue
        if not os.path.isdir(p_exp):
            continue
        if recursive:
            for root, sub, files in os.walk(p_exp):
                sub[:] = [s for s in sub if not s.startswith(".") and s not in skip_set]
                for f in sorted(files):
                    if f.startswith(".") or not f.lower().endswith(".pdf"):
                        continue
                    full = os.path.realpath(os.path.join(root, f))
                    if full not in seen:
                        seen.add(full)
                        out.append(full)
        else:
            try:
                for f in sorted(os.listdir(p_exp)):
                    if f.startswith(".") or not f.lower().endswith(".pdf"):
                        continue
                    full = os.path.realpath(os.path.join(p_exp, f))
                    if os.path.isfile(full) and full not in seen:
                        seen.add(full)
                        out.append(full)
            except OSError:
                pass
    return out


def is_true_binary_duplicate(src_path: str, dest_path: str) -> bool:
    """Return True if two PDFs are byte-identical OR identical after stripping
    dynamic PDF download timestamps (/CreationDate, /ModDate, trailer /ID)."""
    try:
        if filecmp.cmp(src_path, dest_path, shallow=False):
            return True
        if os.path.getsize(src_path) == os.path.getsize(dest_path):
            return normalized_pdf_sha256(src_path) == normalized_pdf_sha256(dest_path)
    except OSError:
        pass
    return False


def same_pdf_content(src_path: str, dest_path: str, db=None) -> bool:
    """Return True if two PDF files are true binary duplicates OR have identical extracted text (exact or whitespace-normalised)."""
    if is_true_binary_duplicate(src_path, dest_path):
        return True
    try:
        t1 = PDFTextExtractor.extract_text(src_path, db=db)
        t2 = PDFTextExtractor.extract_text(dest_path, db=db)
        if t1 and t1 == t2:
            return True
        if t1 and t2:
            if re.sub(r"\s+", "", t1) == re.sub(r"\s+", "", t2):
                return True
            # Ignore dynamic download/portal print timestamps: e.g. "09:58 pm\n09/11/2026", "Generated on: ..."
            ts_re = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\s*\n?\s*\d{1,2}/\d{1,2}/\d{4}\b|\bGenerated on:[^\n]+", re.I)
            t1_clean = ts_re.sub("", t1)
            t2_clean = ts_re.sub("", t2)
            if re.sub(r"\s+", "", t1_clean) == re.sub(r"\s+", "", t2_clean):
                return True
    except Exception:
        pass
    return False


def unique_path_in_dir(dest_dir: str, filename: str) -> str:
    """Return a free path inside dest_dir (e.g. Duplicates/HSBC_..._01.pdf)."""
    cand = os.path.join(dest_dir, filename)
    if not os.path.exists(cand):
        return cand
    base, ext = os.path.splitext(cand)
    n = 1
    while True:
        alt = f"{base}_{n:02d}{ext}"
        if not os.path.exists(alt):
            return alt
        n += 1


def non_clobbering_path(src_path: str, dest_path: str, db=None) -> Optional[str]:
    """Never overwrite an existing file with different content.
    Returns None if dest_path already holds identical content (byte-level or extracted text),
    else a free path."""
    if not os.path.exists(dest_path):
        return dest_path
    if same_pdf_content(src_path, dest_path, db=db):
        return None
    base, ext = os.path.splitext(dest_path)
    n = 1
    while True:
        cand = f"{base}_{n:02d}{ext}"
        if not os.path.exists(cand):
            return cand
        if same_pdf_content(src_path, cand, db=db):
            return None
        n += 1


def set_mtime_from_metadata(path: str, data: Dict[str, Any]) -> bool:
    """Set the file's mtime, atime, and macOS creation date (btime) to the statement's
    period_end or issue_date at 00:00:00 local time. Returns True if modified."""
    import subprocess
    import time

    dt_str = data.get("period_end") or data.get("issue_date") or data.get("period_start")
    if not dt_str:
        return False
    if hasattr(dt_str, "isoformat"):
        dt_str = dt_str.isoformat()
    elif not isinstance(dt_str, str):
        return False
    try:
        year, month, day = (int(x) for x in dt_str.split("-")[:3])
        # Midnight local time (matches `touch -t YYYYMMDD0000.00`)
        epoch = time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))
        st = os.stat(path)
        btime = getattr(st, "st_birthtime", epoch)
        if abs(st.st_mtime - epoch) < 1 and abs(btime - epoch) < 1:
            return False
        os.utime(path, (epoch, epoch))
        setfile_bin = shutil.which("SetFile")
        if setfile_bin:
            # SetFile -d expects "MM/DD/YYYY HH:MM:SS"
            date_arg = f"{month:02d}/{day:02d}/{year:04d} 00:00:00"
            subprocess.run(
                [setfile_bin, "-d", date_arg, "-m", date_arg, path],
                capture_output=True,
                timeout=5,
            )
        return True
    except (ValueError, OSError):
        return False


def ensure_companion_txt(
    pdf_path: str, data: Optional[Dict[str, Any]] = None, db=None
) -> Optional[str]:
    """Ensure a companion .txt file exists alongside pdf_path containing extracted text.
    Matches the file timestamp (mtime/btime) to the PDF / statement date.
    Returns the path to the .txt file if written or already present, or None on failure."""
    if not os.path.isfile(pdf_path):
        return None
    base, _ = os.path.splitext(pdf_path)
    txt_path = f"{base}.txt"
    try:
        if not os.path.exists(txt_path) or os.path.getsize(txt_path) == 0:
            text = PDFTextExtractor.extract_text(pdf_path, db=db)
            if text:
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(text)
        if os.path.exists(txt_path):
            if data:
                set_mtime_from_metadata(txt_path, data)
            else:
                st = os.stat(pdf_path)
                os.utime(txt_path, (st.st_atime, st.st_mtime))
                setfile_bin = shutil.which("SetFile")
                if setfile_bin:
                    import time
                    tm = time.localtime(st.st_mtime)
                    date_arg = time.strftime("%m/%d/%Y %H:%M:%S", tm)
                    subprocess.run(
                        [setfile_bin, "-d", date_arg, "-m", date_arg, txt_path],
                        capture_output=True,
                        timeout=5,
                    )
        return txt_path
    except Exception:
        return None


def matches_provider_filter(provider_name: str, doc_type: str, req_provider: Optional[str]) -> bool:
    if not req_provider or req_provider.lower() == "auto":
        return provider_name != "generic"
    req = req_provider.lower()
    if req == "hsbc":
        return provider_name.startswith("hsbc") or doc_type.startswith("hsbc")
    return provider_name.lower() == req or doc_type.lower() == req


def store_statement(db, path: str, sha: str, data: Dict[str, Any]) -> int:
    """Insert or update a parsed statement and its account sections & transactions in SQLite."""
    existing = db.execute("SELECT id FROM statements WHERE sha256=?", (sha,)).fetchone()
    if existing:
        stmt_id = existing["id"]
        db.execute("DELETE FROM account_sections WHERE statement_id=?", (stmt_id,))
        db.execute("DELETE FROM transactions WHERE statement_id=?", (stmt_id,))
        db.execute(
            "UPDATE statements SET file=?, provider=?, doc_type=?, customer_no=?, "
            "period_start=?, period_end=?, issue_date=?, target_filename=?, reconciled=?, data=? "
            "WHERE id=?",
            (
                path,
                data.get("provider"),
                data.get("doc_type"),
                data.get("customer_no"),
                data.get("period_start"),
                data.get("period_end"),
                data.get("issue_date"),
                data.get("target_filename"),
                1 if data.get("reconciled") else 0,
                json.dumps(data, default=str),
                stmt_id,
            ),
        )
    else:
        cur = db.execute(
            "INSERT INTO statements(sha256, file, provider, doc_type, customer_no, "
            "period_start, period_end, issue_date, target_filename, reconciled, data) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                sha,
                path,
                data.get("provider"),
                data.get("doc_type"),
                data.get("customer_no"),
                data.get("period_start"),
                data.get("period_end"),
                data.get("issue_date"),
                data.get("target_filename"),
                1 if data.get("reconciled") else 0,
                json.dumps(data, default=str),
            ),
        )
        stmt_id = cur.lastrowid

    for acct in data.get("accounts", []):
        cur_sec = db.execute(
            "INSERT INTO account_sections("
            "statement_id, provider, doc_type, account_no, account_name, bsb, currency, "
            "period_start, period_end, opening_balance, closing_balance, calc_closing_balance, "
            "total_debits, total_credits, calc_debits, calc_credits, "
            "count_debits, count_credits, calc_count_debits, calc_count_credits, "
            "interest_rate, credit_limit, reconciled, note"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                stmt_id,
                data.get("provider"),
                data.get("doc_type"),
                acct.get("account_no"),
                acct.get("account_name"),
                acct.get("bsb"),
                acct.get("currency"),
                acct.get("period_start"),
                acct.get("period_end"),
                acct.get("opening_balance"),
                acct.get("closing_balance"),
                acct.get("calc_closing_balance"),
                acct.get("total_debits"),
                acct.get("total_credits"),
                acct.get("calc_debits"),
                acct.get("calc_credits"),
                acct.get("count_debits"),
                acct.get("count_credits"),
                acct.get("calc_count_debits"),
                acct.get("calc_count_credits"),
                acct.get("interest_rate"),
                acct.get("credit_limit"),
                1 if acct.get("reconciled") else 0,
                acct.get("note"),
            ),
        )
        sec_id = cur_sec.lastrowid
        for seq, tx in enumerate(acct.get("transactions", []), 1):
            db.execute(
                "INSERT INTO transactions("
                "statement_id, section_id, provider, doc_type, account_no, account_name, "
                "tx_date, seq, description, card_used, debit, credit, amount, balance, printed_balance, category"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    stmt_id,
                    sec_id,
                    data.get("provider"),
                    data.get("doc_type"),
                    acct.get("account_no"),
                    acct.get("account_name"),
                    tx.get("date"),
                    seq,
                    tx.get("description"),
                    tx.get("card_used"),
                    tx.get("debit"),
                    tx.get("credit"),
                    tx.get("amount"),
                    tx.get("balance"),
                    tx.get("printed_balance"),
                    tx.get("category"),
                ),
            )
    return stmt_id


def sync(
    db,
    cfg: Dict[str, Any],
    paths: Optional[List[str]] = None,
    provider_filter: Optional[str] = None,
    force: bool = False,
    recursive: Optional[bool] = None,
    do_rename: bool = False,
    do_import: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    scan_dirs = paths if paths else cfg["scan_dirs"]
    rec = recursive if recursive is not None else cfg.get("recursive", False)
    pdfs = walk_pdfs(scan_dirs, recursive=rec, skip_dirs=cfg.get("skip_dirs", ()))

    todo = []
    for p in pdfs:
        try:
            st = os.stat(p)
        except OSError:
            continue
        row = db.execute("SELECT size, mtime, provider FROM files WHERE path=?", (p,)).fetchone()
        if (
            force
            or do_rename
            or do_import
            or not row
            or row["size"] != st.st_size
            or abs(row["mtime"] - st.st_mtime) > 1
        ):
            todo.append(p)

    counts = {
        "statements": 0,
        "sections": 0,
        "transactions": 0,
        "skipped": 0,
        "unchanged": len(pdfs) - len(todo),
        "renamed": 0,
        "imported": 0,
        "duplicates": 0,
        "actions": [],
    }

    pr = Progress(len(todo), "Reading PDFs")
    for p in todo:
        pr.step(os.path.basename(p))
        try:
            st = os.stat(p)
            sha = file_sha256(p)
        except OSError:
            continue

        txt = PDFTextExtractor.extract_text(p, db=db, sha=sha)
        if not txt:
            db.execute(
                "INSERT OR REPLACE INTO files(path, sha256, size, mtime, provider, doc_type, status, note) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (p, sha, st.st_size, st.st_mtime, "none", "none", "empty", "empty or encrypted PDF"),
            )
            counts["skipped"] += 1
            continue

        prov = detect_provider(txt, p, forced_provider=None)
        if not matches_provider_filter(prov.name, prov.doc_type, provider_filter):
            db.execute(
                "INSERT OR REPLACE INTO files(path, sha256, size, mtime, provider, doc_type, status, note) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (p, sha, st.st_size, st.st_mtime, prov.name, prov.doc_type, "skipped", "filtered or generic"),
            )
            counts["skipped"] += 1
            continue
        data = prov.extract(txt, p)
        data["target_filename"] = prov.generate_filename(data, p, cfg)
        data["target_dir"] = prov.resolve_target_dir(data, p, cfg)

        final_path = p
        action_taken = None
        held = hold_reason(data) if (do_rename or do_import) else None
        if held:
            print(f"HOLD {os.path.basename(p)}: {held}; left in place", file=sys.stderr)
        if (do_rename or do_import) and not held:
            target_fname = data.get("target_filename") or os.path.basename(p)
            dest_dir = data.get("target_dir") if do_import else os.path.dirname(p)
            dest_dir = dest_dir or os.path.dirname(p)
            candidate_dest = os.path.join(dest_dir, target_fname)
            if os.path.abspath(p) != os.path.abspath(candidate_dest):
                free_dest = non_clobbering_path(p, candidate_dest, db=db)
                if free_dest is None:
                    action_taken = f"duplicate of {candidate_dest}"
                    counts["duplicates"] += 1
                    counts["actions"].append({"src": p, "dest": candidate_dest, "action": "duplicate"})
                    if not dry_run:
                        dup_dir = os.path.join(dest_dir, "Duplicates")
                        paths.check_destination(dup_dest)
                        os.makedirs(dup_dir, exist_ok=True)
                        dup_dest = unique_path_in_dir(dup_dir, os.path.basename(p))
                        try:
                            shutil.move(p, dup_dest)
                            src_txt = os.path.splitext(p)[0] + ".txt"
                            if os.path.isfile(src_txt):
                                dup_txt = os.path.splitext(dup_dest)[0] + ".txt"
                                shutil.move(src_txt, dup_txt)
                        except OSError:
                            pass
                        db.execute("DELETE FROM files WHERE path=?", (p,))
                else:
                    act_type = "import" if do_import else "rename"
                    if not dry_run:
                        paths.check_destination(free_dest)
                        os.makedirs(os.path.dirname(free_dest), exist_ok=True)
                        src_txt = os.path.splitext(p)[0] + ".txt"
                        dest_txt = os.path.splitext(free_dest)[0] + ".txt"
                        if os.path.isfile(src_txt) and src_txt != dest_txt:
                            try:
                                shutil.move(src_txt, dest_txt)
                            except OSError:
                                pass
                        shutil.move(p, free_dest)
                        set_mtime_from_metadata(free_dest, data)
                        ensure_companion_txt(free_dest, data=data, db=db)
                        db.execute("DELETE FROM files WHERE path=?", (p,))
                        final_path = free_dest
                        st = os.stat(final_path)
                    counts["imported" if do_import else "renamed"] += 1
                    counts["actions"].append(
                        {"src": p, "dest": free_dest, "action": act_type, "dry_run": dry_run}
                    )
            elif not dry_run:
                ensure_companion_txt(final_path, data=data, db=db)
                if set_mtime_from_metadata(final_path, data):
                    st = os.stat(final_path)

        store_statement(db, final_path, sha, data)
        note = "; ".join(data.get("warnings") or []) or "ok"
        db.execute(
            "INSERT OR REPLACE INTO files(path, sha256, size, mtime, provider, doc_type, status, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                final_path,
                sha,
                st.st_size,
                st.st_mtime,
                prov.name,
                data.get("doc_type"),
                "ok" if data.get("reconciled") else "warning",
                note,
            ),
        )
        counts["statements"] += 1
        counts["sections"] += len(data.get("accounts", []))
        counts["transactions"] += sum(len(a.get("transactions", [])) for a in data.get("accounts", []))

    pr.done()
    store.mark_current(db)
    db.commit()
    return counts

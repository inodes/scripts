"""PDF Text Extraction & SHA-256 Caching Engine.

Follows the layered architecture of Tools/lib/invoice_common.py and agl-audit:
  1. pypdf (page-separated by =====PAGE=====)
  2. pdfplumber fallback
  3. pdftotext CLI fallback
  4. Pure-Python stream/FlateDecode/ToUnicode CMap extractor fallback
"""
import ctypes
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import zlib
from typing import Dict, List, Optional

_LIBC = None
_WHERE_FROMS_ATTR = b"com.apple.metadata:kMDItemWhereFroms"


def _get_libc():
    global _LIBC
    if _LIBC is None:
        try:
            lib = ctypes.CDLL("/usr/lib/libc.dylib")
            lib.getxattr.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_uint32,
                ctypes.c_int,
            ]
            lib.getxattr.restype = ctypes.c_ssize_t
            _LIBC = lib
        except Exception:
            _LIBC = False
    return _LIBC or None


def get_where_froms(path: str) -> List[str]:
    """Read macOS com.apple.metadata:kMDItemWhereFroms download source URLs."""
    libc = _get_libc()
    if libc is not None:
        try:
            bpath = os.fsencode(path)
            size = libc.getxattr(bpath, _WHERE_FROMS_ATTR, None, 0, 0, 0)
            if size > 0:
                buf = ctypes.create_string_buffer(size)
                res = libc.getxattr(bpath, _WHERE_FROMS_ATTR, buf, size, 0, 0)
                if res > 0:
                    raw = buf.raw[:res]
                    val = plistlib.loads(raw)
                    if isinstance(val, list):
                        return [str(x) for x in val if x]
                    return [str(val)]
        except Exception:
            pass
        return []
    # Fallback if libc.getxattr is unavailable
    try:
        res = subprocess.run(
            ["xattr", "-p", "com.apple.metadata:kMDItemWhereFroms", path],
            capture_output=True,
            timeout=3,
        )
        if res.returncode == 0 and res.stdout:
            val = plistlib.loads(res.stdout)
            if isinstance(val, list):
                return [str(x) for x in val if x]
            return [str(val)]
    except Exception:
        pass
    return []

RE_STREAM = re.compile(rb"stream[\r\n]+(.*?)[\r\n]+endstream", re.DOTALL)
RE_BT_ET = re.compile(rb"BT(.*?)ET", re.DOTALL)
RE_TJ_LIT = re.compile(rb"\((.*?)\)\s*Tj", re.DOTALL)
RE_TJ_HEX = re.compile(rb"<([0-9a-fA-F\s]+)>\s*Tj", re.DOTALL)
RE_TJ_ARR = re.compile(rb"\[(.*?)\]\s*TJ", re.DOTALL)
RE_ARR_ITEMS = re.compile(rb"<([0-9a-fA-F\s]+)>|\(((?:\\.|[^\\)])*)\)", re.DOTALL)
RE_FONTS = re.compile(rb"/([A-Za-z0-9_]+)\s+(\d+)\s+0\s+R")
RE_TOUNICODE = re.compile(rb"/ToUnicode\s+(\d+)\s+0\s+R")
RE_BFCHAR = re.compile(r"beginbfchar(.*?)endbfchar", re.DOTALL)
RE_BFRANGE = re.compile(r"beginbfrange(.*?)endbfrange", re.DOTALL)
RE_PAIR = re.compile(r"<([0-9a-fA-F]+)>\s+<([0-9a-fA-F]+)>")
RE_RANGE_SEQ = re.compile(r"<([0-9a-fA-F]+)>\s+<([0-9a-fA-F]+)>\s+<([0-9a-fA-F]+)>")
RE_RANGE_ARR = re.compile(r"<([0-9a-fA-F]+)>\s+<([0-9a-fA-F]+)>\s*\[(.*?)\]", re.DOTALL)


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_RE_PDF_DYNAMIC_STAMPS = re.compile(
    rb"/(?:CreationDate|ModDate)\s*\(D:[^)]*\)|/ID\s*\[\s*<[0-9A-Fa-f]+>\s*<[0-9A-Fa-f]+>\s*\]"
)


def normalized_pdf_sha256(path: str) -> str:
    """Compute SHA-256 of PDF bytes after normalising dynamic download-time stamps
    (/CreationDate, /ModDate, and trailer /ID)."""
    with open(path, "rb") as f:
        raw = f.read()
    norm = _RE_PDF_DYNAMIC_STAMPS.sub(b"", raw)
    return hashlib.sha256(norm).hexdigest()


class PDFTextExtractor:
    """Multi-backend PDF text extractor with optional SQLite caching."""

    @classmethod
    def _clean_extracted_text(cls, text: str) -> str:
        if not text:
            return ""
        if re.search(r"/(?:\d{2,3}/){3,}", text):
            text = re.sub(r"/(\d{2,3})", lambda m: chr(int(m.group(1))), text)
        return text

    @classmethod
    def extract_text(cls, pdf_path: str, db=None, sha: str = None, max_pages: Optional[int] = None) -> str:
        if db is not None:
            sha = sha or file_sha256(pdf_path)
            row = db.execute("SELECT text FROM pdftext WHERE sha256=?", (sha,)).fetchone()
            if row and row[0]:
                cleaned = cls._clean_extracted_text(row[0])
                if cleaned != row[0]:
                    db.execute("UPDATE pdftext SET text=? WHERE sha256=?", (cleaned, sha))
                if max_pages is not None:
                    pages = cleaned.split("\n=====PAGE=====\n")[:max_pages]
                    return "\n=====PAGE=====\n".join(pages)
                return cleaned
            txt = cls._clean_extracted_text(cls._read_text(pdf_path, max_pages_override=max_pages))
            if txt and max_pages is None:
                db.execute("INSERT OR REPLACE INTO pdftext(sha256, text) VALUES (?,?)", (sha, txt))
            return txt
        return cls._clean_extracted_text(cls._read_text(pdf_path, max_pages_override=max_pages))

    @classmethod
    def _read_text(cls, pdf_path: str, max_pages_override: Optional[int] = None) -> str:
        # 1. Primary path: pypdf (reads first 2 pages first; only reads remaining pages if needed)
        try:
            import io
            import logging
            import pypdf
            logging.getLogger("pypdf").setLevel(logging.ERROR)
            try:
                reader = pypdf.PdfReader(pdf_path)
            except Exception:
                # Fallback for malformed EOF markers (e.g. Workday 2019 PDFs ending with \r0\r%EOF)
                with open(pdf_path, "rb") as f:
                    raw_bytes = f.read()
                fixed_bytes = re.sub(rb"\r?\n?0\r?\n?%EOF\s*$", b"\rstartxref\r0\r%%EOF", raw_bytes)
                if fixed_bytes != raw_bytes:
                    reader = pypdf.PdfReader(io.BytesIO(fixed_bytes), strict=False)
                else:
                    raise

            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    pass
            max_pages = max_pages_override or int(os.environ.get("PDF_AUDIT_MAX_PAGES", "20"))
            total_pages = len(reader.pages)
            if total_pages > 40:
                # 40+ page PDFs are books/manuals/reports, never statements/invoices/receipts;
                # only extract the first 2 pages for rapid header inspection.
                max_pages = min(max_pages, 2)
            pages = [(page.extract_text() or "") for page in reader.pages[:max_pages]]
            full_txt = "\n=====PAGE=====\n".join(pages).strip()
            if full_txt and full_txt.replace("=====PAGE=====", "").strip():
                return full_txt
        except Exception:
            pass

        # 2. Secondary path: pdfplumber
        try:
            import pdfplumber
            max_pages = max_pages_override or int(os.environ.get("PDF_AUDIT_MAX_PAGES", "20"))
            with pdfplumber.open(pdf_path) as pdf:
                if len(pdf.pages) > 40:
                    max_pages = min(max_pages, 2)
                pages = [(page.extract_text() or "") for page in pdf.pages[:max_pages]]
            full_txt = "\n=====PAGE=====\n".join(pages).strip()
            if full_txt and full_txt.replace("=====PAGE=====", "").strip():
                return full_txt
        except Exception:
            pass

        # 3. Tertiary path: pdftotext CLI
        pdftotext_bin = shutil.which("pdftotext")
        if pdftotext_bin:
            try:
                pg_lim = str(max_pages_override or 15)
                res = subprocess.run(
                    [pdftotext_bin, "-l", pg_lim, pdf_path, "-"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.replace("\x0c", "\n=====PAGE=====\n").strip()
            except Exception:
                pass

        # 4. Fallback: pure-Python stream & ToUnicode CMap parser (skip files > 1 MB)
        try:
            if os.path.getsize(pdf_path) > 1 * 1024 * 1024:
                return ""
        except OSError:
            return ""
        return cls._extract_pure_python(pdf_path)

    @classmethod
    def _extract_pure_python(cls, pdf_path: str) -> str:
        if not os.path.isfile(pdf_path):
            return ""
        try:
            with open(pdf_path, "rb") as f:
                content = f.read()
        except Exception:
            return ""

        objects: Dict[int, bytes] = {}
        for m in re.finditer(rb"(\d+)\s+0\s+obj\s*(.*?)\s*endobj", content, re.DOTALL):
            objects[int(m.group(1))] = m.group(2)

        def get_stream(body: bytes) -> bytes:
            m = RE_STREAM.search(body)
            if not m:
                return b""
            raw = m.group(1)
            try:
                return zlib.decompress(raw)
            except Exception:
                try:
                    return zlib.decompress(raw, -15)
                except Exception:
                    return raw

        font_cmaps: Dict[int, Dict[int, str]] = {}
        for oid, body in objects.items():
            if b"/ToUnicode" in body:
                tu_m = RE_TOUNICODE.search(body)
                if tu_m:
                    tu_id = int(tu_m.group(1))
                    tu_body = objects.get(tu_id, b"")
                    tu_stream = get_stream(tu_body).decode("latin1", errors="replace")
                    cmap: Dict[int, str] = {}
                    for block in RE_BFCHAR.findall(tu_stream):
                        for src, dst in RE_PAIR.findall(block):
                            s_val = int(src, 16)
                            dst_str = "".join(chr(int(dst[i:i+4], 16)) for i in range(0, len(dst), 4))
                            cmap[s_val] = dst_str
                    for block in RE_BFRANGE.findall(tu_stream):
                        for s_hex, e_hex, d_hex in RE_RANGE_SEQ.findall(block):
                            s_val, e_val, d_val = int(s_hex, 16), int(e_hex, 16), int(d_hex, 16)
                            for i in range(e_val - s_val + 1):
                                cmap[s_val + i] = chr(d_val + i)
                        for s_hex, e_hex, arr_str in RE_RANGE_ARR.findall(block):
                            s_val = int(s_hex, 16)
                            dests = re.findall(r"<([0-9a-fA-F]+)>", arr_str)
                            for i, d_h in enumerate(dests):
                                d_str = "".join(chr(int(d_h[j:j+4], 16)) for j in range(0, len(d_h), 4))
                                cmap[s_val + i] = d_str
                    font_cmaps[oid] = cmap

        res_fonts: Dict[str, int] = {}
        for oid, body in objects.items():
            if b"/Font" in body:
                for fname, fid in RE_FONTS.findall(body):
                    res_fonts[fname.decode("latin1")] = int(fid)

        chunks: List[str] = []
        for oid, body in objects.items():
            if b"/Contents" in body or b"/Length" in body:
                st = get_stream(body)
                if not st or b"BT" not in st:
                    continue
                for bt in RE_BT_ET.findall(st):
                    f_m = re.search(rb"/([A-Za-z0-9_]+)\s+\d+(?:\.\d+)?\s+Tf", bt)
                    curr_font = f_m.group(1).decode("latin1") if f_m else None
                    fid = res_fonts.get(curr_font or "", -1)
                    cmap = font_cmaps.get(fid, {})

                    for t in RE_TJ_LIT.findall(bt):
                        lit = t.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
                        if len(lit) >= 2 and len(lit) % 2 == 0 and any(cmap.get(int.from_bytes(lit[i:i+2], "big")) for i in range(0, min(10, len(lit)), 2)):
                            for i in range(0, len(lit), 2):
                                cid = int.from_bytes(lit[i:i+2], "big")
                                chunks.append(cmap.get(cid, chr(cid) if 32 <= cid < 128 else ""))
                        else:
                            for b in lit:
                                chunks.append(cmap.get(b, chr(b) if 32 <= b < 128 else ""))

                    for hx in RE_TJ_HEX.findall(bt):
                        hx_clean = re.sub(rb"\s+", b"", hx).decode("ascii", errors="ignore")
                        for i in range(0, len(hx_clean), 4):
                            cid = int(hx_clean[i:i+4], 16)
                            chunks.append(cmap.get(cid, chr(cid) if 32 <= cid < 128 else ""))

                    for arr in RE_TJ_ARR.findall(bt):
                        for hx_item, lit_item in RE_ARR_ITEMS.findall(arr):
                            if hx_item:
                                hx_c = re.sub(rb"\s+", b"", hx_item).decode("ascii", errors="ignore")
                                for i in range(0, len(hx_c), 4):
                                    cid = int(hx_c[i:i+4], 16)
                                    chunks.append(cmap.get(cid, chr(cid) if 32 <= cid < 128 else ""))
                            elif lit_item:
                                lit_c = lit_item.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
                                if len(lit_c) >= 2 and len(lit_c) % 2 == 0 and any(cmap.get(int.from_bytes(lit_c[i:i+2], "big")) for i in range(0, min(10, len(lit_c)), 2)):
                                    for i in range(0, len(lit_c), 2):
                                        cid = int.from_bytes(lit_c[i:i+2], "big")
                                        chunks.append(cmap.get(cid, chr(cid) if 32 <= cid < 128 else ""))
                                else:
                                    for b in lit_c:
                                        chunks.append(cmap.get(b, chr(b) if 32 <= b < 128 else ""))
                    chunks.append("\n")

        text = "".join(chunks)
        return re.sub(r"[ \t]+", " ", text).strip()

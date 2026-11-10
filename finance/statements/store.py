"""SQLite store for statements, account sections, transactions, and cached PDF text.

Everything in the store is rebuilt from the source PDF files, so deleting the
database loses nothing but time.
"""
import decimal
import json
import os
import sqlite3

from . import paths

sqlite3.register_adapter(decimal.Decimal, str)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY,
  sha256 TEXT,
  size INTEGER,
  mtime REAL,
  provider TEXT,
  doc_type TEXT,
  status TEXT,
  note TEXT,
  read_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pdftext (
  sha256 TEXT PRIMARY KEY,
  text TEXT
);
CREATE TABLE IF NOT EXISTS statements (
  id INTEGER PRIMARY KEY,
  sha256 TEXT UNIQUE,
  file TEXT,
  provider TEXT,
  doc_type TEXT,
  customer_no TEXT,
  period_start TEXT,
  period_end TEXT,
  issue_date TEXT,
  target_filename TEXT,
  reconciled INTEGER,
  data TEXT,
  current INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_statements_period ON statements(provider, doc_type, period_end);
CREATE TABLE IF NOT EXISTS account_sections (
  id INTEGER PRIMARY KEY,
  statement_id INTEGER REFERENCES statements(id) ON DELETE CASCADE,
  provider TEXT,
  doc_type TEXT,
  account_no TEXT,
  account_name TEXT,
  bsb TEXT,
  currency TEXT,
  period_start TEXT,
  period_end TEXT,
  opening_balance TEXT,
  closing_balance TEXT,
  calc_closing_balance TEXT,
  total_debits TEXT,
  total_credits TEXT,
  calc_debits TEXT,
  calc_credits TEXT,
  count_debits INTEGER,
  count_credits INTEGER,
  calc_count_debits INTEGER,
  calc_count_credits INTEGER,
  interest_rate TEXT,
  credit_limit TEXT,
  reconciled INTEGER,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_acct_sections ON account_sections(account_no, period_end);
CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  statement_id INTEGER REFERENCES statements(id) ON DELETE CASCADE,
  section_id INTEGER REFERENCES account_sections(id) ON DELETE CASCADE,
  provider TEXT,
  doc_type TEXT,
  account_no TEXT,
  account_name TEXT,
  tx_date TEXT,
  seq INTEGER,
  description TEXT,
  card_used TEXT,
  debit TEXT,
  credit TEXT,
  amount TEXT,
  balance TEXT,
  printed_balance TEXT,
  category TEXT
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(tx_date, account_no);
"""


def connect(db_path: str) -> sqlite3.Connection:
    # SQLite (especially WAL mode) can corrupt when iCloud syncs it mid-write: local folders only.
    paths.check_destination(db_path, kind="database")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    db = sqlite3.connect(db_path, timeout=60.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA busy_timeout = 60000")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    return db


def mark_current(db: sqlite3.Connection):
    """When duplicate PDFs cover the exact same provider, doc_type, accounts, and period, mark one as current=1."""
    db.execute("UPDATE statements SET current=0")
    groups = db.execute(
        """SELECT s.provider, s.doc_type, s.period_start, s.period_end,
                  COALESCE((SELECT GROUP_CONCAT(account_no, ',') FROM (SELECT DISTINCT account_no FROM account_sections WHERE statement_id = s.id ORDER BY account_no)), s.customer_no, '') as accts
           FROM statements s
           GROUP BY s.provider, s.doc_type, accts, s.period_start, s.period_end"""
    ).fetchall()
    for g in groups:
        best = db.execute(
            """SELECT s.id FROM statements s
               WHERE s.provider IS ? AND s.doc_type IS ?
                 AND s.period_start IS ? AND s.period_end IS ?
                 AND COALESCE((SELECT GROUP_CONCAT(account_no, ',') FROM (SELECT DISTINCT account_no FROM account_sections WHERE statement_id = s.id ORDER BY account_no)), s.customer_no, '') = ?
               ORDER BY
                 (s.file NOT LIKE '%/Duplicates/%') DESC,
                 (s.file NOT LIKE '%_01.pdf' AND s.file NOT LIKE '%_0.pdf' AND s.file NOT LIKE '%_1.pdf' AND s.file NOT LIKE '%(1).pdf') DESC,
                 s.reconciled DESC,
                 (s.file LIKE '%HSBC_Statement_%') DESC,
                 s.id DESC
               LIMIT 1""",
            (g["provider"], g["doc_type"], g["period_start"], g["period_end"], g["accts"]),
        ).fetchone()
        if best:
            db.execute("UPDATE statements SET current=1 WHERE id=?", (best["id"],))
    db.commit()


def statement_rows(db: sqlite3.Connection, provider=None, doc_type=None, current_only=True):
    q = "SELECT * FROM statements WHERE 1=1"
    args = []
    if provider:
        if provider == "hsbc":
            q += " AND provider LIKE 'hsbc%'"
        else:
            q += " AND (provider=? OR doc_type=?)"
            args.extend([provider, provider])
    if doc_type:
        q += " AND doc_type=?"
        args.append(doc_type)
    if current_only:
        q += " AND current=1"
    q += " ORDER BY period_end DESC, period_start DESC, id DESC"
    return [dict(r, data=json.loads(r["data"])) for r in db.execute(q, args).fetchall()]

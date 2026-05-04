"""SQLite schema and connection helpers.

Every connection opens with WAL mode so the poller can write while the
dashboard reads, without `database is locked` errors. Every connection
also enables foreign keys (off by default in SQLite).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path("finance.db")


def get_connection(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + FK enforcement.

    Callers are responsible for closing. Prefer the `connection()` context
    manager when you can.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connection(path: Path | str = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = get_connection(path)
    try:
        yield conn
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS email_accounts (
    id              INTEGER PRIMARY KEY,
    label           TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    imap_host       TEXT NOT NULL,
    imap_port       INTEGER NOT NULL DEFAULT 993,
    smtp_host       TEXT,
    smtp_port       INTEGER,
    last_polled_at  TIMESTAMP,
    status          TEXT NOT NULL DEFAULT 'ok',  -- 'ok' | 'error'
    -- IMAP app password. Plaintext; finance.db is local-only and the threat
    -- model matches a developer's .zshrc (anyone with disk access wins).
    -- Optional: if NULL, the poller falls back to APP_PASSWORD_<id> env var
    -- so accounts that pre-date this column keep working unchanged.
    app_password    TEXT
);

CREATE TABLE IF NOT EXISTS bank_accounts (
    id              INTEGER PRIMARY KEY,
    account_name    TEXT NOT NULL,    -- "ICICI Savings", "HDFC Credit Card"
    account_last4   TEXT,             -- "1234" or NULL
    owner           TEXT,             -- one of [owners].names from config.toml, or NULL
    UNIQUE(account_name, account_last4)
);

CREATE TABLE IF NOT EXISTS raw_emails (
    id                INTEGER PRIMARY KEY,
    email_account_id  INTEGER NOT NULL REFERENCES email_accounts(id),
    message_id        TEXT UNIQUE NOT NULL,
    subject           TEXT,
    from_addr         TEXT,
    body              TEXT,           -- raw HTML or plain text
    body_text         TEXT,           -- html2text stripped, used for LLM + verbatim check
    received_at       TIMESTAMP,
    parsed            INTEGER NOT NULL DEFAULT 0,   -- 0=unparsed | 1=ok or skipped | -1=failed after 3 retries
    parse_attempts    INTEGER NOT NULL DEFAULT 0,
    -- Independent of `parsed`: 0=bill scan not yet run, 1=scanned (bill or null).
    -- Lets the bill parser sweep historical emails without disturbing the
    -- transaction parsing flag.
    bills_parsed      INTEGER NOT NULL DEFAULT 0,
    -- Text extracted from unencrypted PDF attachments at fetch time
    -- (HDFC card statements, Tata Power bills, etc.). NULL if no PDF, no
    -- extractable text, or the PDF was encrypted (we skip those for now).
    attachment_text   TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY,
    raw_email_id      INTEGER NOT NULL REFERENCES raw_emails(id),
    bank_account_id   INTEGER NOT NULL REFERENCES bank_accounts(id),
    account_name      TEXT NOT NULL,    -- denormalized for query convenience
    account_last4     TEXT,
    merchant          TEXT,
    amount            REAL NOT NULL,    -- signed: negative=debit, positive=credit
    currency          TEXT NOT NULL DEFAULT 'INR',
    tx_date           DATE NOT NULL,
    category          TEXT,             -- LLM-assigned; always preserved
    manual_tag        TEXT,             -- NULL | 'savings' | 'expense' (user override)
    is_recurring      INTEGER NOT NULL DEFAULT 0,
    notes             TEXT,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- system_status: single-row table for poller heartbeat (D7).
-- Dashboard reads this to surface "Poller: stopped · last run 3h ago".
CREATE TABLE IF NOT EXISTS system_status (
    key           TEXT PRIMARY KEY,
    value         TEXT,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_transactions_tx_date
    ON transactions(tx_date);

CREATE INDEX IF NOT EXISTS idx_transactions_bank_account
    ON transactions(bank_account_id);

-- One transaction per parsed email (cross-process safe).
-- Concurrent parsers (e.g. CLI backfill while UI scheduler also runs) used to
-- produce duplicate transactions because the in-process _POLLER_LOCK doesn't
-- span processes. This UNIQUE INDEX + INSERT OR IGNORE in _persist_transaction
-- makes duplicate inserts a silent no-op regardless of which worker won the race.
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_unique_per_email
    ON transactions(raw_email_id);

-- Partial index: only the unparsed rows (the working set for the poller).
CREATE INDEX IF NOT EXISTS idx_raw_emails_parsed
    ON raw_emails(parsed) WHERE parsed = 0;

-- Functional unique index: SQLite's UNIQUE(...) treats NULL as distinct, so
-- the table-level UNIQUE(account_name, account_last4) does NOT dedup rows
-- where last4 is NULL. COALESCE collapses NULLs to '' for the uniqueness
-- check. This is the real dedup gate; the table-level UNIQUE is now mostly
-- documentation.
CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_accounts_uniq_with_null
    ON bank_accounts(account_name, COALESCE(account_last4, ''));

-- Bank account aliases: explicit user-driven equivalence between bank_accounts.
-- When the user merges a duplicate (e.g. SMS-extracted "ICICI Bank Account
-- ···1234" merging into the email-extracted "ICICI Bank Savings Account
-- ···91234"), an alias row is created so future incoming transactions matching
-- the merged-away (name, last4) auto-route to the canonical account.
-- Deleting an alias just stops the auto-routing — it does NOT restore the
-- deleted source bank_accounts row or move transactions back.
CREATE TABLE IF NOT EXISTS bank_account_aliases (
    id                         INTEGER PRIMARY KEY,
    alias_name                 TEXT NOT NULL,
    alias_last4                TEXT,
    canonical_bank_account_id  INTEGER NOT NULL REFERENCES bank_accounts(id),
    created_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Functional unique index — same NULL-safe pattern as bank_accounts.
CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_account_aliases_uniq
    ON bank_account_aliases(alias_name, COALESCE(alias_last4, ''));

CREATE INDEX IF NOT EXISTS idx_bank_account_aliases_canonical
    ON bank_account_aliases(canonical_bank_account_id);

-- Bill notifications: future obligations parsed from emails. Independent of
-- transactions — a bill is "money you'll owe", a transaction is "money that
-- moved". An incoming bill becomes a transaction when paid (via a separate
-- payment-receipt email), but those two rows are not auto-linked.
CREATE TABLE IF NOT EXISTS bills (
    id            INTEGER PRIMARY KEY,
    raw_email_id  INTEGER NOT NULL REFERENCES raw_emails(id),
    biller        TEXT NOT NULL,
    description   TEXT,
    amount        REAL NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'INR',
    due_date      DATE NOT NULL,
    status        TEXT NOT NULL DEFAULT 'unpaid',  -- 'unpaid' | 'paid'
    paid_at       TIMESTAMP,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Reminder emails for the same bill (initial notice + reminder + final
-- reminder) all carry the same biller/due_date/amount. INSERT OR IGNORE
-- against this index makes the reminder a silent no-op.
CREATE UNIQUE INDEX IF NOT EXISTS idx_bills_unique
    ON bills(biller, due_date, amount);

CREATE INDEX IF NOT EXISTS idx_bills_status_due
    ON bills(status, due_date);
"""


def _migrate_email_accounts_app_password(conn: sqlite3.Connection) -> None:
    """Add email_accounts.app_password to existing DBs.

    SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so we check
    PRAGMA table_info first. Idempotent — safe to call on every startup.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(email_accounts)")}
    if "app_password" not in cols:
        conn.execute("ALTER TABLE email_accounts ADD COLUMN app_password TEXT")


def _migrate_raw_emails_bills_parsed(conn: sqlite3.Connection) -> None:
    """Add raw_emails.bills_parsed to existing DBs."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(raw_emails)")}
    if "bills_parsed" not in cols:
        conn.execute(
            "ALTER TABLE raw_emails ADD COLUMN bills_parsed INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_raw_emails_attachment_text(conn: sqlite3.Connection) -> None:
    """Add raw_emails.attachment_text — extracted text from any unencrypted
    PDF attachments (HDFC card statements, Tata Power bills, etc.). Encrypted
    PDFs are skipped; the column stays empty for those rows."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(raw_emails)")}
    if "attachment_text" not in cols:
        conn.execute("ALTER TABLE raw_emails ADD COLUMN attachment_text TEXT")


def init_schema(path: Path | str = DB_PATH) -> None:
    with connection(path) as conn:
        conn.executescript(SCHEMA)
        _migrate_email_accounts_app_password(conn)
        _migrate_raw_emails_bills_parsed(conn)
        _migrate_raw_emails_attachment_text(conn)


def upsert_bank_account(
    conn: sqlite3.Connection,
    account_name: str,
    account_last4: str | None,
) -> int:
    """Auto-create a bank_account row at parse time. Returns the row id.

    Lookup order:
      1. Exact `(account_name, account_last4)` match in bank_accounts → that id
      2. Exact `(name, last4)` match in bank_account_aliases → the canonical id
         it points to (lets a previously-merged variant auto-route to its
         canonical account without re-merging)
      3. Otherwise INSERT a fresh bank_accounts row

    Step 2 is what makes merge "stick" for future SMS imports: once the user
    merges variant V into canonical C, deleting V from bank_accounts and
    inserting an alias (V.name, V.last4) → C, all subsequent upserts for V's
    identity go straight to C until the alias is deleted.

    SELECT-first pattern (NOT `INSERT OR IGNORE`) because SQLite's UNIQUE
    constraint treats NULL != NULL, so we handle the NULL last4 case explicitly
    with `IS NULL`. The functional unique index on
    `(account_name, COALESCE(account_last4, ''))` provides defense-in-depth.
    """
    # Step 1: real account, exact match
    if account_last4 is None:
        row = conn.execute(
            "SELECT id FROM bank_accounts WHERE account_name = ? AND account_last4 IS NULL",
            (account_name,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM bank_accounts WHERE account_name = ? AND account_last4 = ?",
            (account_name, account_last4),
        ).fetchone()
    if row:
        return row["id"]

    # Step 2: alias fallback
    if account_last4 is None:
        alias = conn.execute(
            "SELECT canonical_bank_account_id FROM bank_account_aliases "
            "WHERE alias_name = ? AND alias_last4 IS NULL",
            (account_name,),
        ).fetchone()
    else:
        alias = conn.execute(
            "SELECT canonical_bank_account_id FROM bank_account_aliases "
            "WHERE alias_name = ? AND alias_last4 = ?",
            (account_name, account_last4),
        ).fetchone()
    if alias:
        return alias["canonical_bank_account_id"]

    # Step 3: insert new
    cur = conn.execute(
        "INSERT INTO bank_accounts (account_name, account_last4) VALUES (?, ?)",
        (account_name, account_last4),
    )
    return cur.lastrowid


def set_poller_status(value: str, path: Path | str = DB_PATH) -> None:
    """Heartbeat write. value should be 'running' or 'stopped' (D7)."""
    with connection(path) as conn:
        conn.execute(
            "INSERT INTO system_status (key, value, updated_at) VALUES ('poller', ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (value,),
        )


def get_poller_status(path: Path | str = DB_PATH) -> tuple[str, str] | None:
    """Returns (value, updated_at_iso) or None if never set."""
    with connection(path) as conn:
        row = conn.execute(
            "SELECT value, updated_at FROM system_status WHERE key = 'poller'"
        ).fetchone()
        if not row:
            return None
        return row["value"], row["updated_at"]


def set_poller_progress(
    phase: str, detail: str = "", path: Path | str = DB_PATH
) -> None:
    """Record live progress of an in-flight poll.

    `phase` is one of: 'idle', 'fetching', 'parsing', 'error'.
    `detail` is a free-form string the UI displays as-is, e.g.
    "Fetching from user@example.com" or "Parsed 5 of 12 emails".
    """
    with connection(path) as conn:
        for k, v in (("poller_phase", phase), ("poller_detail", detail)):
            conn.execute(
                "INSERT INTO system_status (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=CURRENT_TIMESTAMP",
                (k, v),
            )


def get_poller_progress(path: Path | str = DB_PATH) -> tuple[str, str, str] | None:
    """Returns (phase, detail, updated_at_iso) or None if never set."""
    with connection(path) as conn:
        rows = {
            r["key"]: (r["value"], r["updated_at"])
            for r in conn.execute(
                "SELECT key, value, updated_at FROM system_status "
                "WHERE key IN ('poller_phase', 'poller_detail')"
            )
        }
        if "poller_phase" not in rows:
            return None
        phase, ts = rows["poller_phase"]
        detail = rows.get("poller_detail", ("", ts))[0]
        return phase, detail, ts

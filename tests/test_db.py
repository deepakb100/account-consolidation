"""Day 1 critical tests — DB."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from db import (
    connection,
    get_connection,
    get_poller_progress,
    get_poller_status,
    set_poller_progress,
    set_poller_status,
    upsert_bank_account,
)


# ---------- Test 6 — message_id UNIQUE prevents duplicate inserts ------------

def test_message_id_dedup(tmp_db: Path):
    with connection(tmp_db) as conn:
        # Need an email_account row for the FK.
        conn.execute(
            "INSERT INTO email_accounts (label, email, imap_host) VALUES ('test', 'a@b.com', 'imap.example.com')"
        )

        # First insert — should succeed.
        conn.execute(
            "INSERT INTO raw_emails (email_account_id, message_id, subject, body, body_text) "
            "VALUES (1, 'abc123', 'Test', 'body', 'body')"
        )

        # Second insert with same message_id should be a no-op.
        # We use INSERT OR IGNORE because that's what poller.py will do.
        conn.execute(
            "INSERT OR IGNORE INTO raw_emails (email_account_id, message_id, subject, body, body_text) "
            "VALUES (1, 'abc123', 'Test', 'body', 'body')"
        )

        rows = conn.execute("SELECT COUNT(*) FROM raw_emails WHERE message_id = 'abc123'").fetchone()
        assert rows[0] == 1, "dedup failed: expected 1 row, got more"


def test_message_id_dedup_raises_without_or_ignore(tmp_db: Path):
    """Confirm the UNIQUE constraint is real (not just compliant with OR IGNORE)."""
    with connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO email_accounts (label, email, imap_host) VALUES ('test', 'a@b.com', 'imap.example.com')"
        )
        conn.execute(
            "INSERT INTO raw_emails (email_account_id, message_id, subject, body, body_text) "
            "VALUES (1, 'msgid', 'a', 'b', 'c')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO raw_emails (email_account_id, message_id, subject, body, body_text) "
                "VALUES (1, 'msgid', 'a', 'b', 'c')"
            )


# ---------- Test 7 — WAL mode lets reads run concurrently with writes --------

def test_wal_concurrent_read_write(tmp_db: Path):
    """conn1 holds a write transaction; conn2 reads concurrently.

    Without WAL mode this would either block, time out, or fail with
    "database is locked". With WAL, the read sees the last committed
    snapshot and returns immediately.
    """
    # Seed a row so the SELECT has something to find.
    with connection(tmp_db) as seed:
        seed.execute(
            "INSERT INTO email_accounts (label, email, imap_host) VALUES ('seed', 's@s.com', 'imap.example.com')"
        )

    conn1 = get_connection(tmp_db)
    conn2 = get_connection(tmp_db)
    try:
        # conn1 starts an explicit transaction and writes.
        conn1.execute("BEGIN IMMEDIATE")
        conn1.execute(
            "INSERT INTO email_accounts (label, email, imap_host) VALUES ('writer', 'w@w.com', 'imap.example.com')"
        )

        # conn2 reads while conn1's write transaction is still open.
        # In WAL mode this is allowed and returns the last committed snapshot
        # (so it sees 'seed' but not the not-yet-committed 'writer').
        result = conn2.execute(
            "SELECT label FROM email_accounts WHERE label = 'seed'"
        ).fetchone()
        assert result is not None
        assert result["label"] == "seed"

        # Should NOT see the uncommitted 'writer' row.
        not_visible = conn2.execute(
            "SELECT label FROM email_accounts WHERE label = 'writer'"
        ).fetchone()
        assert not_visible is None

        conn1.execute("COMMIT")
    finally:
        conn1.close()
        conn2.close()


# ---------- Bonus: bank_accounts UPSERT --------------------------------------

def test_upsert_bank_account_creates_then_returns_same_id(tmp_db: Path):
    with connection(tmp_db) as conn:
        id1 = upsert_bank_account(conn, "ICICI Savings", "4400")
        id2 = upsert_bank_account(conn, "ICICI Savings", "4400")
        assert id1 == id2

        # Different last4 -> different row.
        id3 = upsert_bank_account(conn, "ICICI Savings", "9999")
        assert id3 != id1

        # NULL last4 is its own row.
        id4 = upsert_bank_account(conn, "ICICI Savings", None)
        assert id4 not in (id1, id3)


def test_upsert_bank_account_dedupes_null_last4(tmp_db: Path):
    """Regression: SQLite's UNIQUE constraint treats NULL != NULL, so the
    original `INSERT OR IGNORE` pattern created duplicate rows when
    `account_last4` was NULL. The fix uses an explicit SELECT-first lookup
    backed by a functional UNIQUE INDEX on COALESCE(account_last4, '').
    """
    with connection(tmp_db) as conn:
        id1 = upsert_bank_account(conn, "HDFC Bank Account", None)
        id2 = upsert_bank_account(conn, "HDFC Bank Account", None)
        id3 = upsert_bank_account(conn, "HDFC Bank Account", None)
        assert id1 == id2 == id3, "NULL last4 should still dedup"

        n = conn.execute(
            "SELECT COUNT(*) FROM bank_accounts WHERE account_name = 'HDFC Bank Account'"
        ).fetchone()[0]
        assert n == 1, f"expected 1 row, found {n}"


def test_functional_unique_index_blocks_direct_dup_insert(tmp_db: Path):
    """Defense-in-depth: if a future caller bypasses upsert_bank_account()
    and INSERTs directly, the functional UNIQUE INDEX on
    (account_name, COALESCE(account_last4, '')) must reject the duplicate.
    """
    import sqlite3
    with connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO bank_accounts (account_name, account_last4) VALUES ('HDFC', NULL)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO bank_accounts (account_name, account_last4) VALUES ('HDFC', NULL)"
            )


def test_poller_status_heartbeat(tmp_db: Path):
    set_poller_status("running", tmp_db)
    val, ts = get_poller_status(tmp_db)
    assert val == "running"
    assert ts is not None

    set_poller_status("stopped", tmp_db)
    val2, _ = get_poller_status(tmp_db)
    assert val2 == "stopped"


def test_unique_transaction_per_raw_email(tmp_db: Path):
    """Regression: cross-process concurrent parsers used to produce duplicate
    transactions for the same raw_email. UNIQUE INDEX on raw_email_id +
    INSERT OR IGNORE in _persist_transaction makes the second insert a no-op.
    """
    with connection(tmp_db) as db:
        db.execute(
            "INSERT INTO email_accounts (label, email, imap_host) VALUES ('t','a@b.com','x')"
        )
        db.execute(
            "INSERT INTO raw_emails (email_account_id, message_id, body, body_text) "
            "VALUES (1, 'mid', 'b', 'b')"
        )
        bid = upsert_bank_account(db, "ICICI Bank Credit Card", "2012")

        # First insert succeeds
        db.execute(
            "INSERT OR IGNORE INTO transactions "
            "(raw_email_id, bank_account_id, account_name, account_last4, merchant, amount, currency, tx_date) "
            "VALUES (1, ?, 'ICICI Bank Credit Card', '2012', 'LEVIS', -1412, 'INR', '2026-04-26')",
            (bid,),
        )
        # Second insert (simulating a concurrent worker) silently no-ops
        db.execute(
            "INSERT OR IGNORE INTO transactions "
            "(raw_email_id, bank_account_id, account_name, account_last4, merchant, amount, currency, tx_date) "
            "VALUES (1, ?, 'ICICI Bank Credit Card', '2012', 'LEVIS', -1412, 'INR', '2026-04-26')",
            (bid,),
        )

        n = db.execute("SELECT COUNT(*) FROM transactions WHERE raw_email_id = 1").fetchone()[0]
        assert n == 1, f"expected exactly 1 transaction for raw_email_id=1, got {n}"


def test_unique_constraint_blocks_direct_dup_insert(tmp_db: Path):
    """Defense-in-depth: if a future caller bypasses INSERT OR IGNORE and
    just INSERTs, the UNIQUE INDEX must reject the duplicate."""
    import sqlite3
    with connection(tmp_db) as db:
        db.execute(
            "INSERT INTO email_accounts (label, email, imap_host) VALUES ('t','a@b.com','x')"
        )
        db.execute(
            "INSERT INTO raw_emails (email_account_id, message_id, body, body_text) "
            "VALUES (1, 'mid', 'b', 'b')"
        )
        bid = upsert_bank_account(db, "X", "1234")
        db.execute(
            "INSERT INTO transactions "
            "(raw_email_id, bank_account_id, account_name, merchant, amount, tx_date) "
            "VALUES (1, ?, 'X', 'M', -100, '2026-04-26')",
            (bid,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO transactions "
                "(raw_email_id, bank_account_id, account_name, merchant, amount, tx_date) "
                "VALUES (1, ?, 'X', 'M', -100, '2026-04-26')",
                (bid,),
            )


def test_poller_progress_roundtrip(tmp_db: Path):
    """Live progress writes (phase + detail) round-trip through system_status."""
    # No progress recorded yet -> None
    assert get_poller_progress(tmp_db) is None

    set_poller_progress("fetching", "Fetching from user@example.com [1/2]", tmp_db)
    phase, detail, ts = get_poller_progress(tmp_db)
    assert phase == "fetching"
    assert "user@example.com" in detail
    assert ts is not None

    # Update both fields
    set_poller_progress("parsing", "Parsing 5 of 12 emails", tmp_db)
    phase, detail, _ = get_poller_progress(tmp_db)
    assert phase == "parsing"
    assert detail == "Parsing 5 of 12 emails"

    # Empty detail is fine — phase alone is meaningful
    set_poller_progress("idle", "", tmp_db)
    phase, detail, _ = get_poller_progress(tmp_db)
    assert phase == "idle"
    assert detail == ""


def test_repoint_then_delete_bank_account(tmp_db: Path):
    """Validates the DB primitive used by /transactions/{id}/move's
    auto-cleanup path: when all transactions move OFF a bank_account, the
    empty bank_account row can be DELETEd without violating the FK constraint.
    """
    with connection(tmp_db) as db:
        # Setup: 1 email_account, 1 raw_email, 2 bank_accounts, 3 transactions
        # (2 on source, 1 on target — only the 2 should move)
        db.execute(
            "INSERT INTO email_accounts (label, email, imap_host) VALUES ('t','a@b.com','imap.x.com')"
        )
        # Insert 3 distinct raw_emails so each transaction below can have its
        # own unique raw_email_id (required by the UNIQUE INDEX added to
        # prevent concurrent-parser duplicates).
        for i in range(1, 4):
            db.execute(
                "INSERT INTO raw_emails (email_account_id, message_id, body, body_text) "
                "VALUES (1, ?, 'b', 'b')",
                (f"mid-{i}",),
            )
        source_id = upsert_bank_account(db, "HDFC Bank Account", None)        # orphan
        target_id = upsert_bank_account(db, "HDFC Bank Account", "1100")      # canonical
        # 2 transactions on the source (orphan), each tied to its own raw_email
        for raw_email_id in (1, 2):
            db.execute(
                "INSERT INTO transactions (raw_email_id, bank_account_id, account_name, "
                "merchant, amount, currency, tx_date) "
                "VALUES (?, ?, 'HDFC Bank Account', 'beneficiary', -200000, 'INR', '2026-04-23')",
                (raw_email_id, source_id),
            )
        # 1 transaction already on the target (raw_email_id=3)
        db.execute(
            "INSERT INTO transactions (raw_email_id, bank_account_id, account_name, "
            "account_last4, merchant, amount, currency, tx_date) "
            "VALUES (3, ?, 'HDFC Bank Account', '1100', 'Sample Payee', -300000, 'INR', '2026-04-23')",
            (target_id,),
        )

        # Sanity check before merge
        before = db.execute(
            "SELECT bank_account_id, COUNT(*) FROM transactions GROUP BY bank_account_id"
        ).fetchall()
        before_map = {r[0]: r[1] for r in before}
        assert before_map[source_id] == 2
        assert before_map[target_id] == 1

        # Simulate the merge route's body
        db.execute(
            "UPDATE transactions SET bank_account_id = ? WHERE bank_account_id = ?",
            (target_id, source_id),
        )
        db.execute("DELETE FROM bank_accounts WHERE id = ?", (source_id,))

        # After merge: source row gone, target row has all 3 transactions
        remaining = db.execute(
            "SELECT id FROM bank_accounts WHERE id = ?", (source_id,)
        ).fetchone()
        assert remaining is None, "source bank_account row should be deleted"

        moved = db.execute(
            "SELECT COUNT(*) FROM transactions WHERE bank_account_id = ?", (target_id,)
        ).fetchone()[0]
        assert moved == 3, f"expected 3 transactions on target, found {moved}"

        orphaned = db.execute(
            "SELECT COUNT(*) FROM transactions WHERE bank_account_id = ?", (source_id,)
        ).fetchone()[0]
        assert orphaned == 0, "no transactions should still reference deleted source"

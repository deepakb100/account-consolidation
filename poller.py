"""IMAP poller + Ollama parse loop.

Two modes:
  python poller.py            # one-shot: fetch new mail, parse the unparsed queue, exit
  python poller.py --backfill # async: fetch the last 90 days, parse with Semaphore(3) cap

Pipeline (per email account):
  imaplib.IMAP4_SSL.login()         # 3x retry w/ backoff on failure
        |
        v
  search SINCE last_polled_at       # only new mail
        |
        v
  fetch + parse Message-Id, Subject, From, Body
        |
        v
  html2text strip                   # store raw body + body_text
        |
        v
  INSERT OR IGNORE raw_emails       # message_id UNIQUE -> dedup
        |
        v
  parse_email() (LLM)               # parse_attempts++, parsed=1 on success, =-1 after 3 failures
        |
        v
  upsert_bank_account + INSERT transactions
"""
from __future__ import annotations

import argparse
import asyncio
import email
import imaplib
import logging
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message
from pathlib import Path
from typing import Iterable

import html2text

from db import (
    connection,
    init_schema,
    set_poller_progress,
    set_poller_status,
    upsert_bank_account,
)
from parser import parse_bill, parse_email, signed_amount

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)
log = logging.getLogger("poller")

CONFIG_PATH = Path("config.toml")
BACKOFF_DELAYS_S = (5, 15, 45)  # 3x retry on IMAP failure (D from design doc)
PARSE_MAX_ATTEMPTS = 3
ASYNC_PARSE_CONCURRENCY = 3       # D10 — Semaphore cap for backfill
ASYNC_PARSE_TIMEOUT_S = 30.0      # critical gap fix — per-call timeout to prevent hang
IMAP_TIMEOUT_S = 60               # socket timeout — without this, a silently-dropped
                                  # TCP connection makes imap.fetch() block forever


# --- config + Ollama plumbing ------------------------------------------------


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.warning("config.toml not found; using defaults. Copy config.toml.example.")
        return {"llm": {"provider": "ollama", "model": "llama3.2", "host": "http://localhost:11434"}}
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def _ollama_call_factory(cfg: dict):
    """Build a synchronous ollama_call(prompt) -> str closure.

    Uses the `ollama` Python client. Falls back to a clear startup error if
    the host is unreachable (D from design doc — fail fast, do NOT silently
    fall through to OpenAI).
    """
    import ollama  # imported lazily so tests don't need the package

    client = ollama.Client(host=cfg["llm"]["host"])
    model = cfg["llm"]["model"]

    # Startup ping — fail fast if Ollama isn't reachable.
    try:
        client.list()
    except Exception as e:
        raise RuntimeError(
            f"Ollama unreachable at {cfg['llm']['host']}: {e}\n"
            "Start it with `ollama serve` and confirm `ollama list` works."
        ) from e

    def _call(prompt: str) -> str:
        resp = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0},
        )
        return resp["message"]["content"]

    return _call


# --- IMAP fetch --------------------------------------------------------------


def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _extract_body(msg: Message) -> tuple[str, str]:
    """Returns (body_raw, body_text). Prefer text/html when present
    (banks send formatted alerts), fall back to text/plain.
    """
    raw = ""
    if msg.is_multipart():
        # Walk parts; prefer text/html.
        html_part = None
        text_part = None
        for part in msg.walk():
            ct = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            if ct == "text/html" and html_part is None:
                html_part = part
            elif ct == "text/plain" and text_part is None:
                text_part = part
        chosen = html_part or text_part
        if chosen is not None:
            payload = chosen.get_payload(decode=True) or b""
            charset = chosen.get_content_charset() or "utf-8"
            raw = payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        raw = payload.decode(charset, errors="replace")

    h = html2text.HTML2Text()
    h.ignore_links = True
    h.ignore_images = True
    body_text = h.handle(raw) if raw else ""
    return raw, body_text


def _extract_pdf_attachment_text(msg: Message) -> str | None:
    """Extract concatenated text from any unencrypted PDF attachments.

    Returns None when there are no PDF attachments at all (so callers can
    distinguish 'no PDF' from 'PDF but no text'). Returns an empty string
    if every PDF was encrypted or unparseable.

    Encrypted PDFs are skipped silently — passwords are out of scope per
    the v1 spec. They're noted as `[encrypted: filename]` so the bill parser
    can at least see that *something* was attached.
    """
    from io import BytesIO
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    chunks: list[str] = []
    found_any_pdf = False

    for part in msg.walk():
        ct = (part.get_content_type() or "").lower()
        filename = part.get_filename() or ""
        is_pdf = ct == "application/pdf" or filename.lower().endswith(".pdf")
        if not is_pdf:
            continue
        found_any_pdf = True

        payload = part.get_payload(decode=True)
        if not payload:
            continue
        try:
            reader = PdfReader(BytesIO(payload))
        except (PdfReadError, Exception) as e:
            chunks.append(f"[PDF parse failed: {filename}: {type(e).__name__}]")
            continue
        if reader.is_encrypted:
            # Skip — password handling is deliberately out of scope.
            chunks.append(f"[encrypted PDF skipped: {filename}]")
            continue
        try:
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:
            chunks.append(f"[PDF text extraction failed: {filename}: {type(e).__name__}]")
            continue
        # Mark each PDF with its filename so downstream parsing has context.
        chunks.append(f"[PDF: {filename}]\n{text.strip()}")

    if not found_any_pdf:
        return None
    return "\n\n".join(chunks)


def _imap_login_with_backoff(host: str, port: int, user: str, password: str) -> imaplib.IMAP4_SSL:
    last: Exception | None = None
    for i, delay in enumerate((0,) + BACKOFF_DELAYS_S, start=1):
        if delay:
            log.info("IMAP retry in %ds (attempt %d/%d)", delay, i, len(BACKOFF_DELAYS_S) + 1)
            time.sleep(delay)
        try:
            conn = imaplib.IMAP4_SSL(host, port, timeout=IMAP_TIMEOUT_S)
            conn.login(user, password)
            return conn
        except Exception as e:
            last = e
            log.warning("IMAP login failed: %s", e)
    raise RuntimeError(f"IMAP login to {host} failed after {len(BACKOFF_DELAYS_S) + 1} attempts: {last}")


def imap_fetch_new(
    account: dict,
    app_password: str,
    days_back: int = 7,
    *,
    force_window: bool = False,
) -> int:
    """Connect to one IMAP account, fetch new messages, INSERT OR IGNORE for dedup.

    By default the window is from `last_polled_at` (incremental poll). When
    `force_window=True`, the window is exactly `days_back` days from now,
    ignoring `last_polled_at`. Backfill mode passes force_window=True so
    `--days N` actually means "the last N days".

    Returns count of NEW rows inserted (after dedup).
    """
    if force_window:
        since_dt = datetime.now(timezone.utc) - timedelta(days=days_back)
    else:
        since = account.get("last_polled_at")
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
            except Exception:
                since_dt = datetime.now(timezone.utc) - timedelta(days=days_back)
        else:
            since_dt = datetime.now(timezone.utc) - timedelta(days=days_back)

    try:
        imap = _imap_login_with_backoff(
            account["imap_host"], account["imap_port"],
            account["email"], app_password,
        )
    except RuntimeError as e:
        log.error("Marking %s as error: %s", account["email"], e)
        with connection() as conn:
            conn.execute(
                "UPDATE email_accounts SET status = 'error' WHERE id = ?",
                (account["id"],),
            )
        return 0

    inserted = 0
    try:
        imap.select("INBOX", readonly=True)
        # IMAP SINCE date format: DD-Mon-YYYY
        since_str = since_dt.strftime("%d-%b-%Y")
        typ, data = imap.search(None, f'(SINCE "{since_str}")')
        if typ != "OK":
            log.error("IMAP SEARCH failed for %s", account["email"])
            return 0

        ids = data[0].split() if data and data[0] else []
        log.info("%s: %d messages since %s", account["email"], len(ids), since_str)

        aborted = False
        with connection() as db:
            for msg_id in ids:
                try:
                    typ, data = imap.fetch(msg_id, "(RFC822)")
                except (OSError, imaplib.IMAP4.abort) as e:
                    # Socket timeout / dropped connection: imap state is now
                    # unreliable, so stop the loop and leave last_polled_at
                    # unchanged so the next cycle retries this window.
                    log.error("IMAP fetch aborted for %s after %d new rows: %s",
                              account["email"], inserted, e)
                    aborted = True
                    break
                if typ != "OK" or not data or data[0] is None:
                    continue
                raw_bytes = data[0][1]
                msg = email.message_from_bytes(raw_bytes)
                message_id = (msg.get("Message-Id") or msg.get("Message-ID") or "").strip()
                if not message_id:
                    # Skip messages without an ID; we can't dedup them safely.
                    continue
                subject = _decode_header(msg.get("Subject"))
                from_addr = _decode_header(msg.get("From"))
                date_str = msg.get("Date") or ""
                received_at = email.utils.parsedate_to_datetime(date_str) if date_str else datetime.now(timezone.utc)
                body_raw, body_text = _extract_body(msg)
                attachment_text = _extract_pdf_attachment_text(msg)

                cur = db.execute(
                    "INSERT OR IGNORE INTO raw_emails "
                    "(email_account_id, message_id, subject, from_addr, body, body_text, "
                    " attachment_text, received_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (account["id"], message_id, subject, from_addr, body_raw, body_text,
                     attachment_text, received_at.isoformat()),
                )
                if cur.rowcount:
                    inserted += 1

            if not aborted:
                db.execute(
                    "UPDATE email_accounts SET last_polled_at = CURRENT_TIMESTAMP, status = 'ok' WHERE id = ?",
                    (account["id"],),
                )
    finally:
        try:
            imap.close()
            imap.logout()
        except Exception:
            pass

    log.info("%s: %d new emails inserted", account["email"], inserted)
    return inserted


# --- parse loop --------------------------------------------------------------


def _persist_transaction(db, raw_email_id: int, tx) -> None:
    """Insert one transaction. INSERT OR IGNORE makes this safe for concurrent
    parsers — the UNIQUE INDEX on raw_email_id ensures one transaction per
    email even if multiple workers race to claim the same row."""
    bank_id = upsert_bank_account(db, tx.account_name, tx.account_last4)
    db.execute(
        "INSERT OR IGNORE INTO transactions "
        "(raw_email_id, bank_account_id, account_name, account_last4, merchant, amount, currency, tx_date, category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            raw_email_id, bank_id, tx.account_name, tx.account_last4,
            tx.merchant, signed_amount(tx), tx.currency, tx.date.isoformat(), tx.category,
        ),
    )


def _persist_bill(db, raw_email_id: int, bill) -> None:
    """Insert one bill. INSERT OR IGNORE makes reminder emails (multiple
    raw_emails for the same bill) silent no-ops via the unique index on
    (biller, due_date, amount)."""
    db.execute(
        "INSERT OR IGNORE INTO bills "
        "(raw_email_id, biller, description, amount, currency, due_date) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            raw_email_id, bill.biller, bill.description,
            bill.amount, bill.currency, bill.due_date.isoformat(),
        ),
    )


def _fetch_unparsed(db, limit: int | None = None):
    sql = (
        "SELECT id, subject, from_addr, body_text, parse_attempts, received_at FROM raw_emails "
        "WHERE parsed = 0 AND parse_attempts < ? "
        "ORDER BY received_at ASC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.execute(sql, (PARSE_MAX_ATTEMPTS,)).fetchall()


def _received_date(row) -> "date | None":
    """Parse the row's received_at string into a date, or None on failure."""
    raw = row["received_at"] if "received_at" in row.keys() else None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace(" ", "T")).date()
    except Exception:
        return None


def parse_unparsed_queue(ollama_call) -> tuple[int, int, int]:
    """Iterate raw_emails where parsed=0, run parse_email().
    Returns (parsed_ok, skipped, failed_terminal).
    """
    ok = skipped = terminal = 0
    with connection() as db:
        rows = _fetch_unparsed(db)
        for row in rows:
            result = parse_email(
                from_addr=row["from_addr"],
                subject=row["subject"],
                body_text=row["body_text"] or "",
                ollama_call=ollama_call,
                received_at=_received_date(row),
            )
            if result.skipped:
                # Allowlist rejected: mark as parsed=1 (not a transaction, no retry).
                db.execute("UPDATE raw_emails SET parsed = 1 WHERE id = ?", (row["id"],))
                skipped += 1
            elif result.transaction is not None:
                _persist_transaction(db, row["id"], result.transaction)
                db.execute("UPDATE raw_emails SET parsed = 1 WHERE id = ?", (row["id"],))
                ok += 1
            else:
                attempts = row["parse_attempts"] + 1
                new_status = -1 if attempts >= PARSE_MAX_ATTEMPTS else 0
                db.execute(
                    "UPDATE raw_emails SET parse_attempts = ?, parsed = ? WHERE id = ?",
                    (attempts, new_status, row["id"]),
                )
                if new_status == -1:
                    terminal += 1
                    log.warning("raw_emails id=%d permanently failed: %s", row["id"], result.reason)

    log.info("parse loop: ok=%d skipped=%d terminal=%d", ok, skipped, terminal)
    return ok, skipped, terminal


# --- async backfill ----------------------------------------------------------


async def parse_unparsed_queue_async(ollama_call) -> tuple[int, int, int]:
    """Concurrent parser for the 90-day backfill (D10).

    Wraps the sync ollama_call with asyncio.to_thread so it doesn't block
    the event loop. asyncio.wait_for prevents one stuck Ollama call from
    hanging the whole gather.
    """
    sem = asyncio.Semaphore(ASYNC_PARSE_CONCURRENCY)
    ok = skipped = terminal = 0

    with connection() as db:
        rows = _fetch_unparsed(db)
        log.info("async backfill: %d emails to parse", len(rows))

        async def _one(row):
            async with sem:
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(
                            parse_email,
                            from_addr=row["from_addr"],
                            subject=row["subject"],
                            body_text=row["body_text"] or "",
                            ollama_call=ollama_call,
                            received_at=_received_date(row),
                        ),
                        timeout=ASYNC_PARSE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    log.warning("ollama call timed out on raw_emails id=%d", row["id"])
                    return None  # treated as a parse failure

        results = await asyncio.gather(*(_one(r) for r in rows))

        for row, result in zip(rows, results):
            if result is None:
                # timeout -> count as one attempt
                attempts = row["parse_attempts"] + 1
                new_status = -1 if attempts >= PARSE_MAX_ATTEMPTS else 0
                db.execute(
                    "UPDATE raw_emails SET parse_attempts = ?, parsed = ? WHERE id = ?",
                    (attempts, new_status, row["id"]),
                )
                if new_status == -1:
                    terminal += 1
                continue

            if result.skipped:
                db.execute("UPDATE raw_emails SET parsed = 1 WHERE id = ?", (row["id"],))
                skipped += 1
            elif result.transaction is not None:
                _persist_transaction(db, row["id"], result.transaction)
                db.execute("UPDATE raw_emails SET parsed = 1 WHERE id = ?", (row["id"],))
                ok += 1
            else:
                attempts = row["parse_attempts"] + 1
                new_status = -1 if attempts >= PARSE_MAX_ATTEMPTS else 0
                db.execute(
                    "UPDATE raw_emails SET parse_attempts = ?, parsed = ? WHERE id = ?",
                    (attempts, new_status, row["id"]),
                )
                if new_status == -1:
                    terminal += 1

    log.info("async parse: ok=%d skipped=%d terminal=%d", ok, skipped, terminal)
    return ok, skipped, terminal


# --- bill scan queue (runs after the transaction parser) ----------------

def _fetch_bills_unscanned(db, limit: int | None = None):
    """raw_emails awaiting a bill-extraction pass.

    Restricted to emails the transaction parser has already processed
    (parsed=1) — the bill path is a fallthrough, never the primary classifier.
    Selects attachment_text so PDF-only bills (HDFC card statements, Tata
    Power, etc.) are visible to the LLM.
    """
    sql = (
        "SELECT id, subject, from_addr, body_text, attachment_text "
        "FROM raw_emails "
        "WHERE bills_parsed = 0 AND parsed = 1 "
        "ORDER BY received_at ASC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.execute(sql).fetchall()


def _bill_corpus(row) -> str:
    """Combine email body + PDF attachment text for the bill LLM.

    Body alone misses statement-only emails ("your statement is attached");
    attachment alone misses inline bills (Airtel Fiber). Concatenate so the
    LLM sees both.
    """
    body = row["body_text"] or ""
    attach = row["attachment_text"] if "attachment_text" in row.keys() else None
    if attach:
        return f"{body}\n\n=== PDF ATTACHMENT TEXT ===\n{attach}"
    return body


async def parse_bills_queue_async(ollama_call) -> tuple[int, int]:
    """Concurrent bill-extraction pass over raw_emails where bills_parsed=0.

    Independent of the transaction parser: by the time we run, those emails
    already have parsed=1 (they're either a transaction or were skipped).
    Skipping the bill check on transaction-already emails would miss the rare
    case where an email is BOTH a payment receipt AND a future bill notice,
    but those are vanishingly rare in practice and would just produce one
    spurious bill. Better simplicity than a special case.

    Returns (ok, skipped). bills_parsed = 1 after every attempt regardless
    of outcome — we don't retry; no per-email retry counter for v1.
    """
    sem = asyncio.Semaphore(ASYNC_PARSE_CONCURRENCY)
    ok = skipped = 0

    with connection() as db:
        rows = _fetch_bills_unscanned(db)
        log.info("bill scan: %d emails to check", len(rows))

        async def _one(row):
            async with sem:
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(
                            parse_bill,
                            from_addr=row["from_addr"],
                            subject=row["subject"],
                            body_text=_bill_corpus(row),
                            ollama_call=ollama_call,
                        ),
                        timeout=ASYNC_PARSE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    log.warning("bill ollama timed out on raw_emails id=%d", row["id"])
                    return None

        results = await asyncio.gather(*(_one(r) for r in rows))

        for row, result in zip(rows, results):
            # Mark scanned regardless — no retry policy for bills in v1.
            if result is not None and result.bill is not None:
                _persist_bill(db, row["id"], result.bill)
                ok += 1
            else:
                skipped += 1
            db.execute(
                "UPDATE raw_emails SET bills_parsed = 1 WHERE id = ?",
                (row["id"],),
            )

    log.info("bill scan: ok=%d skipped=%d", ok, skipped)
    return ok, skipped


# --- main entry --------------------------------------------------------------


def _list_email_accounts() -> list[dict]:
    with connection() as db:
        rows = db.execute("SELECT * FROM email_accounts WHERE status != 'disabled'").fetchall()
        return [dict(r) for r in rows]


def _app_password_for(account: dict) -> str | None:
    """Resolve the IMAP app password for one account.

    Priority:
      1. `APP_PASSWORD_<id>` env var — preserved so existing accounts (1, 2)
         keep working from the env vars baked into the running server process.
      2. `APP_PASSWORD` env var — single-account fallback.
      3. `email_accounts.app_password` DB column — populated when the account
         is created via the Settings form. Lets new accounts work without a
         server restart.
    """
    import os
    env_pw = (os.environ.get(f"APP_PASSWORD_{account['id']}")
              or os.environ.get("APP_PASSWORD"))
    if env_pw:
        return env_pw
    return account.get("app_password") or None


def run_once(days_back: int = 7) -> None:
    """Fetch + parse one cycle. Called by scheduler, CLI, or the manual
    Refresh button. Updates set_poller_progress() at each transition so the
    UI widget can show what's happening.
    """
    init_schema()
    cfg = _load_config()
    set_poller_status("running")
    set_poller_progress("starting", "Connecting to Ollama…")
    try:
        try:
            ollama_call = _ollama_call_factory(cfg)
        except RuntimeError as e:
            set_poller_progress("error", str(e).splitlines()[0])
            raise

        accounts = _list_email_accounts()
        for i, account in enumerate(accounts, 1):
            pw = _app_password_for(account)
            if not pw:
                log.warning("No APP_PASSWORD env var set for %s, skipping", account["email"])
                set_poller_progress(
                    "fetching",
                    f"Skipped {account['email']} (no APP_PASSWORD env var) [{i}/{len(accounts)}]",
                )
                continue
            set_poller_progress(
                "fetching",
                f"Fetching {account['email']} [{i}/{len(accounts)}]",
            )
            imap_fetch_new(account, pw, days_back=days_back)

        # Count unparsed before we start parsing so the UI can show progress
        with connection() as db:
            unparsed = db.execute(
                "SELECT COUNT(*) FROM raw_emails WHERE parsed = 0 AND parse_attempts < ?",
                (PARSE_MAX_ATTEMPTS,),
            ).fetchone()[0]
        if unparsed:
            set_poller_progress("parsing", f"Parsing {unparsed} new emails…")
            parse_unparsed_queue(ollama_call)

        # Bill pass — runs after the transaction parser has marked rows
        # parsed=1. Counts only what's pending so we don't re-scan history.
        with connection() as db:
            bills_pending = db.execute(
                "SELECT COUNT(*) FROM raw_emails WHERE bills_parsed = 0 AND parsed = 1"
            ).fetchone()[0]
        if bills_pending:
            set_poller_progress("parsing", f"Bill scan: {bills_pending} emails…")
            asyncio.run(parse_bills_queue_async(ollama_call))

        set_poller_progress("idle", "Refresh complete")
    finally:
        set_poller_status("stopped")


def run_backfill(days_back: int = 90) -> None:
    """Async backfill — concurrent Ollama parsing for the last N days.

    Always uses a fresh time window (force_window=True), ignoring each
    account's last_polled_at. That way `--days 30` truly means "the last
    30 days", regardless of incremental-poll state.
    """
    init_schema()
    cfg = _load_config()
    ollama_call = _ollama_call_factory(cfg)
    set_poller_status("running")
    set_poller_progress("starting", f"Backfill {days_back} days starting…")
    try:
        accounts = _list_email_accounts()
        for i, account in enumerate(accounts, 1):
            pw = _app_password_for(account)
            if not pw:
                log.warning("No APP_PASSWORD env var set for %s, skipping", account["email"])
                set_poller_progress(
                    "fetching",
                    f"Skipped {account['email']} (no APP_PASSWORD env var) [{i}/{len(accounts)}]",
                )
                continue
            set_poller_progress(
                "fetching",
                f"Backfilling {account['email']} [{i}/{len(accounts)}, last {days_back} days]",
            )
            imap_fetch_new(account, pw, days_back=days_back, force_window=True)

        with connection() as db:
            unparsed = db.execute(
                "SELECT COUNT(*) FROM raw_emails WHERE parsed = 0 AND parse_attempts < ?",
                (PARSE_MAX_ATTEMPTS,),
            ).fetchone()[0]
        if unparsed:
            set_poller_progress("parsing", f"Parsing {unparsed} new emails (concurrent)…")
            asyncio.run(parse_unparsed_queue_async(ollama_call))

        with connection() as db:
            bills_pending = db.execute(
                "SELECT COUNT(*) FROM raw_emails WHERE bills_parsed = 0 AND parsed = 1"
            ).fetchone()[0]
        if bills_pending:
            set_poller_progress("parsing", f"Bill scan: {bills_pending} emails (concurrent)…")
            asyncio.run(parse_bills_queue_async(ollama_call))

        set_poller_progress("idle", f"Backfill complete ({days_back} days)")
    finally:
        set_poller_status("stopped")


def _bodystructure_likely_has_pdf(bs_response) -> bool:
    """Cheap PDF presence check from a BODYSTRUCTURE response.

    BODYSTRUCTURE is a tiny IMAP response describing MIME parts (kilobytes),
    while RFC822 is the full message including attachment bytes (megabytes).
    Pre-filtering with BODYSTRUCTURE skips the expensive download for emails
    that can't possibly carry a PDF.

    Parsing BODYSTRUCTURE properly is a pain (nested parens, quoted strings).
    Substring match for "PDF" is good enough — false positives just cause an
    extra full fetch (cost: same as before), false negatives are vanishingly
    rare since real PDF parts always carry an "application/pdf" MIME type or
    a ".pdf" filename.
    """
    if not bs_response or not bs_response[0]:
        return False
    raw = bs_response[0]
    if isinstance(raw, tuple):
        raw = b" ".join(p for p in raw if isinstance(p, (bytes, bytearray)))
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    return "PDF" in raw.upper()


def update_attachments_for_account(
    account: dict, password: str, days_back: int = 30,
    only_message_ids: list[str] | None = None,
) -> int:
    """Re-fetch IMAP messages; for any with PDF attachments whose raw_emails
    row has attachment_text=NULL, extract PDF text and UPDATE in place.
    Resets bills_parsed=0 so the bill scan re-checks them.

    Optimization: BODYSTRUCTURE-pre-check before downloading full RFC822 —
    most emails don't have PDFs, so we skip the heavy fetch on them.

    `only_message_ids` (optional): if provided, only process these specific
    Message-IDs. Used for targeted testing without re-fetching the whole
    30-day window.
    """
    since_dt = datetime.now(timezone.utc) - timedelta(days=days_back)
    since = since_dt.strftime("%d-%b-%Y")
    log.info("attachment update: %s, since %s", account["email"], since)

    imap = _imap_login_with_backoff(
        account["imap_host"], account["imap_port"], account["email"], password
    )
    updated = 0
    skipped_no_pdf = 0
    target_set = set(only_message_ids) if only_message_ids else None
    try:
        imap.select("INBOX", readonly=True)
        status, data = imap.search(None, f'SINCE {since}')
        if status != "OK" or not data or not data[0]:
            return 0
        ids = data[0].split()
        log.info("attachment update: %s, %d messages in window", account["email"], len(ids))
        with connection() as db:
            for msg_id in ids:
                # Step 1 (cheap): peek BODYSTRUCTURE. Skip if no PDF marker.
                bs_status, bs_data = imap.fetch(msg_id, "(BODYSTRUCTURE)")
                if bs_status != "OK":
                    continue
                if not _bodystructure_likely_has_pdf(bs_data):
                    skipped_no_pdf += 1
                    continue

                # Step 2: peek headers to get Message-ID without downloading body.
                hdr_status, hdr_data = imap.fetch(
                    msg_id, "(BODY.PEEK[HEADER.FIELDS (Message-ID)])"
                )
                if hdr_status != "OK" or not hdr_data or not hdr_data[0]:
                    continue
                hdr_raw = hdr_data[0][1] if isinstance(hdr_data[0], tuple) else hdr_data[0]
                if isinstance(hdr_raw, (bytes, bytearray)):
                    hdr_raw = hdr_raw.decode("utf-8", errors="ignore")
                # Header line shape: "Message-ID: <abc@def>"
                message_id = None
                for line in hdr_raw.splitlines():
                    if line.lower().startswith("message-id:"):
                        message_id = line.split(":", 1)[1].strip()
                        break
                if not message_id:
                    continue
                if target_set is not None and message_id not in target_set:
                    continue

                # Step 3: only touch rows that exist AND have NULL attachment_text.
                row = db.execute(
                    "SELECT id, attachment_text FROM raw_emails WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if not row or row["attachment_text"] is not None:
                    continue

                # Step 4: full fetch + extract.
                _, msg_data = imap.fetch(msg_id, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                raw_bytes = msg_data[0][1]
                msg = email.message_from_bytes(raw_bytes)
                attach = _extract_pdf_attachment_text(msg)
                if attach is None:
                    db.execute(
                        "UPDATE raw_emails SET attachment_text = '' WHERE id = ?",
                        (row["id"],),
                    )
                    continue
                db.execute(
                    "UPDATE raw_emails SET attachment_text = ?, bills_parsed = 0 "
                    "WHERE id = ?",
                    (attach, row["id"]),
                )
                updated += 1
                log.info(
                    "%s: extracted PDF from raw_emails id=%d (msg_id=%s)",
                    account["email"], row["id"], message_id[:60],
                )
    finally:
        try:
            imap.close()
            imap.logout()
        except Exception:
            pass
    log.info(
        "%s: %d updated, %d skipped (no PDF in BODYSTRUCTURE)",
        account["email"], updated, skipped_no_pdf,
    )
    return updated


def run_attachment_update(days_back: int = 30) -> None:
    """One-off retroactive: re-fetch each account's last N days from IMAP and
    populate attachment_text for any rows that were inserted before that
    column existed. Then run the bill scan to pick up newly-extractable PDF
    bills (HDFC card statements etc.)."""
    init_schema()
    cfg = _load_config()
    ollama_call = _ollama_call_factory(cfg)
    set_poller_status("running")
    set_poller_progress("fetching", f"Updating PDF attachments (last {days_back}d)")
    try:
        accounts = _list_email_accounts()
        total_updated = 0
        for i, account in enumerate(accounts, 1):
            pw = _app_password_for(account)
            if not pw:
                log.warning("No password for %s, skipping", account["email"])
                continue
            set_poller_progress(
                "fetching",
                f"Updating attachments for {account['email']} [{i}/{len(accounts)}]",
            )
            total_updated += update_attachments_for_account(
                account, pw, days_back=days_back
            )

        with connection() as db:
            bills_pending = db.execute(
                "SELECT COUNT(*) FROM raw_emails WHERE bills_parsed = 0 AND parsed = 1"
            ).fetchone()[0]
        if bills_pending:
            set_poller_progress(
                "parsing", f"Bill scan: {bills_pending} emails (concurrent)…"
            )
            asyncio.run(parse_bills_queue_async(ollama_call))
        set_poller_progress(
            "idle", f"Attachment update complete ({total_updated} rows touched)"
        )
    finally:
        set_poller_status("stopped")


def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Email -> SQLite poller")
    p.add_argument("--backfill", action="store_true",
                   help="Initial 90-day fetch with concurrent parsing.")
    p.add_argument("--days", type=int, default=None,
                   help="Look back N days (default: 7 for poll, 90 for backfill, 30 for attachment update).")
    p.add_argument("--update-attachments", action="store_true",
                   help="Retroactively populate attachment_text by re-fetching last N days "
                        "from IMAP. Used after adding PDF attachment support.")
    args = p.parse_args(argv)

    try:
        if args.update_attachments:
            run_attachment_update(days_back=args.days or 30)
        elif args.backfill:
            run_backfill(days_back=args.days or 90)
        else:
            run_once(days_back=args.days or 7)
    except RuntimeError as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

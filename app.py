"""FastHTML dashboard.

Routes:
  GET  /                            home — account-sectioned transactions list
  GET  /account/{id}/expand         HTMX partial — full transaction list for one account
  GET  /account/{id}/collapse       HTMX partial — 5 most recent for one account
  POST /transactions/{id}/tag       HTMX partial — inline savings/expense pill swap
  GET  /settings                    email accounts + bank-account ownership
  POST /settings/account            create email_account
  POST /settings/test               test IMAP connection
  POST /settings/owner              assign owner to a bank_account
  GET  /api/poller-status           json status

UI design (revision 20260426): account-first layout. One section per bank_account
that has at least one transaction matching the owner filter. Sections sorted by
most-recent-activity DESC. 5 most recent rows shown by default; "Show all" expands
in place via HTMX.
"""
from __future__ import annotations

import imaplib
import logging
import threading
import time
import tomllib
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path

from fasthtml.common import (
    H1, H2, H3, A, Body, Button, Div, FastHTML, Form, Header,
    Html, Input, Label, Li, Link, Main, Meta, Option, P, Pre, Script,
    Select, Span, Textarea, Title, Ul, serve,
)

from db import (
    connection, get_poller_progress, get_poller_status, init_schema,
    set_poller_progress, set_poller_status,
)

log = logging.getLogger("app")

init_schema()


def _clear_stale_poller_flag() -> None:
    """Reset poller=running on startup. The poller runs in-process with the
    FastHTML server, so any 'running' flag from a prior process is by
    definition stale — that process is gone. Without this, a SIGKILL or hard
    restart leaves the widget spinning forever (the `finally` that sets
    status='stopped' doesn't run on SIGKILL).
    """
    status = get_poller_status()
    if status and status[0] == "running":
        log.warning("clearing stale poller=running flag (last updated %s)", status[1])
        set_poller_status("stopped")
        progress = get_poller_progress()
        if progress and progress[0] not in ("idle", "error"):
            set_poller_progress("idle", "Recovered from stale running flag")


_clear_stale_poller_flag()

DEFAULT_IMAP = {
    "gmail.com":   ("imap.gmail.com",         993, "smtp.gmail.com",        587),
    "outlook.com": ("outlook.office365.com", 993, "smtp.office365.com",    587),
    "hotmail.com": ("outlook.office365.com", 993, "smtp.office365.com",    587),
    "aol.com":     ("imap.aol.com",          993, "smtp.aol.com",          587),
}

def _load_owner_names() -> tuple[str, ...]:
    """Owner labels for the bank-account dropdown. Override per install in
    config.toml under [owners] names = ["Alice", "Bob", "MyCompany"].
    Falls back to neutral placeholders so the committed code carries no PII."""
    cfg_path = Path("config.toml")
    if cfg_path.exists():
        try:
            with open(cfg_path, "rb") as f:
                names = tomllib.load(f).get("owners", {}).get("names")
            if names and all(isinstance(n, str) and n for n in names):
                return tuple(names)
        except Exception:
            pass
    return ("Owner1", "Owner2", "Owner3")


ALLOWED_OWNERS = ("All",) + _load_owner_names()


# --- helpers -----------------------------------------------------------------


def _normalize_owner(raw: str | None) -> str:
    """Case-insensitive match against ALLOWED_OWNERS, fall back to 'All'."""
    if not raw:
        return "All"
    s = raw.strip().lower()
    for valid in ALLOWED_OWNERS:
        if s == valid.lower():
            return valid
    return "All"


def _current_month_prefix() -> str:
    """YYYY-MM string for the current local-time month."""
    return date.today().strftime("%Y-%m")


def _month_label() -> str:
    """Human label for the current month, e.g. 'April 2026'."""
    return date.today().strftime("%B %Y")


def _fmt_inr(amount: float) -> str:
    """Signed amount in Western grouping. Indian lakhs grouping is reserved
    for a future polish — Western grouping is unambiguous for now."""
    if amount == 0:
        return "₹0.00"
    sign = "+" if amount > 0 else "-"
    return f"{sign}₹{abs(amount):,.2f}"


def _fmt_date_short(iso: str | None) -> str:
    """'2026-04-25' -> 'Apr 25'. Returns the input unchanged on parse failure."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%b %d")
    except Exception:
        return iso


def _fmt_relative(iso_ts: str | None) -> str:
    if not iso_ts:
        return "never"
    try:
        ts = datetime.fromisoformat(iso_ts.replace(" ", "T"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return iso_ts
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60: return "just now"
    if secs < 3600: return f"{secs // 60} min ago"
    if secs < 86400: return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


# --- data access -------------------------------------------------------------


def _fetch_sections(owner: str) -> list[dict]:
    """Single SQL query, group + slice in Python.

    Returns a list of section dicts sorted by most-recent-activity DESC.
    Each section: account_name, account_last4, max_tx_date, month_total,
    total_count, rows[:5].
    """
    # Use bank_account JOIN for display name/last4 (b.* not t.*) so that
    # after a per-transaction move, the row appears under the TARGET account's
    # name, not the row's stale denormalized field.
    # Also surface the row's ORIGINAL extraction (t.account_last4) so the
    # template can decide whether to show the Move control: only orphans
    # (where the LLM didn't extract a last4) get the dropdown.
    sql = (
        "SELECT t.id, t.merchant, t.amount, t.currency, t.tx_date, "
        "       t.category, t.manual_tag, t.bank_account_id, "
        "       b.account_name AS account_name, "
        "       b.account_last4 AS account_last4, "
        "       b.owner, "
        "       t.account_last4 AS row_account_last4, "
        "       r.from_addr AS source_from_addr "
        "FROM transactions t "
        "JOIN bank_accounts b ON b.id = t.bank_account_id "
        "JOIN raw_emails r ON r.id = t.raw_email_id "
    )
    params: list = []
    if owner != "All":
        sql += "WHERE b.owner = ? "
        params.append(owner)
    sql += "ORDER BY t.bank_account_id, t.tx_date DESC, t.id DESC"

    with connection() as db:
        rows = [dict(r) for r in db.execute(sql, params).fetchall()]

    if not rows:
        return []

    month_pfx = _current_month_prefix()
    sections: list[dict] = []
    for bid, group_iter in groupby(rows, key=lambda r: r["bank_account_id"]):
        txs = list(group_iter)  # rows DESC within group; [0] is newest
        sections.append({
            "bank_account_id": bid,
            "account_name": txs[0]["account_name"],   # from b., not t.
            "account_last4": txs[0]["account_last4"], # from b., not t.
            "owner": txs[0]["owner"],                  # from b.owner via JOIN
            "max_tx_date": txs[0]["tx_date"],
            "month_total": sum(t["amount"] for t in txs if (t["tx_date"] or "")[:7] == month_pfx),
            "total_count": len(txs),
            "rows": txs[:5],
            "all_rows": txs,  # used by /expand
        })

    sections.sort(key=lambda s: s["max_tx_date"] or "", reverse=True)
    return sections


def _fetch_one_section(bank_account_id: int, owner: str) -> dict | None:
    """Single section by id, used by expand/collapse routes."""
    sections = _fetch_sections(owner)
    for s in sections:
        if s["bank_account_id"] == bank_account_id:
            return s
    return None


def _fetch_day_transactions(day: date) -> list[dict]:
    """All transactions on a given day, joined with bank_account so we can
    show name+last4+owner inline in the day card. Sort: most-recent email
    first within the day, falling back to id (newest insert) as tiebreaker."""
    sql = (
        "SELECT t.id, t.merchant, t.amount, t.currency, t.tx_date, "
        "       t.manual_tag, t.bank_account_id, "
        "       b.account_name, b.account_last4, b.owner, "
        "       r.received_at, r.id AS raw_email_id, "
        "       r.from_addr AS source_from_addr "
        "FROM transactions t "
        "JOIN bank_accounts b ON b.id = t.bank_account_id "
        "JOIN raw_emails r ON r.id = t.raw_email_id "
        "WHERE t.tx_date = ? "
        "ORDER BY datetime(r.received_at) DESC, t.id DESC"
    )
    with connection() as db:
        return [dict(r) for r in db.execute(sql, (day.isoformat(),)).fetchall()]


def _fetch_bills(status: str = "unpaid") -> list[dict]:
    """Bills filtered by status, sorted by due_date ASC (soonest first).
    JOINs raw_emails so the renderer can fall back to the email subject when
    the LLM didn't extract a description."""
    with connection() as db:
        return [dict(r) for r in db.execute(
            "SELECT b.id, b.biller, b.description, b.amount, b.currency, b.due_date, "
            "       b.status, b.paid_at, b.created_at, b.raw_email_id, "
            "       r.subject AS email_subject "
            "FROM bills b JOIN raw_emails r ON r.id = b.raw_email_id "
            "WHERE b.status = ? "
            "ORDER BY date(b.due_date) ASC, b.id ASC",
            (status,),
        ).fetchall()]


# --- SMS import (Option C: paste-and-go) ----------------------------------
#
# Bank SMS messages on iPhone don't sync to email or any third-party API.
# Workaround: user copies SMS text from iPhone (Universal Clipboard sends it
# to Mac), pastes into the Add-SMS form. We synthesize an email-shaped row
# so the existing transaction parser can handle it unchanged.
#
# Marker: from_addr = SMS_FROM_ADDR. The renderer uses this to show a delete
# button on SMS-derived transaction rows.
SMS_FROM_ADDR = "alerts@sms-import.finmail"
SMS_ACCOUNT_LABEL = "iPhone SMS"
SMS_ACCOUNT_EMAIL = "sms-import@finmail.local"


def _get_or_create_sms_account_id() -> int:
    """Return the synthetic email_account row used for SMS imports.
    Created on first use; idempotent."""
    with connection() as db:
        row = db.execute(
            "SELECT id FROM email_accounts WHERE email = ?", (SMS_ACCOUNT_EMAIL,)
        ).fetchone()
        if row:
            return row["id"]
        cur = db.execute(
            "INSERT INTO email_accounts (label, email, imap_host, imap_port, status) "
            "VALUES (?, ?, '', 0, 'sms-only')",
            (SMS_ACCOUNT_LABEL, SMS_ACCOUNT_EMAIL),
        )
        return cur.lastrowid


def _email_account_health() -> list[dict]:
    """Returns one dict per account with the columns the Settings page needs.
    `app_password` is selected as a boolean-ish flag (1/0) rather than the
    raw value so we never pass plaintext credentials into the rendered HTML."""
    with connection() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, label, email, imap_host, imap_port, status, last_polled_at, "
            "(app_password IS NOT NULL AND app_password != '') AS has_password "
            "FROM email_accounts"
        ).fetchall()]


def _email_count() -> int:
    with connection() as db:
        return db.execute("SELECT COUNT(*) FROM raw_emails").fetchone()[0]


def _bank_accounts_all() -> list[dict]:
    with connection() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, account_name, account_last4, owner FROM bank_accounts ORDER BY account_name"
        ).fetchall()]


def _fetch_account_aliases(bank_account_id: int) -> list[dict]:
    """All aliases pointing to one canonical bank_account, sorted by display."""
    with connection() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, alias_name, alias_last4, created_at "
            "FROM bank_account_aliases WHERE canonical_bank_account_id = ? "
            "ORDER BY alias_name, alias_last4",
            (bank_account_id,),
        ).fetchall()]


# Display order for owner groups on the home page. Real owners first in the
# order they were declared, then "Unowned" at the bottom.
OWNER_GROUP_ORDER = [o for o in ALLOWED_OWNERS if o != "All"] + ["Unowned"]


def _group_sections_by_owner(sections: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group sections by owner, return ordered (owner_name, [sections]) pairs.

    Empty groups are skipped. Order matches OWNER_GROUP_ORDER (configured
    owners from config.toml [owners].names, then "Unowned"). Within each
    group, sections keep their incoming sort order (most-recent-activity
    DESC from _fetch_sections).
    """
    buckets: dict[str, list[dict]] = {}
    for s in sections:
        key = s.get("owner") or "Unowned"
        buckets.setdefault(key, []).append(s)
    return [(name, buckets[name]) for name in OWNER_GROUP_ORDER if buckets.get(name)]


# --- background poller (scheduler + manual button) --------------------------

# Single lock guards the poller so the scheduled tick and the manual button
# can't run concurrently. Try-acquire so a second click silently skips while
# the first run is still in flight.
_POLLER_LOCK = threading.Lock()


def _read_poll_interval_minutes() -> int:
    """Minutes between scheduled polls. Default 60. Override in config.toml."""
    cfg_path = Path("config.toml")
    if not cfg_path.exists():
        return 60
    try:
        with open(cfg_path, "rb") as f:
            return int(tomllib.load(f).get("poll", {}).get("interval_minutes", 60))
    except Exception:
        return 60


def _run_poll_locked():
    """Wraps poller.run_once() under _POLLER_LOCK. If the lock is busy,
    silently skips (the in-flight run will complete on its own).
    """
    if not _POLLER_LOCK.acquire(blocking=False):
        log.info("poll skipped: another run already in flight")
        return
    try:
        # Lazy import — poller pulls in ollama, html2text, etc. that we don't
        # want to load at app-start if we don't have to.
        from poller import run_once
        try:
            run_once()
        except Exception as e:
            log.exception("scheduled poll failed: %s", e)
            from db import set_poller_progress
            set_poller_progress("error", str(e).splitlines()[0])
    finally:
        _POLLER_LOCK.release()


def _scheduler_loop(interval_seconds: int):
    """Daemon background thread: run forever, polling every N seconds."""
    log.info("poller scheduler started (interval=%ds)", interval_seconds)
    while True:
        time.sleep(interval_seconds)  # sleep first so we don't hammer at startup
        try:
            _run_poll_locked()
        except Exception:
            log.exception("scheduler iteration error")


# Spawn the daemon scheduler on module load. Daemon=True so it dies cleanly
# when the server stops (Ctrl+C). uvicorn --reload restarts the worker, which
# re-imports this module and starts a fresh thread — no risk of duplication.
_SCHEDULER_INTERVAL_S = _read_poll_interval_minutes() * 60
_scheduler_thread = threading.Thread(
    target=_scheduler_loop, args=(_SCHEDULER_INTERVAL_S,), daemon=True
)
_scheduler_thread.start()


# --- rendering ---------------------------------------------------------------


def _render_account_display_name(section: dict) -> str:
    name = section["account_name"]
    last4 = section["account_last4"]
    return f"{name} ···{last4}" if last4 else name


def _render_tag_pill(tx_id: int, manual_tag: str | None):
    """Tag pill — either the active tag or the trio of options. Same shape
    used by the per-account view AND the day card."""
    if manual_tag == "savings":
        return Button("savings", cls="tx-tag savings-tag",
                      hx_post=f"/transactions/{tx_id}/tag?value=",
                      hx_swap="outerHTML", hx_target="closest .tx-tag")
    if manual_tag == "expense":
        return Button("expense", cls="tx-tag expense-tag",
                      hx_post=f"/transactions/{tx_id}/tag?value=",
                      hx_swap="outerHTML", hx_target="closest .tx-tag")
    if manual_tag == "transfer":
        return Button("transfer", cls="tx-tag transfer-tag",
                      hx_post=f"/transactions/{tx_id}/tag?value=",
                      hx_swap="outerHTML", hx_target="closest .tx-tag")
    return Span(
        Button("savings", cls="tx-tag",
               hx_post=f"/transactions/{tx_id}/tag?value=savings",
               hx_swap="outerHTML", hx_target="closest span"),
        Button("expense", cls="tx-tag",
               hx_post=f"/transactions/{tx_id}/tag?value=expense",
               hx_swap="outerHTML", hx_target="closest span"),
        Button("transfer", cls="tx-tag",
               hx_post=f"/transactions/{tx_id}/tag?value=transfer",
               hx_swap="outerHTML", hx_target="closest span"),
    )


def _render_tx_row(row: dict):
    """One transaction row. HTMX tag toggle preserved + Move-to control
    when the transaction has no source account_last4 (orphan).
    """
    amount = row["amount"]
    sign_cls = "savings" if amount > 0 else "expense"
    amount_span = Span(
        Span(_fmt_inr(amount), cls="amount-real"),
        Span("••••", cls="amount-mask"),
        cls=f"tx-amount {sign_cls}",
    )
    tag = _render_tag_pill(row["id"], row["manual_tag"])

    # --- move control: only on rows where the LLM didn't extract a last4 ---
    # Rule: a row is "orphan" when its ORIGINAL extraction had no
    # account_last4. The move route preserves t.account_last4, so an
    # orphan stays orphan even after being moved. This gives persistent
    # undo/redo on the rows that needed manual assignment.
    # Merchant text doubles as the source-email toggle: clicking it opens an
    # inline panel below the row; clicking again closes it. Toggle logic is
    # client-side (toggleEmail in _page) so we don't pay a round-trip to close.
    children = [
        Span(_fmt_date_short(row["tx_date"]), cls="tx-date"),
        Button(
            row["merchant"] or "(unknown)",
            type="button",
            cls="tx-merchant",
            onclick=f"toggleEmail('tx-email-{row['id']}', '/transaction/{row['id']}/email')",
            title="Show source email",
        ),
        amount_span,
        tag,
    ]

    # SMS-derived rows get an inline delete button so the user can drop
    # accidental adds or duplicates without touching SQL. Email-derived rows
    # are deliberately not deletable from the UI — those reflect a real bank
    # email and the right fix for a misparse is to repair the parser, not
    # hide the symptom.
    if row.get("source_from_addr") == SMS_FROM_ADDR:
        children.append(Button(
            "✕",
            type="button",
            cls="tx-delete",
            title="Delete this SMS-imported transaction",
            aria_label="Delete transaction",
            hx_post=f"/transactions/{row['id']}/delete",
            hx_target=f"#acct-{row['bank_account_id']}",
            hx_swap="outerHTML",
            hx_confirm="Delete this transaction? This also removes the source SMS.",
        ))

    is_orphan = row.get("row_account_last4") is None
    if is_orphan:
        # Move dropdown: every OTHER bank_account (including currently-empty
        # ones, so the user can move things back to a previous source).
        other_accounts = [
            b for b in _bank_accounts_all()
            if b["id"] != row["bank_account_id"]
        ]
        if other_accounts:
            move_options = [Option("Move to…", value="", selected=True)]
            for b in other_accounts:
                last4 = f" ···{b['account_last4']}" if b["account_last4"] else ""
                move_options.append(Option(f"{b['account_name']}{last4}", value=str(b["id"])))
            children.append(Form(
                Select(*move_options, name="target", onchange="this.form.submit()"),
                Input(type="hidden", name="tx_id", value=str(row["id"])),
                method="post",
                action=f"/transactions/{row['id']}/move",
                cls="tx-move-form",
            ))

    return Div(
        Div(*children, cls="transaction-row"),
        Div(id=f"tx-email-{row['id']}", cls="tx-email-slot"),
        cls="tx-wrapper",
    )


def _render_section(section: dict, *, expanded: bool, owner: str):
    """Render one .account-section.

    The expand/collapse partials return the same shape so HTMX outerHTML
    swaps work seamlessly.
    """
    rows = section["all_rows"] if expanded else section["rows"]
    bid = section["bank_account_id"]
    total = section["month_total"]

    if total < 0:
        total_cls = "account-total expense"
    elif total > 0:
        total_cls = "account-total savings"
    else:
        total_cls = "account-total zero"

    owner_value = section.get("owner")
    if owner_value:
        owner_text = f"Owned by {owner_value}"
        owner_cls = "account-owner"
    else:
        owner_text = "Unowned"
        owner_cls = "account-owner unassigned"

    total_span = Span(
        f"{_month_label()}: ",
        Span(_fmt_inr(total), cls="amount-real"),
        Span("••••", cls="amount-mask"),
        cls=total_cls,
    )

    children = [
        Div(
            Div(
                H3(_render_account_display_name(section)),
                Button(
                    cls="toggle-amounts",
                    type="button",
                    aria_label="Toggle amount visibility",
                    title="Show / hide amounts",
                    onclick="this.closest('.account-section').classList.toggle('amounts-hidden')",
                ),
                cls="account-title-row",
            ),
            Div(
                Span(owner_text, cls=owner_cls),
                total_span,
                cls="account-meta",
            ),
            cls="account-header",
        ),
        Div(*(_render_tx_row(r) for r in rows), cls="account-rows"),
    ]

    # "Show all" / "Show less" button — hidden when total_count <= 5
    if section["total_count"] > 5:
        if expanded:
            children.append(Button(
                "Show less",
                cls="show-all-btn",
                hx_get=f"/account/{bid}/collapse?owner={owner}",
                hx_target=f"#acct-{bid}",
                hx_swap="outerHTML",
            ))
        else:
            children.append(Button(
                f"Show all {section['total_count']}",
                cls="show-all-btn",
                hx_get=f"/account/{bid}/expand?owner={owner}",
                hx_target=f"#acct-{bid}",
                hx_swap="outerHTML",
            ))

    # `amounts-hidden` is the default — the toggle button flips it client-side.
    # We deliberately DO NOT persist user preference: every page render and
    # every HTMX swap (expand/collapse) starts hidden so a casual glance never
    # exposes amounts.
    return Div(*children, cls="account-section amounts-hidden", id=f"acct-{bid}")


def _render_day_tx_row(row: dict):
    """One row inside the day card. Same email-toggle pattern as the per-account
    rows but uses a `day-tx-email-{id}` slot id so the two views don't collide
    in the DOM. Adds account name+owner inline since the card spans accounts.
    """
    amount = row["amount"]
    sign_cls = "savings" if amount > 0 else "expense"
    amount_span = Span(
        Span(_fmt_inr(amount), cls="amount-real"),
        Span("••••", cls="amount-mask"),
        cls=f"tx-amount {sign_cls}",
    )

    name = row["account_name"]
    last4 = row["account_last4"]
    acct_label = f"{name} ···{last4}" if last4 else name
    owner = row["owner"] or "Unowned"
    owner_cls = "day-tx-owner unowned" if owner == "Unowned" else "day-tx-owner"

    slot_id = f"day-tx-email-{row['id']}"
    day_children = [
        Button(
            row["merchant"] or "(unknown)",
            type="button",
            cls="tx-merchant",
            onclick=f"toggleEmail('{slot_id}', '/transaction/{row['id']}/email')",
            title="Show source email",
        ),
        amount_span,
        Span(acct_label, cls="day-tx-account"),
        Span(owner, cls=owner_cls),
        _render_tag_pill(row["id"], row["manual_tag"]),
    ]
    if row.get("source_from_addr") == SMS_FROM_ADDR:
        day_children.append(Button(
            "✕",
            type="button",
            cls="tx-delete",
            title="Delete this SMS-imported transaction",
            aria_label="Delete transaction",
            hx_post=f"/transactions/{row['id']}/delete",
            hx_target="#day-card",
            hx_swap="outerHTML",
            hx_confirm="Delete this transaction? This also removes the source SMS.",
        ))
    return Div(
        Div(*day_children, cls="day-tx-row"),
        Div(id=slot_id, cls="tx-email-slot"),
        cls="day-tx-wrapper",
    )


def _day_label(day: date) -> str:
    today = date.today()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return day.strftime("%b %d, %Y")


def _render_day_card(day: date):
    """Top-of-page summary: every transaction on `day` across all accounts.
    Prev/next buttons HTMX-swap the card in place. Future days are blocked
    (no transactions can exist there) so the next-button disables at today.
    """
    rows = _fetch_day_transactions(day)
    today = date.today()
    prev_d = day - timedelta(days=1)
    next_d = day + timedelta(days=1)
    next_disabled = day >= today

    label = _day_label(day)
    sub = day.strftime("%a, %b %d %Y")
    n = len(rows)

    next_btn_kw = dict(
        cls="day-nav-btn",
        title="Next day",
        type="button",
        hx_get=f"/day?d={next_d.isoformat()}",
        hx_target="#day-card",
        hx_swap="outerHTML",
    )
    if next_disabled:
        next_btn_kw["disabled"] = True

    body_children = (
        [_render_day_tx_row(r) for r in rows]
        if rows
        else [P(f"No transactions on {sub}", cls="empty-state")]
    )

    children = [
        Div(
            Div(
                Button("←", cls="day-nav-btn", title="Previous day", type="button",
                       hx_get=f"/day?d={prev_d.isoformat()}",
                       hx_target="#day-card", hx_swap="outerHTML"),
                Div(
                    H2(label, cls="day-label"),
                    Span(sub, cls="day-sub"),
                    cls="day-title",
                ),
                Button("→", **next_btn_kw),
                cls="day-nav",
            ),
            Button(
                cls="toggle-amounts",
                type="button",
                aria_label="Toggle amount visibility",
                title="Show / hide amounts",
                onclick="this.closest('.day-card').classList.toggle('amounts-hidden')",
            ),
            cls="day-header",
        ),
        Div(*body_children, cls="day-rows"),
    ]
    if rows:
        count_label = "1 transaction" if n == 1 else f"{n} transactions"
        children.append(Div(Span(count_label, cls="day-count"), cls="day-footer"))

    # Default `amounts-hidden` mirrors the per-account card behavior: every
    # render and every HTMX prev/next swap starts with amounts masked.
    return Div(*children, id="day-card", cls="day-card amounts-hidden")


def _bill_urgency_class(due: date, today: date) -> str:
    """Color signal:
       red  = due in next 2 days OR overdue
       amber = due in next 7 days
       neutral = anything further out
    """
    delta = (due - today).days
    if delta <= 2:
        return "due-red"
    if delta <= 7:
        return "due-amber"
    return "due-neutral"


def _bill_due_label(due: date, today: date) -> str:
    delta = (due - today).days
    if delta < 0:
        return f"Overdue by {-delta}d"
    if delta == 0:
        return "Due today"
    if delta == 1:
        return "Due tomorrow"
    return due.strftime("%b %d, %Y")


def _render_bill_row(bill: dict, today: date, paid: bool):
    """One row in the bills card. Paid rows render a 'Mark unpaid' undo;
    unpaid rows render 'Mark paid'. The merchant name uses the same email
    toggle pattern as transactions so you can always inspect the source.
    """
    due = date.fromisoformat(bill["due_date"])
    urgency = _bill_urgency_class(due, today) if not paid else "due-paid"
    label = _bill_due_label(due, today) if not paid else "Paid"

    bid = bill["id"]
    slot_id = f"bill-email-{bid}"

    # Action button: paid → unpaid (undo); unpaid → paid (mark)
    if paid:
        action = Button(
            "Mark unpaid",
            type="button",
            cls="bill-action undo",
            hx_post=f"/bills/{bid}/unpaid",
            hx_target=f"#bills-card",
            hx_swap="outerHTML",
        )
    else:
        action = Button(
            "Mark paid",
            type="button",
            cls="bill-action",
            hx_post=f"/bills/{bid}/paid",
            hx_target=f"#bills-card",
            hx_swap="outerHTML",
        )

    # Description fallback chain: LLM-extracted description → email subject
    # → biller name alone. The subject is usually the most descriptive thing
    # (e.g. "Bill for your Airtel Xstream Fiber - April'26").
    desc = (bill.get("description") or bill.get("email_subject") or "").strip()
    label_text = f"{bill['biller']} · {desc}" if desc else bill["biller"]

    # Bills don't have a transaction row, so /transaction/{id}/email returns
    # nothing. Route through /email/{raw_email_id} which fetches by raw_email
    # directly. (Transaction rows still use /transaction/.../email.)
    email_url = f"/email/{bill['raw_email_id']}"

    return Div(
        Div(
            Span(label, cls=f"bill-due {urgency}"),
            Button(
                label_text,
                type="button",
                cls="tx-merchant bill-biller",
                onclick=f"toggleEmail('{slot_id}', '{email_url}')",
                title="Show source email",
            ),
            Span(
                # Bills are always money owed — no +/− sign; the bill-due
                # color signal carries urgency, the amount carries magnitude.
                Span(f"₹{bill['amount']:,.2f}", cls="amount-real"),
                Span("••••", cls="amount-mask"),
                cls="tx-amount expense bill-amount",
            ),
            action,
            cls="bill-row",
        ),
        Div(id=slot_id, cls="tx-email-slot"),
        cls="bill-wrapper",
    )


def _render_bills_card(show_paid: bool = False):
    """Top-of-page bills card. Same shape as the day card so the visual
    language stays consistent. Defaults to amounts-hidden + paid section
    collapsed."""
    today = date.today()
    unpaid = _fetch_bills("unpaid")
    paid = _fetch_bills("paid") if show_paid else []
    n_unpaid = len(unpaid)
    n_paid_total = 0
    with connection() as db:
        n_paid_total = db.execute(
            "SELECT COUNT(*) FROM bills WHERE status = 'paid'"
        ).fetchone()[0]

    # Header
    header = Div(
        H2("Bills due", cls="bills-title"),
        Span(
            f"{n_unpaid} unpaid" if n_unpaid != 1 else "1 unpaid",
            cls="bills-count",
        ),
        Button(
            cls="toggle-amounts",
            type="button",
            aria_label="Toggle amount visibility",
            title="Show / hide amounts",
            onclick="this.closest('.bills-card').classList.toggle('amounts-hidden')",
        ),
        cls="bills-header",
    )

    # Unpaid body
    if unpaid:
        unpaid_rows = Div(
            *(_render_bill_row(b, today, paid=False) for b in unpaid),
            cls="bill-rows",
        )
    else:
        unpaid_rows = Div(
            P("No unpaid bills 🎉", cls="empty-state"),
            cls="bill-rows",
        )

    children = [header, unpaid_rows]

    # Paid section: collapsed link by default; clicking expands inline
    if n_paid_total > 0:
        if show_paid:
            paid_rows = Div(
                *(_render_bill_row(b, today, paid=True) for b in paid),
                cls="bill-rows paid",
            ) if paid else Div(P("No paid bills.", cls="empty-state"), cls="bill-rows paid")
            toggle = Button(
                f"▾ Hide paid ({n_paid_total})",
                type="button",
                cls="bills-paid-toggle",
                hx_get="/bills/hide-paid",
                hx_target="#bills-card",
                hx_swap="outerHTML",
            )
            children += [Div(toggle, cls="bills-paid-header"), paid_rows]
        else:
            toggle = Button(
                f"▸ Show paid ({n_paid_total})",
                type="button",
                cls="bills-paid-toggle",
                hx_get="/bills/show-paid",
                hx_target="#bills-card",
                hx_swap="outerHTML",
            )
            children.append(Div(toggle, cls="bills-paid-header"))

    return Div(*children, id="bills-card", cls="bills-card amounts-hidden")


def _render_onboarding(emails: list[dict]):
    """First-run checklist. Same intent as before, kept simple."""
    has_accounts = bool(emails)
    poller = get_poller_status()
    poller_running = poller and poller[0] == "running"
    has_emails = _email_count() > 0
    return Div(
        H2("Welcome — let's get you set up"),
        # Use Ul + manual numbering since we removed the auto-numbering CSS
        Div(
            P("1. ", A("Add an email account in Settings →", href="/settings"),
              cls=("step done" if has_accounts else "step")),
            P("2. Run the poller (python poller.py --backfill)",
              cls=("step done" if poller_running else "step")),
            P("3. Wait for emails to be parsed…",
              cls=("step done" if has_emails else "step")),
        ),
        cls="onboarding",
    )


def _render_owner_group(owner_name: str, sections: list[dict], owner_filter: str):
    """One owner section: header (name + count) + grid of that owner's cards."""
    n = len(sections)
    count_label = f"{n} account" if n == 1 else f"{n} accounts"
    section_cls = "owner-section unowned" if owner_name == "Unowned" else "owner-section"
    return Div(
        Div(
            H2(owner_name, cls="owner-name"),
            Span(count_label, cls="owner-count"),
            cls="owner-header",
        ),
        Div(
            *(_render_section(s, expanded=False, owner=owner_filter) for s in sections),
            cls="account-grid",
        ),
        cls=section_cls,
    )


def _render_empty_owner_filter(owner: str):
    return Div(
        P(f"No transactions for owner '{owner}'.", cls="empty-state"),
        P(A("View all owners →", href="/?owner=All"), cls="empty-state"),
        cls="home-empty",
    )


def _render_refresh_widget():
    """Single component used in the header. Three visual states:

    - in-flight (poller_status='running'): spinner + detail text + auto-poll
      every 2s via HTMX hx-trigger
    - idle (poller_status='stopped' or never run): "Refresh" button
    - error: red text + Refresh button to retry

    The widget is the same DOM id (refresh-widget) so HTMX swaps cleanly.
    """
    progress = get_poller_progress()
    status = get_poller_status()
    status_value = status[0] if status else None
    is_running = status_value == "running"

    common_attrs = {"id": "refresh-widget", "cls": "refresh-widget"}

    if is_running:
        phase, detail, _ts = progress or ("starting", "Starting…", "")
        return Div(
            Span(cls="spinner"),
            Span(detail or f"{phase}…", cls="refresh-detail"),
            hx_get="/api/poller-widget",
            hx_trigger="load delay:2s",
            hx_target="#refresh-widget",
            hx_swap="outerHTML",
            **common_attrs,
        )

    # Not running: figure out the secondary line ("just refreshed", "error", etc.)
    if progress:
        phase, detail, ts = progress
        if phase == "error":
            sub = Span(f"Error: {detail}", cls="refresh-sub error")
        elif phase == "idle":
            sub = Span(f"Refreshed {_fmt_relative(ts)}", cls="refresh-sub muted")
        else:
            sub = Span(_fmt_relative(ts), cls="refresh-sub muted")
    else:
        sub = Span("Never refreshed", cls="refresh-sub muted")

    return Div(
        sub,
        Button(
            "Refresh",
            cls="btn secondary",
            hx_post="/poll/run",
            hx_target="#refresh-widget",
            hx_swap="outerHTML",
        ),
        **common_attrs,
    )


# --- page shell --------------------------------------------------------------


_TOGGLE_EMAIL_JS = """
window.toggleEmail = function(slotId, url) {
  var slot = document.getElementById(slotId);
  if (!slot) return;
  if (slot.children.length > 0) {
    slot.innerHTML = '';
    return;
  }
  htmx.ajax('GET', url, { target: '#' + slotId, swap: 'innerHTML' });
};
"""


def _page(title: str, *body_children):
    return Html(
        Title(title),
        Meta(charset="UTF-8"),
        Meta(name="viewport", content="width=device-width, initial-scale=1.0"),
        Link(rel="stylesheet", href="/static/style.css"),
        Script(src="https://unpkg.com/htmx.org@1.9.10"),
        Script(_TOGGLE_EMAIL_JS),
        Body(*body_children),
    )


app = FastHTML(hdrs=())
from starlette.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")


# --- routes: home + expand/collapse -----------------------------------------


@app.get("/")
def index(owner: str | None = None):
    norm_owner = _normalize_owner(owner)

    # First-run state: no accounts configured at all, or zero parsed emails
    emails = _email_account_health()
    if not emails or _email_count() == 0:
        return _page(
            "Finance — Setup",
            Header(H1("FinMail"), A("Settings", href="/settings", cls="header-link"), cls="header"),
            Main(_render_onboarding(emails), cls="home"),
        )

    sections = _fetch_sections(norm_owner)
    day_card = _render_day_card(date.today())
    bills_card = _render_bills_card()
    if not sections:
        return _page(
            "Finance",
            Header(H1("FinMail"), Div(_render_refresh_widget(), A("Add SMS", href="/add-sms", cls="header-link"), A("Settings", href="/settings", cls="header-link"), cls="header-right"), cls="header"),
            Main(day_card, bills_card, _render_empty_owner_filter(norm_owner), cls="home"),
        )

    grouped = _group_sections_by_owner(sections)
    return _page(
        "Finance",
        Header(H1("FinMail"), Div(_render_refresh_widget(), A("Add SMS", href="/add-sms", cls="header-link"), A("Settings", href="/settings", cls="header-link"), cls="header-right"), cls="header"),
        Main(
            day_card,
            bills_card,
            *(_render_owner_group(owner_name, owner_sections, norm_owner)
              for owner_name, owner_sections in grouped),
            cls="home",
        ),
    )


@app.post("/bills/{bill_id}/paid")
def bill_mark_paid(bill_id: int):
    with connection() as db:
        db.execute(
            "UPDATE bills SET status = 'paid', paid_at = CURRENT_TIMESTAMP WHERE id = ?",
            (bill_id,),
        )
    return _render_bills_card()


@app.post("/bills/{bill_id}/unpaid")
def bill_mark_unpaid(bill_id: int):
    with connection() as db:
        db.execute(
            "UPDATE bills SET status = 'unpaid', paid_at = NULL WHERE id = ?",
            (bill_id,),
        )
    # If the only paid bill was just unmarked, the card no longer needs the
    # paid section expanded — but keeping show_paid=True is harmless.
    return _render_bills_card(show_paid=True)


@app.get("/bills/show-paid")
def bills_show_paid():
    return _render_bills_card(show_paid=True)


@app.get("/bills/hide-paid")
def bills_hide_paid():
    return _render_bills_card(show_paid=False)


@app.get("/day")
def day_card_partial(d: str | None = None):
    """HTMX partial: re-renders the day card for a given date. Defaults to
    today if no date given or if the date is invalid/in the future."""
    today_d = date.today()
    try:
        day = datetime.fromisoformat(d).date() if d else today_d
    except (ValueError, TypeError):
        day = today_d
    if day > today_d:
        day = today_d
    return _render_day_card(day)


@app.get("/account/{bank_account_id}/expand")
def account_expand(bank_account_id: int, owner: str | None = None):
    norm_owner = _normalize_owner(owner)
    section = _fetch_one_section(bank_account_id, norm_owner)
    if not section:
        # Stale id or owner-filter mismatch — return an empty section that disappears.
        return Div(id=f"acct-{bank_account_id}", cls="account-section")
    return _render_section(section, expanded=True, owner=norm_owner)


@app.get("/account/{bank_account_id}/collapse")
def account_collapse(bank_account_id: int, owner: str | None = None):
    norm_owner = _normalize_owner(owner)
    section = _fetch_one_section(bank_account_id, norm_owner)
    if not section:
        return Div(id=f"acct-{bank_account_id}", cls="account-section")
    return _render_section(section, expanded=False, owner=norm_owner)


# --- routes: tag toggle (unchanged behavior) ---------------------------------


VALID_TAGS = ("savings", "expense", "transfer")


@app.post("/transactions/{tx_id}/tag")
def tag_transaction(tx_id: int, value: str = ""):
    new_tag = value if value in VALID_TAGS else None
    with connection() as db:
        db.execute("UPDATE transactions SET manual_tag = ? WHERE id = ?", (new_tag, tx_id))
    if new_tag == "savings":
        return Button("savings", cls="tx-tag savings-tag",
                      hx_post=f"/transactions/{tx_id}/tag?value=",
                      hx_swap="outerHTML", hx_target="this")
    if new_tag == "expense":
        return Button("expense", cls="tx-tag expense-tag",
                      hx_post=f"/transactions/{tx_id}/tag?value=",
                      hx_swap="outerHTML", hx_target="this")
    if new_tag == "transfer":
        return Button("transfer", cls="tx-tag transfer-tag",
                      hx_post=f"/transactions/{tx_id}/tag?value=",
                      hx_swap="outerHTML", hx_target="this")
    # Untagged: 3 buttons in a span
    return Span(
        Button("savings", cls="tx-tag",
               hx_post=f"/transactions/{tx_id}/tag?value=savings",
               hx_swap="outerHTML", hx_target="closest span"),
        Button("expense", cls="tx-tag",
               hx_post=f"/transactions/{tx_id}/tag?value=expense",
               hx_swap="outerHTML", hx_target="closest span"),
        Button("transfer", cls="tx-tag",
               hx_post=f"/transactions/{tx_id}/tag?value=transfer",
               hx_swap="outerHTML", hx_target="closest span"),
    )


@app.post("/transactions/{tx_id}/move")
def move_transaction(tx_id: int, target: str = ""):
    """Move one transaction to a different bank_account.

    Only the bank_account_id link changes. The transaction's own denormalized
    `account_name`/`account_last4` are preserved as audit trail of the
    original LLM extraction. Display uses `b.account_name`/`b.account_last4`
    from the bank_account JOIN, so the section header tracks the new target.

    Empty source bank_accounts are NOT auto-deleted — they stay in the DB
    (invisible on home, visible in the Move dropdown) so the user can move
    transactions back to them later.
    """
    from fasthtml.common import RedirectResponse
    if not target:
        return RedirectResponse("/", status_code=303)
    try:
        target_id = int(target)
    except ValueError:
        return RedirectResponse("/", status_code=303)

    with connection() as db:
        tx = db.execute(
            "SELECT bank_account_id FROM transactions WHERE id = ?", (tx_id,)
        ).fetchone()
        if not tx:
            return RedirectResponse("/", status_code=303)
        if tx["bank_account_id"] == target_id:
            return RedirectResponse("/", status_code=303)
        # Confirm target exists
        target_exists = db.execute(
            "SELECT 1 FROM bank_accounts WHERE id = ?", (target_id,)
        ).fetchone()
        if not target_exists:
            return RedirectResponse("/", status_code=303)
        # Single-column update: only the bank_account link changes.
        db.execute(
            "UPDATE transactions SET bank_account_id = ? WHERE id = ?",
            (target_id, tx_id),
        )

    return RedirectResponse("/", status_code=303)


# --- routes: settings --------------------------------------------------------


def _render_bank_account_row(b: dict, all_accounts: list[dict]):
    """One row in Settings → Bank accounts.
    Includes: header (name + owner select + save), aliases list (with delete
    X per alias), and a 'Merge into ▾' dropdown to create new aliases."""
    last4 = f" ···{b['account_last4']}" if b["account_last4"] else ""
    aliases = _fetch_account_aliases(b["id"])
    other = [oa for oa in all_accounts if oa["id"] != b["id"]]

    # Owner-assignment form (existing UI, unchanged behavior).
    owner_form = Form(
        Span(f"{b['account_name']}{last4}", cls="bank-row-name"),
        Select(
            Option("(unassigned)", value="", selected=(not b.get("owner"))),
            *(Option(name, value=name, selected=(b.get("owner") == name))
              for name in ALLOWED_OWNERS if name != "All"),
            name="owner",
        ),
        Input(type="hidden", name="bank_account_id", value=str(b["id"])),
        Button("Save", cls="btn secondary"),
        method="post", action="/settings/owner",
        cls="bank-owner-form",
    )

    # Alias chips (only when this canonical account has any). Each chip is
    # a tiny inline form so a single click on the X submits to the delete
    # route — no JS, no HTMX, just a form-per-chip.
    alias_block = ""
    if aliases:
        chips = []
        for a in aliases:
            label = a["alias_name"] + (f" ···{a['alias_last4']}" if a["alias_last4"] else "")
            chips.append(Form(
                Span(label, cls="alias-chip-label"),
                Button(
                    "✕",
                    type="submit",
                    cls="alias-chip-delete",
                    title="Delete alias — future transactions for this identity "
                          "will go to a fresh bank account again",
                ),
                method="post",
                action=f"/settings/aliases/{a['id']}/delete",
                cls="alias-chip",
            ))
        alias_block = Div(
            Span("Aliases:", cls="aliases-label"),
            *chips,
            cls="aliases-row",
        )

    # Merge-into form. Hidden when there are no other accounts (nothing to
    # merge into). Confirmation prompt covers the irreversible-data warning.
    merge_block = ""
    if other:
        options = [Option("Merge into…", value="", selected=True, disabled=True)]
        for oa in other:
            oa_last4 = f" ···{oa['account_last4']}" if oa["account_last4"] else ""
            options.append(Option(f"{oa['account_name']}{oa_last4}", value=str(oa["id"])))
        merge_block = Form(
            Select(*options, name="target", required=True),
            Button(
                "Merge",
                cls="btn secondary merge-btn",
                onclick=(
                    "return confirm('Merge this account into the selected target? "
                    "All transactions move to the target. The target account name "
                    "+ owner are kept; this account row is deleted but recorded as "
                    "an alias so future imports auto-route to the target.');"
                ),
            ),
            method="post",
            action=f"/settings/account/{b['id']}/merge",
            cls="merge-form",
        )

    return Li(
        owner_form,
        alias_block,
        merge_block,
        cls="bank-row",
    )


def _render_email_account_row(e: dict):
    """One row in the Settings → Email accounts list.
    Header line: label, email, status, last-polled.
    Status pill: 🔒 password set | ⚠ no password.
    Inline form: app-password input + Test + Save, results land in
    #pwd-result-{id} via HTMX outerHTML swap.
    """
    has_pw = bool(e.get("has_password"))
    pw_indicator = (
        Span("🔒 password set", cls="pw-indicator ok")
        if has_pw
        else Span("⚠ no password — Refresh will skip this account", cls="pw-indicator warn")
    )
    aid = e["id"]
    return Li(
        Div(
            f"{e['label']} ({e['email']}) — {e['status']} · last polled {_fmt_relative(e['last_polled_at'])}",
            cls="email-account-head",
        ),
        pw_indicator,
        Form(
            Input(
                type="password",
                name="app_password",
                placeholder="App password",
                required=True,
                cls="pw-input",
            ),
            Button(
                "Test",
                type="button",
                cls="btn secondary",
                hx_post=f"/settings/account/{aid}/test",
                hx_target=f"#pwd-result-{aid}",
                hx_swap="outerHTML",
                hx_include="closest form",
            ),
            Button(
                "Save",
                type="button",
                cls="btn",
                hx_post=f"/settings/account/{aid}/password",
                hx_target=f"#pwd-result-{aid}",
                hx_swap="outerHTML",
                hx_include="closest form",
            ),
            Span(id=f"pwd-result-{aid}", cls="pw-result"),
            cls="pw-form",
        ),
        cls="email-account-row",
    )


@app.get("/settings")
def settings_page():
    emails = _email_account_health()
    bank_accounts = _bank_accounts_all()

    email_rows = [_render_email_account_row(e) for e in emails]

    bank_rows = [_render_bank_account_row(b, bank_accounts) for b in bank_accounts]

    poller = get_poller_status()
    if poller:
        poller_text = f"Poller: {poller[0]} · {_fmt_relative(poller[1])}"
    else:
        poller_text = "Poller: not yet run"

    return _page(
        "Settings",
        Header(H1("Settings"), Div(_render_refresh_widget(), A("← back", href="/", cls="header-link"), cls="header-right"), cls="header"),
        Main(
            Div(
                # Owner filter doc — links generated from ALLOWED_OWNERS so
                # they reflect the user's configured owners, not hardcoded names.
                H2("View by owner"),
                P("Filter the home page by owner via URL: ",
                  A("/?owner=All", href="/?owner=All"), " (default)",
                  *(child for n in ALLOWED_OWNERS if n != "All"
                    for child in (", ", A(f"/?owner={n}", href=f"/?owner={n}"))),
                  ". Bookmark the URL you use most."),

                # Poller status
                H2("Poller", style="margin-top:24px"),
                P(poller_text),

                # Email accounts
                H2("Email accounts", style="margin-top:24px"),
                Ul(*email_rows) if email_rows else P("No accounts yet.", cls="empty-state"),
                H3("Add new", style="margin-top:24px"),
                Form(
                    Div(Label("Label"),
                        Input(name="label", placeholder="Personal Gmail", required=True),
                        cls="form-row"),
                    Div(Label("Email"),
                        Input(name="email", type="email", placeholder="you@gmail.com", required=True),
                        cls="form-row"),
                    Div(Label("IMAP host"),
                        Input(name="imap_host",
                              placeholder="auto-fills for @gmail/@outlook/@hotmail/@aol; for Workspace use imap.gmail.com"),
                        cls="form-row"),
                    Div(Label("IMAP port"),
                        Input(name="imap_port", type="number", value="993"),
                        cls="form-row"),
                    Div(Label("App password"),
                        Input(name="app_password", type="password",
                              placeholder="(used for connection test only)", required=True),
                        cls="form-row"),
                    Span(
                        Button("Test connection", cls="btn secondary",
                               hx_post="/settings/test", hx_target="#test-result", hx_include="closest form"),
                        Button("Save account", cls="btn", type="submit"),
                        Span(id="test-result"),
                    ),
                    method="post", action="/settings/account",
                ),

                # Bank accounts
                H2("Bank accounts (assign owner)", style="margin-top:32px"),
                Ul(*bank_rows) if bank_rows else P("None yet.", cls="empty-state"),

                cls="settings",
            ),
            cls="home",
        ),
    )


@app.post("/settings/account")
def settings_create_account(label: str, email: str, imap_host: str = "",
                            imap_port: int = 993, app_password: str = ""):
    domain = email.split("@")[-1].lower() if "@" in email else ""
    defaults = DEFAULT_IMAP.get(domain, ("", 993, "", 587))
    imap_host = imap_host or defaults[0]
    smtp_host, smtp_port = defaults[2], defaults[3]
    # Strip whitespace — Gmail App Passwords are shown as "abcd efgh ijkl mnop"
    # and IMAP login works either way, but storing the spaces is just noise.
    pw_to_store = app_password.replace(" ", "").strip() or None
    with connection() as db:
        db.execute(
            "INSERT INTO email_accounts (label, email, imap_host, imap_port, "
            "smtp_host, smtp_port, app_password) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (label, email, imap_host, imap_port, smtp_host, smtp_port, pw_to_store),
        )
    from fasthtml.common import RedirectResponse
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/test")
def settings_test_connection(email: str, imap_host: str, imap_port: int = 993,
                              app_password: str = "", **_extras):
    if not (email and imap_host and app_password):
        return Span("Missing fields", cls="test-result err", id="test-result")
    try:
        c = imaplib.IMAP4_SSL(imap_host, imap_port)
        c.login(email, app_password)
        c.logout()
        return Span("✓ Connected", cls="test-result ok", id="test-result")
    except Exception as e:
        return Span(f"✗ {type(e).__name__}: {e}", cls="test-result err", id="test-result")


def _normalize_app_password(raw: str) -> str:
    """Strip whitespace; Gmail App Passwords are displayed as 'abcd efgh ijkl mnop'
    and IMAP login accepts either form, but storing without spaces keeps the
    DB tidy and makes future comparisons predictable."""
    return raw.replace(" ", "").strip()


@app.post("/settings/account/{account_id}/test")
def settings_test_existing_account(account_id: int, app_password: str = ""):
    """Test connection for an already-saved account using a password from the
    inline form. Looks up email/imap_host/imap_port from the DB so the form
    only needs the password field."""
    rid = f"pwd-result-{account_id}"
    pw = _normalize_app_password(app_password)
    if not pw:
        return Span("Enter a password first", cls="pw-result err", id=rid)
    with connection() as db:
        row = db.execute(
            "SELECT email, imap_host, imap_port FROM email_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
    if not row:
        return Span("Account not found", cls="pw-result err", id=rid)
    try:
        c = imaplib.IMAP4_SSL(row["imap_host"], row["imap_port"])
        c.login(row["email"], pw)
        c.logout()
        return Span("✓ Connected", cls="pw-result ok", id=rid)
    except Exception as e:
        return Span(f"✗ {type(e).__name__}: {e}", cls="pw-result err", id=rid)


@app.post("/settings/account/{account_id}/password")
def settings_update_password(account_id: int, app_password: str = ""):
    """Save the password to email_accounts.app_password. No connection test
    is forced — that's the Test button's job. We do reject empty values
    because saving '' would silently break Refresh and confuse the user."""
    rid = f"pwd-result-{account_id}"
    pw = _normalize_app_password(app_password)
    if not pw:
        return Span("Enter a password first", cls="pw-result err", id=rid)
    with connection() as db:
        cur = db.execute(
            "UPDATE email_accounts SET app_password = ? WHERE id = ?",
            (pw, account_id),
        )
        if cur.rowcount == 0:
            return Span("Account not found", cls="pw-result err", id=rid)
    return Span("✓ Saved — refresh page to update lock indicator",
                cls="pw-result ok", id=rid)


@app.post("/settings/owner")
def settings_set_owner(bank_account_id: int, owner: str = ""):
    # Real owners = ALLOWED_OWNERS minus the "All" pseudo-filter
    real_owners = {o for o in ALLOWED_OWNERS if o != "All"}
    new = owner if owner in real_owners else None
    with connection() as db:
        db.execute("UPDATE bank_accounts SET owner = ? WHERE id = ?", (new, bank_account_id))
    from fasthtml.common import RedirectResponse
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/account/{account_id}/merge")
def settings_merge_account(account_id: int, target: str = ""):
    """Merge `account_id` (source) into `target` (canonical):
      1. Move all transactions from source → target
      2. Re-target any aliases that pointed to source so they now point to
         target (handles chained merges A → B then B → C)
      3. Insert a new alias mapping (source.name, source.last4) → target so
         future imports for the source's identity auto-route to target
      4. Delete the source bank_accounts row

    Refuses no-op (target == source) and missing target.
    """
    from fasthtml.common import RedirectResponse
    if not target:
        return RedirectResponse("/settings", status_code=303)
    try:
        target_id = int(target)
    except ValueError:
        return RedirectResponse("/settings", status_code=303)
    if target_id == account_id:
        return RedirectResponse("/settings", status_code=303)

    with connection() as db:
        src = db.execute(
            "SELECT id, account_name, account_last4 FROM bank_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        tgt = db.execute(
            "SELECT id FROM bank_accounts WHERE id = ?", (target_id,),
        ).fetchone()
        if not src or not tgt:
            return RedirectResponse("/settings", status_code=303)

        db.execute(
            "UPDATE transactions SET bank_account_id = ? WHERE bank_account_id = ?",
            (target_id, account_id),
        )
        db.execute(
            "UPDATE bank_account_aliases SET canonical_bank_account_id = ? "
            "WHERE canonical_bank_account_id = ?",
            (target_id, account_id),
        )
        # INSERT OR IGNORE — if for some reason an alias with this (name, last4)
        # already exists pointing somewhere, don't clobber it. The user can
        # delete that alias and re-merge if they want a different mapping.
        db.execute(
            "INSERT OR IGNORE INTO bank_account_aliases "
            "(alias_name, alias_last4, canonical_bank_account_id) "
            "VALUES (?, ?, ?)",
            (src["account_name"], src["account_last4"], target_id),
        )
        db.execute("DELETE FROM bank_accounts WHERE id = ?", (account_id,))

    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/aliases/{alias_id}/delete")
def settings_delete_alias(alias_id: int):
    """Drop an alias mapping. Future imports matching the alias's (name, last4)
    will fall through `upsert_bank_account` step 2 and create a fresh
    bank_accounts row again. Existing transactions on the canonical row are
    NOT moved — deletion only stops future auto-routing."""
    from fasthtml.common import RedirectResponse
    with connection() as db:
        db.execute("DELETE FROM bank_account_aliases WHERE id = ?", (alias_id,))
    return RedirectResponse("/settings", status_code=303)


@app.get("/api/poller-status")
def api_poller_status():
    s = get_poller_status()
    if not s:
        return {"status": "never_run"}
    return {"status": s[0], "last_update": s[1], "relative": _fmt_relative(s[1])}


# --- refresh: button + progress widget --------------------------------------


@app.post("/poll/run")
def poll_run():
    """Manual Refresh button. Spawns a background thread (returns instantly)
    so the UI stays responsive. The thread acquires _POLLER_LOCK; if the lock
    is busy (scheduler or another click), it silently skips. The user sees
    the same widget either way — running shows live progress, idle shows the
    button.
    """
    threading.Thread(target=_run_poll_locked, daemon=True).start()
    # Briefly wait so the first widget poll already shows "Starting…" rather
    # than the previous idle state. 200ms is enough for the worker to call
    # set_poller_status('running') and set_poller_progress('starting', ...).
    time.sleep(0.2)
    return _render_refresh_widget()


@app.get("/api/poller-widget")
def api_poller_widget():
    """HTMX polling target. Returns the current widget HTML."""
    return _render_refresh_widget()


# --- routes: SMS import (paste textarea) ------------------------------------

import hashlib as _hashlib


def _render_add_sms_page(message: str = "", message_kind: str = ""):
    """Form + result message. Result kinds: 'ok', 'err', '' (initial)."""
    msg_block = (
        Div(message, cls=f"sms-result {message_kind}") if message else ""
    )
    return _page(
        "Add SMS",
        Header(
            H1("FinMail"),
            Div(_render_refresh_widget(),
                A("Add SMS", href="/add-sms", cls="header-link"),
                A("Settings", href="/settings", cls="header-link"),
                A("← back", href="/", cls="header-link"),
                cls="header-right"),
            cls="header",
        ),
        Main(
            Div(
                H2("Add SMS-only transaction", cls="add-sms-title"),
                P(
                    "Some bank SMS messages don't sync to email. Paste a single "
                    "transaction SMS here and FinMail will parse it through the "
                    "same Ollama pipeline as your email alerts.",
                    cls="add-sms-help",
                ),
                P(
                    "Tip: on macOS, copy the SMS from your iPhone via Universal "
                    "Clipboard (must be on same Wi-Fi + iCloud account).",
                    cls="add-sms-help muted",
                ),
                Form(
                    Div(
                        Label("SMS text"),
                        Input(
                            type="hidden", name="form_marker", value="add-sms",
                        ),
                        # textarea isn't in the import list; use a Div fallback
                        # or rely on raw HTML. FastHTML supports Textarea via
                        # fasthtml.common — let's import below.
                        Textarea(
                            placeholder='ICICI Bank Acc XX440 debited Rs. 5,000.00 on 28-Apr-26 InfoACH*GENERIC 28...',
                            name="sms_text",
                            rows="6",
                            required=True,
                            cls="sms-textarea",
                        ),
                        cls="form-row",
                    ),
                    Button("Add transaction", cls="btn", type="submit"),
                    method="post", action="/add-sms",
                ),
                msg_block,
                cls="add-sms",
            ),
            cls="home",
        ),
    )


@app.get("/add-sms")
def add_sms_page():
    return _render_add_sms_page()


@app.post("/add-sms")
def add_sms_submit(sms_text: str = ""):
    """Parse pasted SMS through the existing transaction parser, persist if
    extracted. Returns the form page with a success/error banner.
    """
    sms_text = (sms_text or "").strip()
    if not sms_text:
        return _render_add_sms_page("Empty SMS — nothing to parse.", "err")

    # Lazy imports — parser/poller pull in ollama which we don't want at app
    # startup time on cold paths.
    from datetime import date as _date
    from poller import _load_config, _ollama_call_factory, _persist_transaction
    from parser import parse_email

    cfg = _load_config()
    try:
        ollama_call = _ollama_call_factory(cfg)
    except RuntimeError as e:
        return _render_add_sms_page(f"Ollama not reachable: {e}", "err")

    result = parse_email(
        from_addr=SMS_FROM_ADDR,
        subject="SMS",
        body_text=sms_text,
        ollama_call=ollama_call,
        received_at=_date.today(),
    )

    if result.skipped or not result.transaction:
        return _render_add_sms_page(
            f"Parser returned no transaction. Reason: {result.reason}", "err"
        )

    # Persist: synthesize a raw_emails row first (so the FK on transactions
    # is satisfied), then insert the transaction.
    sms_account_id = _get_or_create_sms_account_id()
    # message_id must be unique. Use a content hash + timestamp so identical
    # re-pastes get distinct ids (user can dedupe via the delete button).
    digest = _hashlib.sha256(sms_text.encode()).hexdigest()[:16]
    msg_id = f"<sms-{digest}-{int(datetime.now().timestamp())}@finmail.local>"

    with connection() as db:
        cur = db.execute(
            "INSERT INTO raw_emails "
            "(email_account_id, message_id, subject, from_addr, body, body_text, "
            " received_at, parsed, parse_attempts, bills_parsed) "
            "VALUES (?, ?, 'SMS', ?, ?, ?, ?, 1, 0, 1)",
            (sms_account_id, msg_id, SMS_FROM_ADDR, sms_text, sms_text,
             datetime.now(timezone.utc).isoformat()),
        )
        raw_email_id = cur.lastrowid
        _persist_transaction(db, raw_email_id, result.transaction)

    tx = result.transaction
    sign = "+" if tx.amount_type == "credit" else "-"
    return _render_add_sms_page(
        f"✓ Added: {tx.merchant} · {sign}₹{tx.amount:,.2f} · "
        f"{tx.account_name} ···{tx.account_last4 or '?'} · {tx.date}",
        "ok",
    )


@app.post("/transactions/{tx_id}/delete")
def delete_transaction(tx_id: int):
    """Delete an SMS-imported transaction + its synthetic raw_emails row.
    Refuses to touch email-derived rows — those should be fixed by repairing
    the parser, not hidden by deletion.
    """
    from fasthtml.common import RedirectResponse
    with connection() as db:
        row = db.execute(
            "SELECT t.bank_account_id, r.from_addr, r.id AS raw_email_id "
            "FROM transactions t JOIN raw_emails r ON r.id = t.raw_email_id "
            "WHERE t.id = ?",
            (tx_id,),
        ).fetchone()
        if not row:
            return RedirectResponse("/", status_code=303)
        if row["from_addr"] != SMS_FROM_ADDR:
            # Refuse: not SMS-derived. UI never offers the button for these,
            # so this only fires on a tampered/manual request.
            return RedirectResponse("/", status_code=303)
        db.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        db.execute("DELETE FROM raw_emails WHERE id = ?", (row["raw_email_id"],))

    return RedirectResponse("/", status_code=303)


# --- routes: source email viewer --------------------------------------------


def _fetch_email_for_tx(tx_id: int) -> dict | None:
    """Return the raw_email row that produced this transaction, or None."""
    with connection() as db:
        row = db.execute(
            "SELECT r.subject, r.from_addr, r.received_at, r.body_text, r.body "
            "FROM raw_emails r JOIN transactions t ON t.raw_email_id = r.id "
            "WHERE t.id = ?",
            (tx_id,),
        ).fetchone()
    return dict(row) if row else None


def _render_email_panel(email: dict):
    """Returns the panel content (no outer id) — gets swapped as innerHTML
    of the row's #tx-email-{id} slot. Renders body_text (html2text-stripped)
    so embedded HTML is already neutralized; FastHTML escapes string content
    automatically as a second layer of defense."""
    body = (email.get("body_text") or email.get("body") or "(empty body)").strip()
    return Div(
        Div(
            Div(
                Span("From: ", cls="email-label"),
                Span(email.get("from_addr") or "(unknown)", cls="email-value"),
                cls="email-meta-row",
            ),
            Div(
                Span("Subject: ", cls="email-label"),
                Span(email.get("subject") or "(no subject)", cls="email-value"),
                cls="email-meta-row",
            ),
            Div(
                Span("Received: ", cls="email-label"),
                Span(email.get("received_at") or "", cls="email-value"),
                cls="email-meta-row",
            ),
            cls="email-meta",
        ),
        Pre(body, cls="email-body"),
        cls="tx-email-panel",
    )


@app.get("/transaction/{tx_id}/email")
def transaction_email(tx_id: int):
    email = _fetch_email_for_tx(tx_id)
    if not email:
        return ""
    return _render_email_panel(email)


def _fetch_raw_email(raw_email_id: int) -> dict | None:
    """Direct raw_emails fetch — used by the bills card where there's no
    transaction to JOIN against."""
    with connection() as db:
        row = db.execute(
            "SELECT subject, from_addr, received_at, body_text, body "
            "FROM raw_emails WHERE id = ?",
            (raw_email_id,),
        ).fetchone()
    return dict(row) if row else None


@app.get("/email/{raw_email_id}")
def email_by_id(raw_email_id: int):
    email = _fetch_raw_email(raw_email_id)
    if not email:
        return ""
    return _render_email_panel(email)


if __name__ == "__main__":
    # Bind to 0.0.0.0 so other devices on the same LAN (e.g. your phone) can
    # reach the dashboard at http://<laptop-lan-ip>:8000. Anyone on the same
    # WiFi can hit this URL — fine on a home network, risky on coffee-shop
    # WiFi. To restrict to just the laptop, change back to "127.0.0.1".
    serve(host="0.0.0.0", port=8000)

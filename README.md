# Personal Finance Dashboard

A local-first dashboard that turns transaction-notification emails (Gmail / Outlook / AOL) into a categorised, searchable, multi-account ledger. No third-party aggregator. No bank API credentials. No data leaves your machine.

The pipeline:

```
IMAP inboxes ──► raw_emails (SQLite)
                       │
                       ▼
                 Local LLM (Ollama)  ──►  ParsedTransaction (Pydantic)
                       │                          │
                       │                          ▼
                       │                  verbatim-amount check
                       │                          │
                       └──────────────────────────▼
                                        transactions / bills (SQLite)
                                                  │
                                                  ▼
                                        FastHTML + HTMX dashboard
                                              (localhost:8000)
```

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [System requirements](#system-requirements)
- [Setup](#setup)
- [Configuration](#configuration)
- [Running the application](#running-the-application)
- [How it works](#how-it-works)
- [Project layout](#project-layout)
- [Tests and eval](#tests-and-eval)
- [Troubleshooting](#troubleshooting)
- [Privacy and security](#privacy-and-security)

---

## What it does

- Polls one or more email inboxes over IMAP every N minutes.
- For each new email, runs an Ollama-hosted LLM with a strict prompt to extract:
  account name, last-4 digits, merchant, amount, debit/credit, currency, date, category.
- Validates the LLM output against a Pydantic schema and a "the amount must
  appear verbatim in the body" check, which catches hallucinations.
- Stores results in SQLite (WAL mode), grouped by bank account, with owner
  attribution (you / spouse / company / whatever you configure).
- Renders an HTMX dashboard: per-account sections, day-grouped transactions,
  bill due-dates, "move this transaction to a different account" actions, and
  an inline view of the original email for any row.
- Optional bill-detector pass that flags upcoming due dates separately from
  transactions.

This is opinionated for **Indian bank email formats** out of the box (HDFC, ICICI, SBI Card, Axis, Kotak, Yes Bank). The prompt has examples for Indian-lakhs notation, UPI VPAs, IMPS / NEFT / RTGS confirmations, and the boilerplate fraud-warning text those banks include. It will work with US/EU bank emails too, but you may need to add prompt examples in `parser.py` for unfamiliar formats.

## Architecture

Five Python modules, one SQLite database, one optional Ollama process.

| Module | Responsibility |
|---|---|
| `db.py` | SQLite schema, connection helper (WAL mode), poller status table, bank-account upsert, alias dedup. |
| `poller.py` | IMAP login + retry/backoff, fetch new emails, dedup by `Message-Id`, drive the parse loop. CLI for one-shot or 90-day backfill. |
| `parser.py` | Allowlist filter, prompt construction, Ollama call, JSON parsing, Pydantic validation, verbatim-amount check. Two paths: transactions and bills. |
| `app.py` | FastHTML routes, HTMX rendering, the dashboard UI, `/settings` page, the manual Refresh button, the in-process scheduler. |
| `tests/` | Unit tests for the parser (stub LLM) and DB helpers, plus an LLM eval harness for accuracy regression testing. |

The scheduler runs as a daemon thread inside `app.py`. There is **no separate poller process** in normal operation — the dashboard handles its own polling. `python poller.py` is only needed for backfill or one-off CLI runs.

### Database

Six tables in `finance.db`:

- `email_accounts` — one row per inbox you connect (label, email, IMAP host, optional app_password, last_polled_at, status).
- `bank_accounts` — derived from parsed emails, deduped by `(account_name, account_last4)`. Each one is assignable to an "owner".
- `bank_account_aliases` — explicit user-driven equivalence (e.g. an SMS-extracted "ICICI Bank Account ···1234" maps to email-extracted "ICICI Bank Savings Account ···91234").
- `raw_emails` — verbatim email bodies + parse status (`parsed = 0|1|-1`, `parse_attempts`).
- `transactions` — signed amounts, FK to bank_account, FK to source raw_email, optional manual tag (savings / expense override).
- `bills` — separate from transactions: future obligations (postpaid, EMI, statement due dates).
- `system_status` — single-key heartbeat table for the poller widget.

Schema is created idempotently by `init_schema()` on every app start. There is no migration framework — the project is small enough that we extend the schema with `ALTER TABLE ... IF NOT EXISTS` patterns when needed.

## System requirements

| Thing | Why | Tested with |
|---|---|---|
| **Python 3.12+** | Uses `tomllib` (3.11+), modern type hints, `dataclasses` slots. | 3.12.1 |
| **Ollama** | Local LLM runtime. | 0.1.7+ |
| **Disk: ~10 GB free** | The recommended `qwen2.5:14b` model is ~9 GB. A 7B model is ~5 GB. | — |
| **RAM: 16 GB+** | qwen2.5:14b needs ~10 GB resident; 7B needs ~5 GB. On 8 GB you'll page heavily. | — |
| **macOS or Linux** | Anything Python + Ollama runs on. The `imaplib` socket-timeout fix relies on Python 3.9+ behaviour. Should work on Windows but untested. | macOS 14+ |
| **Apple Silicon recommended** | qwen2.5:14b takes ~30-40s per email on M-series; ~2-3x slower on x86 without a GPU. | M-series |
| **Email inboxes with IMAP enabled** | Gmail, Outlook/365, AOL all work. Yahoo / iCloud should work but untested. | Gmail, AOL, Outlook |
| **App passwords for those inboxes** | Bank emails are flagged by IMAP, so you can't reuse a normal password. See [Email setup](#email-setup) below. | — |

## Setup

```bash
# 1. Clone
git clone <this-repo-url> account-consolidation
cd account-consolidation

# 2. Create a virtualenv and install deps
python3.12 -m venv .venv
source .venv/bin/activate          # macOS/Linux
# .venv\Scripts\activate           # Windows
pip install -r requirements.txt

# 3. Install Ollama (https://ollama.com/download)
#    Then pull the recommended model:
ollama pull qwen2.5:14b
#    (qwen2.5:7b also works — faster but less reliable on edge cases)

# 4. Start Ollama in a separate terminal
ollama serve

# 5. Copy the config example and edit if needed
cp config.toml.example config.toml
#    Default model is "llama3.1" — change to "qwen2.5:14b" if you pulled that.

# 6. Run the dashboard
python app.py
#    Open http://localhost:8000
```

On first run, `init_schema()` creates `finance.db` automatically. The scheduler thread starts immediately and will fire its first poll cycle after `interval_minutes` (default 60) — or click the **Refresh** button in the UI to trigger one manually.

### Adding your first email account

The dashboard's `/settings` page lets you add inboxes:

1. Click **Settings** in the header (or open `http://localhost:8000/settings`).
2. Under **Add new**, fill in: a label (e.g. "Personal Gmail"), the email address, and an app password.
3. The IMAP host auto-fills for `@gmail.com` / `@outlook.com` / `@hotmail.com` / `@aol.com`. For Workspace Gmail, override with `imap.gmail.com`.
4. Click **Add** — the account is saved to `email_accounts` and the next poll will pick it up.

Or, if you prefer the DB:

```bash
sqlite3 finance.db "
INSERT INTO email_accounts (label, email, imap_host, imap_port, app_password)
VALUES ('Personal Gmail', 'you@gmail.com', 'imap.gmail.com', 993, 'xxxx xxxx xxxx xxxx');
"
```

### Email setup (app passwords)

Use an **app password**, not your normal account password. Banks send transaction alerts with strict authentication, and most providers require app passwords for IMAP access anyway.

| Provider | IMAP host | Port | App-password setup |
|---|---|---|---|
| Gmail | `imap.gmail.com` | 993 | Enable 2FA, then https://myaccount.google.com/apppasswords |
| Outlook / 365 | `outlook.office365.com` | 993 | Enable 2FA, then https://account.microsoft.com/security → App passwords |
| AOL | `imap.aol.com` | 993 | https://login.aol.com → Account info → Generate app password |
| Yahoo | `imap.mail.yahoo.com` | 993 | Account security → Generate app password |
| iCloud | `imap.mail.me.com` | 993 | https://appleid.apple.com → App-specific passwords |

**Storage:** the password lives in `email_accounts.app_password` (sqlite, on your disk). For local-only use this is fine. If you'd rather not have it in the DB, set the env var `APP_PASSWORD_<id>` (per account) or `APP_PASSWORD` (single-account fallback) before starting the server. The poller checks env first, then the DB column.

## Configuration

`config.toml` is the local config file. It is **gitignored** — never commit it. Use `config.toml.example` as a template.

```toml
[llm]
provider = "ollama"               # "ollama" | (future: "openai")
model = "qwen2.5:14b"             # any model you've pulled
host = "http://localhost:11434"   # Ollama default

[poll]
interval_minutes = 60             # how often the scheduler ticks

[owners]
# Labels for the bank-account assignment dropdown.
# These are personal — they don't get committed.
names = ["Owner1", "Owner2", "Owner3"]

[digest]
# Weekly summary email (planned, not yet implemented)
enabled = false
```

Email accounts are NOT in `config.toml`. They live in `email_accounts` and are managed via `/settings` or direct INSERT.

### Switching LLM model

```bash
ollama pull qwen2.5:7b            # faster, less accurate
ollama pull qwen2.5:14b           # slower (~3x), more reliable
ollama pull phi4:14b              # alternative family
```

Then update `[llm].model` in `config.toml` and restart the server. The eval harness (see [Tests and eval](#tests-and-eval)) lets you compare models on your own fixtures.

## Running the application

### Normal operation

```bash
python app.py
```

That's it. The scheduler thread wakes up every `interval_minutes` and runs the IMAP fetch + parse loop. The browser dashboard at `http://localhost:8000` shows live status (spinner while polling, "Refreshed N min ago" when idle, error message on failure). The **Refresh** button forces an immediate cycle.

The server binds to `0.0.0.0:8000` so other devices on your LAN (your phone, for instance) can reach the dashboard. To restrict to localhost only, edit the last line of `app.py`:

```python
serve(host="127.0.0.1", port=8000)
```

### Manual one-shot or backfill

```bash
python poller.py                  # one-shot: fetch since last_polled_at, parse, exit
python poller.py --backfill       # 90-day backfill, async parse with concurrency=3
python poller.py --backfill --days 30   # custom backfill window
```

These are useful for first-time setup (backfill 90 days of history before going live) and for testing parser changes against a known set of emails. They use the same logic as the in-app scheduler.

### Stopping

`Ctrl-C` in the terminal running `app.py`. The scheduler thread is a daemon, so it dies with the parent. The poller's `try/finally` block clears the `running` flag in `system_status`. If the process is force-killed (`kill -9`) the flag stays stale; the app's startup sweep in `_clear_stale_poller_flag()` cleans it up on next launch.

## How it works

### The parse contract

`parser.py` defines a `ParsedTransaction` Pydantic model with these fields:

- `account_name`, `account_last4`, `merchant`
- `amount` (float, must be positive), `amount_type` (`"debit"` | `"credit"`)
- `currency`, `date`, `category` (one of 10 enums)

The LLM is told to return JSON matching this exact shape, or the literal string `null` if the email is not a transaction (OTPs, marketing, statement-ready notices). The prompt includes 11 worked examples covering common Indian-bank shapes:

- HDFC RTGS initiation (8-digit lakhs amounts: `40,00,000.00` = ₹4M, not ₹400K)
- ICICI card swipe with merchant
- Airtel postpaid receipt
- HDFC e-mandate with DD/MM/YYYY date interpretation
- RTGS completion confirmation (treated as duplicate, returns `null`)
- IMPS debit with both "debited" and "credited to" in one sentence
- ACH transfers with no proper-noun merchant (derive from descriptor)
- HDFC UPI debit to a VPA
- SBI Card spend with the "Trxn. not done by you?" disclaimer

After the LLM returns JSON, `parse_email()`:

1. Strips Markdown code fences if the model added them.
2. Runs `json.loads()` — a parse error is a retryable failure.
3. Validates against `ParsedTransaction` — a validation error is a retryable failure.
4. Falls back to `received_at` (email arrival timestamp) if `date` is null.
5. Runs `_verbatim_check(amount, body_text)` — the amount must appear in the body in any common format (`1234.56`, `1,234.56`, `Rs. 1234.56`, `1,23,456.78` lakhs). This catches LLM hallucinations.

If any of those fail, `parse_attempts` increments. After 3 attempts, the row is marked `parsed = -1` (terminal) and skipped going forward.

### The candidates hint

Real bank emails contain phone numbers, reference numbers, balances, and other amount-shaped tokens. To stop the LLM from picking the wrong number as the transaction amount, `_candidates_for_hint()` extracts every amount-shaped token from the body, filters out anything ≥ 1e9 (phone numbers and reference IDs), and includes the remaining list in the prompt. The LLM is told its `amount` must be one of those values.

### The alias system

Some accounts get extracted twice — once from email (with a long descriptive name) and once from SMS (short name). The `bank_account_aliases` table lets you mark them as the same account. Future transactions matching the merged-away `(name, last4)` auto-route to the canonical row.

## Project layout

```
.
├── app.py                          FastHTML server, HTMX routes, scheduler
├── poller.py                       IMAP fetch + parse loop, CLI entry
├── parser.py                       LLM prompt, Pydantic, verbatim check
├── db.py                           Schema, connection, status helpers
├── config.toml                     Your local config (gitignored)
├── config.toml.example             Template — copy to config.toml
├── finance.db                      SQLite database (gitignored)
├── requirements.txt                Pinned deps
├── static/
│   ├── style.css                   Single stylesheet
│   └── fonts/                      (Optional: drop Inter-* woff2s here)
├── tests/
│   ├── conftest.py                 Pytest fixtures (tmp_db)
│   ├── test_parser.py              Parser unit tests (stub LLM)
│   ├── test_db.py                  DB helper tests
│   ├── fixtures/                   Synthetic test emails (HTML)
│   └── eval/
│       ├── README.md               Eval harness docs (data is local-only)
│       ├── run_eval.py             Real-Ollama accuracy regression harness
│       ├── fixtures/               (gitignored: real bank emails)
│       ├── ground_truth.jsonl      (gitignored: hand-labelled expected outputs)
│       └── results.jsonl           (gitignored: per-run scorecard log)
├── CLAUDE.md                       AI-coding-agent instructions (gstack)
├── TODOS.md                        Backlog (manual)
└── README.md                       This file
```

## Tests and eval

Two distinct things, both useful:

### Unit tests (fast, stubbed)

```bash
.venv/bin/python -m pytest tests/test_parser.py tests/test_db.py -v
```

23 tests. The parser tests stub the LLM with `_stub_ollama({...})` so they validate the parser's plumbing — JSON handling, Pydantic coercion, the verbatim check, the allowlist gate, the date-fallback logic — without touching Ollama. Run these on every code change. ~0.2 seconds.

### LLM eval (slow, real Ollama)

```bash
.venv/bin/python tests/eval/run_eval.py
.venv/bin/python tests/eval/run_eval.py --model qwen2.5:7b   # override
```

This is the **accuracy regression harness**. Reads real bank-email fixtures from `tests/eval/fixtures/`, runs them through the actual configured Ollama model, compares field-by-field against `ground_truth.jsonl`, prints a scorecard, appends a row to `results.jsonl`. Threshold: ≥80% on amount/merchant/category.

Fixtures and ground truth are **not committed** — they contain real banking data. After your first 90-day backfill, copy interesting `raw_emails` rows to `tests/eval/fixtures/`, hand-label them in `ground_truth.jsonl`, and you have a regression suite. See `tests/eval/README.md`.

## Troubleshooting

### The poller widget spins forever

Either the poller is stuck on a slow LLM call (qwen2.5:14b at ~30-40s/email × 6 emails = 3-4 minutes is normal), or a previous poll was force-killed and left a stale flag. The startup sweep in `app.py:_clear_stale_poller_flag()` clears stale flags on next launch — restart the server. If it still spins after restart, check `sqlite3 finance.db "SELECT * FROM system_status;"` and the timestamps.

### `Ollama refused connection`

```bash
curl http://localhost:11434/api/tags     # should return JSON
```

If not, run `ollama serve` in a separate terminal. On macOS, the menu-bar app can also handle this. Confirm `[llm].host` in `config.toml` matches.

### IMAP login fails

- Did you use an **app password**? Normal passwords don't work for IMAP on Gmail/Outlook/AOL.
- Is 2FA enabled on the account? App passwords usually require it.
- Check `email_accounts.status` in the DB. The poller marks accounts `error` after retry failures.
- Watch the server log: `tail -f` whatever your shell shows; the poller logs IMAP errors with the account email.

### IMAP fetch hangs

We hit this earlier in the project: a silently-dropped TCP connection makes `imaplib.IMAP4_SSL.fetch()` block forever because Python's default socket timeout is `None`. Fixed in `poller.py` by passing `timeout=IMAP_TIMEOUT_S` (60s) to `IMAP4_SSL` and wrapping the fetch loop in `try/except (OSError, imaplib.IMAP4.abort)`. If you see hangs >2 min, restart the server — the next poll resumes from the same `last_polled_at` watermark.

### LLM returns null on what's clearly a transaction

This is the LLM mis-classifying a real transaction as "system notification" — usually because of disclaimer boilerplate. Three things to check:

1. Is the from-address in `is_transaction_email()`'s allowlist (`alerts@…`, `noreply@…`, etc.)? If not, add it to `TRANSACTION_SENDER_PATTERNS` in `parser.py`.
2. Does the email have an unusual format the prompt examples don't cover? Add a new example to `PARSE_PROMPT`.
3. Is it qwen2.5:7b being flaky? Try `qwen2.5:14b`. The eval harness will tell you which is better on your fixtures.

### LLM returns the wrong amount

The verbatim check should catch this — you'd see `parsed=0, parse_attempts=N` with `reason="verbatim check failed"` in the logs. Most common cause: 8-digit Indian-lakhs notation (`40,00,000.00` parsed as 4 lakhs instead of 40). Example 9 in `PARSE_PROMPT` plus the `_candidates_for_hint()` filter address this — but if you see it on a new format, add an example.

### `finance.db is locked`

SQLite WAL mode allows concurrent reads while one writer is active, but if you hold a transaction open in the sqlite3 CLI while the app is writing, you'll see this. Quit the CLI and try again. The connection helper in `db.py` always uses `with connection() as db:` so app-level code releases locks promptly.

### Tests fail after a code change

The 23 unit tests are deterministic (stubbed LLM). If they fail after your change, the change broke a contract — read the assertion, fix the code. They take ~0.2s, run them often.

### Eval scorecard regresses

You changed `PARSE_PROMPT` and accuracy dropped. Compare the latest two rows in `tests/eval/results.jsonl` to see which fixture(s) regressed. Add a new prompt example targeting that case, re-run.

### Port 8000 already in use

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN | awk 'NR>1 {print $2}' | xargs kill
```

Or change the port in the `serve(host=..., port=...)` call at the bottom of `app.py`.

### Owner names show as `Owner1 / Owner2 / Owner3`

You don't have an `[owners]` section in your `config.toml`, so the fallback kicks in. Add:

```toml
[owners]
names = ["Alice", "Bob", "Mortgage"]
```

…and restart the server. (Existing rows in `bank_accounts` keep whatever owner string they were saved with — the dropdown is just for choosing what to assign next.)

## Privacy and security

This project is built around a basic premise: **your transaction history should never leave your machine**. Concretely:

- All inference is local (Ollama). No prompts go to OpenAI, Anthropic, or any other API by default. The optional `[llm].fallback_provider = "openai"` exists for power users; if you enable it, email bodies will be sent to OpenAI.
- `finance.db` (with raw email bodies, OTPs, balances, account numbers) is gitignored.
- `config.toml` (with email-account labels and owner names) is gitignored.
- `tests/eval/fixtures/`, `tests/eval/ground_truth.jsonl`, and `tests/eval/results.jsonl` are gitignored — they're meant to hold real bank emails for personal accuracy regression testing.
- `.sesskey` (FastHTML session-signing UUID) is gitignored and regenerated on each app start.
- The scheduler thread runs in-process; nothing daemonises or installs at the OS level.
- The dashboard binds to `0.0.0.0:8000` by default for LAN access. On a shared network, change to `127.0.0.1`.

If you fork this and intend to publish, run a PII sweep on the codebase first — particularly look for prompt examples and test fixtures that might contain real account data.

---

**Open issues / planned work:** see `TODOS.md`. Pull requests welcome.

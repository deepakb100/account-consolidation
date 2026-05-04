# Eval harness — local data only

The eval harness CODE (`run_eval.py`) is committed. The DATA is **not**:

- `fixtures/*.txt` — raw bank-email bodies. Real personal banking data.
- `ground_truth.jsonl` — labelled expected outputs referencing real account
  numbers, merchants, and transaction reference IDs.
- `results.jsonl` — per-fixture LLM accuracy traces.

All three paths are gitignored (`.gitignore` `tests/eval/fixtures/`,
`tests/eval/ground_truth.jsonl`, `tests/eval/results.jsonl`).

## Generating your own fixtures

1. Run the poller for at least a few days so `raw_emails` contains real
   transactions you've personally received.
2. Copy interesting bodies from `raw_emails` to `tests/eval/fixtures/<name>.txt`.
3. Hand-label expected output in `tests/eval/ground_truth.jsonl`:

   ```json
   {"fixture": "<name>.txt", "from_addr": "...", "subject": "...",
    "received_at": "YYYY-MM-DD",
    "expected": {"account_name": "...", "account_last4": "...",
                 "merchant": "...", "amount": 1234.56, "amount_type": "debit",
                 "currency": "INR", "date": "YYYY-MM-DD", "category": "..."}}
   ```

4. Run: `.venv/bin/python tests/eval/run_eval.py [--model qwen2.5:14b]`

The harness appends a summary row per run to `results.jsonl` for trend tracking.

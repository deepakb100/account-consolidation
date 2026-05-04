"""Email -> ParsedTransaction.

Pipeline (per email):
  is_transaction_email()        # cheap allowlist filter (D12)
        |
        v
  Ollama LLM call               # returns JSON
        |
        v
  ParsedTransaction (Pydantic)  # type coercion + category enum
        |
        v
  _verbatim_check(amount)       # confirms LLM didn't hallucinate
        |
        v
  ParsedTransaction or None
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date as _date
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError, field_validator

# Sender + subject allowlist (D12 — skip OTPs/marketing before LLM).
# Conservative: a handful of known patterns. New senders go through unfiltered
# initially; if LLM keeps returning null, the parsed=-1 path catches them.
TRANSACTION_SENDER_PATTERNS = [
    r"alerts?@",
    r"noreply@",
    r"no[-_]reply@",
    r"transactions?@",
    r"statements?@",
    r"@icicibank\.com$",
    r"@hdfcbank\.net$",
    r"@hdfcbank\.com$",
    r"@axisbank\.com$",
    r"@sbi\.co\.in$",
    r"@kotak\.com$",
    r"@yesbank\.in$",
]
TRANSACTION_SUBJECT_KEYWORDS = [
    "transaction", "debited", "credited", "spent", "purchase",
    "payment", "transfer", "withdrawn", "deposited", "alert",
    "txn", "salary credit", "upi",
]
NON_TRANSACTION_SUBJECT_KEYWORDS = [
    "otp", "one time password", "verification code",
    "statement is ready", "e-statement", "monthly statement",
    "offer", "reward", "cashback offer", "promotional",
    "newsletter", "credit card bill",  # bill ready != transaction
]

CATEGORY_VALUES = (
    "Food", "Shopping", "Utilities", "Transport", "Healthcare",
    "Entertainment", "Transfer", "Salary", "Savings", "Other",
)

PARSE_PROMPT = """Extract transaction details from this bank notification email.
Return JSON only, no explanation, no markdown fences.

If the email is not a transaction (OTP, marketing, statement-ready notice, EMI offer,
birthday wish, system notification), return the literal string: null

If it IS a transaction, return JSON with EXACTLY these fields. Every field below is
REQUIRED and must NOT be null (except account_last4 and date as noted):

{{
  "account_name": "bank account description (e.g. 'HDFC Savings Account', 'ICICI Credit Card', 'Airtel Wallet'). NEVER null for a transaction.",
  "account_last4": "last 4 DIGITS only of card/account, e.g. '1234'. Strip any 'X' or '*' prefix. null if the email genuinely has no account number.",
  "merchant": "the OTHER party in the transaction: payee name, retailer, biller, or sender. NOT the bank name. NOT the account holder's name. NEVER null.",
  "amount": 1234.56,
  "amount_type": "debit" or "credit",
  "currency": "INR" or "USD" etc,
  "date": "YYYY-MM-DD" or null if no transaction date appears in the email body,
  "category": one of: Food, Shopping, Utilities, Transport, Healthcare, Entertainment, Transfer, Salary, Savings, Other
}}

Rules:
- amount is always a positive number. Use amount_type for direction (debit = money out, credit = money in).
- The amount value MUST appear verbatim somewhere in the email body. Common formats:
  "1234.56", "1,234.56", "Rs. 1234.56", "Rs 1234", "₹1,234.56", "INR 1234.56",
  or Indian lakhs format like "1,23,456.78".
- account_name describes WHICH ACCOUNT the money moved through (the user's account).
  merchant describes WHO/WHAT was on the OTHER end of the transaction.
- If the email mentions only "your account" with no specific name, infer the bank from
  the sender domain (e.g. alerts@hdfcbank.com → "HDFC Bank Account").
- A bill payment (utility, telecom, recharge) is amount_type="debit" — money LEFT your
  account to pay the bill. The merchant is the biller (e.g. "Airtel", "Tata Power").
- IMPORTANT: payment receipts and confirmations FROM service providers ("Thank you for
  your payment of...", "We have received a payment of...", "Payment received") ARE
  transactions. The provider received YOUR money — that means YOU paid (debit) and the
  provider is the merchant. Do NOT return null for these. Even if no account number
  appears, set account_name to the provider name + "Account" (e.g. "Airtel Postpaid").
- AMOUNT SIZE DOES NOT MATTER. A ₹50 coffee charge is just as much a transaction as
  a ₹50,000 transfer. Do NOT return null because the amount seems small.
- The phrases "Your card has been used for a transaction", "has been debited",
  "has been credited", "has been deducted", "transferred to", "transaction of INR/Rs/USD"
  are ALWAYS real transactions — even when the email also mentions credit limits or
  available balances. The transaction amount is the one being CHARGED, not the limit.
- Multiple amounts in the body? The transaction amount is the one tied to "transaction of",
  "debited", "credited", or similar action verbs. The "Available Credit Limit" or
  "Available Balance" is informational context, NOT the transaction amount.
- DATE FORMAT: Indian banks use DD/MM/YYYY. A bare numeric date like "09/04/2026" means
  9 April 2026, NOT 4 September. "01/04/2026" means 1 April 2026, NOT 4 January.
  Always interpret slash- or dash-separated numeric dates as DD/MM/YYYY (or DD-MM-YYYY).
  Dates with month names ("09-Apr-2026", "Apr 9, 2026") follow the named month and are
  unambiguous. ISO dates ("2026-04-09") are unambiguous and stay as-is.
- MERCHANT IS NEVER NULL FOR A TRANSACTION. If the body has no clear merchant
  proper-noun (e.g. "Amazon", "Swiggy"), use the most informative non-numeric
  descriptor from the transaction context. Common Indian-bank-SMS shapes:
    "InfoACH*NSEMFS 28"  → merchant: "ACH NSEMFS"
    "UPI:611496812230"   → merchant: "UPI Transfer" (only when no payee name
                              appears anywhere else in the same message)
    "NEFT/AXIS/12345"    → merchant: "NEFT Transfer"
    "IMPS REF 123"       → merchant: "IMPS Transfer"
    "VPS DEBIT BANGLR"   → merchant: "POS BANGLR"
    "INB TRANSFER"       → merchant: "Internet Banking Transfer"
  The point is to give the user a row they can recognize later — pick something
  from the body. NEVER return null for merchant on a real transaction.
- DISCLAIMER BOILERPLATE IS NOT A NULL TRIGGER. Phrases like "If you did not authorize
  this transaction", "Trxn. not done by you? Report at...", "If this wasn't you, call...",
  "Not you? Call/SMS BLOCK..." appear on EVERY real bank transaction alert. They are
  boilerplate fraud-warning footers, not signs that the email is a system notification.
  When the email body has BOTH a specific debit/spend ("Rs. X has been debited",
  "Rs. X spent on", "Rs. X has been credited") AND a specific account/card identifier,
  it IS a real transaction. Return JSON, not null.
- TRANSFER COMPLETION CONFIRMATIONS ARE NOT TRANSACTIONS. Some banks send a separate
  follow-up email after the initiation alert just to confirm the transfer succeeded.
  That follow-up is a duplicate — return null for it. The null trigger requires at
  least ONE of these phrases as the email's PRIMARY message:
    * "transfer has been completed successfully"
    * "transfer was successful" / "transfer is successful"
    * "your transfer has been processed"
    * "we are happy to confirm" combined with "Reference Number:"
  If NONE of those phrases appear, the email is NOT a completion confirmation, even
  if the word "credited" appears. Normal debit alerts often say "INR X has been
  debited from your account ending XXAAAA and credited to the account ending XXBBBB"
  in a single sentence — that style IS a real debit transaction. The user's account
  is XXAAAA (tied to "debited"); XXBBBB is the payee's. Set merchant to a short label
  like "IMPS to XXBBBB" / "NEFT to XXBBBB" when no payee name appears, and
  amount_type="debit".
  Separately: in confirmation-style emails, "Credited to beneficiary A/c ending: XXNNNN"
  also describes the PAYEE's account — but only matters because it appears alongside
  the completion verbs above.
- RTGS/NEFT/IMPS INITIATION ALERTS *ARE* TRANSACTIONS. Do NOT return null just because
  the email mentions "RTGS" or "transfer" — only the COMPLETION confirmation rule above
  is a null trigger. The initiation alert uses verbs like "you have successfully
  initiated", "you have initiated", "you have transferred", "you initiated a transfer";
  it identifies the SOURCE account (e.g. "from your HDFC Bank A/c XX3300") and the
  PAYEE name (e.g. "for a transfer to payee SamplePayee"). This IS a debit transaction.
  Return JSON with amount_type="debit", account_last4 = the source account, merchant =
  the payee name. Two consecutive RTGS transfers on the same day are common — each
  initiation email is a distinct transaction.

Example 1 (HDFC alert):
Body: "Rs. 300000 has been deducted from your Account No. ending in XX1100 for a
Transfer to payee Bharat Mehta via HDFC Bank Online Banking."
Output: {{"account_name":"HDFC Bank Account","account_last4":"1100","merchant":"Bharat Mehta","amount":300000,"amount_type":"debit","currency":"INR","date":null,"category":"Transfer"}}

Example 2 (ICICI card swipe):
Body: "Your ICICI Bank Credit Card XX4400 has been used for a transaction of INR 847.32
on 22-Apr-2026 at SWIGGY BANGALORE."
Output: {{"account_name":"ICICI Bank Credit Card","account_last4":"4400","merchant":"SWIGGY BANGALORE","amount":847.32,"amount_type":"debit","currency":"INR","date":"2026-04-22","category":"Food"}}

Example 3 (Airtel bill receipt):
Body: "Thanks for your payment of Rs. 3200 towards Airtel Postpaid bill for mobile 9999999999."
Output: {{"account_name":"Airtel Postpaid","account_last4":null,"merchant":"Airtel","amount":3200,"amount_type":"debit","currency":"INR","date":null,"category":"Utilities"}}

Example 4 (HDFC e-mandate, DD/MM/YYYY date):
Body: "Your AcmeCloud bill, set up through E-mandate, has been successfully paid using your
HDFC Bank Debit Card ending 1234. Transaction Details: Amount: USD 14.00 Date: 09/04/2026 SI Hub ID: ABCDE."
Output: {{"account_name":"HDFC Bank Debit Card","account_last4":"1234","merchant":"AcmeCloud","amount":14.00,"amount_type":"debit","currency":"USD","date":"2026-04-09","category":"Other"}}

Example 5 (RTGS completion confirmation — DUPLICATE, return null):
Body: "Important Note: Your RTGS transfer has been completed successfully. We are happy to
confirm this for you. Transaction Details: Amount: INR 20,00,000.00 Credited to beneficiary
A/c ending: XX2200 Date & Time: 08-04-2026 at 14:52:58 Reference Number: HDFCR50000000000000000"
Output: null

Example 6 (RTGS INITIATION alert — REAL transaction, return JSON):
Body: "Thank you for banking with HDFC Bank. You have successfully initiated RTGS transaction
of Rs. 200000 from your HDFC Bank A/c XX3300 for a transfer to payee RaviSharma using HDFC
Bank Online Banking."
Output: {{"account_name":"HDFC Bank Account","account_last4":"3300","merchant":"RaviSharma","amount":200000,"amount_type":"debit","currency":"INR","date":null,"category":"Transfer"}}

Example 7 (HDFC IMPS debit with both "debited" and "credited to" in one sentence — REAL debit):
Body: "INR 10,000.00 has been debited from your account ending xxxxxxxxxx1100 on 04-04-26 and
credited to the account ending xxxxxxxxxx2200 via IMPS. IMPS Reference No: 600000000000
Available Balance: INR 2,37,562.00"
Output: {{"account_name":"HDFC Bank Account","account_last4":"1100","merchant":"IMPS to XX2200","amount":10000,"amount_type":"debit","currency":"INR","date":"2026-04-04","category":"Transfer"}}

Example 8 (ICICI SMS, ACH transfer with no proper-noun merchant — derive from descriptor):
Body: "ICICI Bank Acc XX440 debited Rs. 5,000.00 on 28-Apr-26 InfoACH*GENERIC 28.Avl Bal Rs. 49,237.87.To dispute call 18002662 or SMS BLOCK 440 to 9215676766"
Output: {{"account_name":"ICICI Bank Account","account_last4":"440","merchant":"ACH GENERIC","amount":5000,"amount_type":"debit","currency":"INR","date":"2026-04-28","category":"Transfer"}}

Example 9 (RTGS, 8-digit Indian-lakhs amount — "40,00,000.00" reads as 40 lakhs = 4000000, NOT 4 lakhs = 400000. Always strip ALL commas first, then parse the digits as a single number):
Body: "You have successfully initiated a RTGS transaction of Rs. 40,00,000.00 from your HDFC Bank A/c XX5500 for a transfer to payee BluHorizon using HDFC Bank Online Banking."
Output: {{"account_name":"HDFC Bank Account","account_last4":"5500","merchant":"BluHorizon","amount":4000000,"amount_type":"debit","currency":"INR","date":null,"category":"Transfer"}}

Example 10 (HDFC UPI debit to a VPA — the "If you did not authorize" disclaimer is BOILERPLATE on every bank alert; it does NOT make this a system notification. This IS a real transaction):
Body: "Dear Customer, Rs.5664.00 has been debited from account 5500 to VPA samplepayee@sbi SAMPLE PAYEE on 28-04-26. Your UPI transaction reference number is 300000000000. If you did not authorize this transaction, please report it immediately by calling 18002586161 Or SMS BLOCK UPI to 7308080808."
Output: {{"account_name":"HDFC Bank Account","account_last4":"5500","merchant":"SAMPLE PAYEE","amount":5664,"amount_type":"debit","currency":"INR","date":"2026-04-28","category":"Transfer"}}

Example 11 (SBI Card spend with "Trxn. not done by you?" disclaimer — same boilerplate, real transaction. Merchant comes from "spent ... at MERCHANT"):
Body: "Dear Cardholder, This is to inform you that, Rs.345.00 spent on your SBI Credit Card ending with 4400 at SAMPLESTORE on 27-04-26 via UPI (Ref No. 300000000001). Trxn. not done by you? Report at https://sbicard.com/Dispute. If you have not authorized this transaction please contact the SBI Card helpline."
Output: {{"account_name":"SBI Credit Card","account_last4":"4400","merchant":"SAMPLESTORE","amount":345,"amount_type":"debit","currency":"INR","date":"2026-04-27","category":"Shopping"}}

Numeric values extracted verbatim from this email body's amount-shaped tokens. The
"amount" field you return MUST equal exactly one of these. Pick the one that is
the transaction amount, NOT a phone number, reference number, or available balance:
{candidates}

Email:
---
{body}
---
"""


class ParsedTransaction(BaseModel):
    account_name: str
    account_last4: str | None = None
    merchant: str
    amount: float  # Pydantic coerces "1234.56" string -> float automatically
    amount_type: Literal["debit", "credit"]
    currency: str = "INR"
    date: Optional[_date] = None  # caller fills in from received_at if LLM returns null
    category: Literal[
        "Food", "Shopping", "Utilities", "Transport", "Healthcare",
        "Entertainment", "Transfer", "Salary", "Savings", "Other",
    ]

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v

    @field_validator("account_last4")
    @classmethod
    def strip_account_prefix(cls, v: str | None) -> str | None:
        # LLMs sometimes return "XX1234" — keep just the trailing digits.
        if v is None:
            return None
        digits = "".join(c for c in v if c.isdigit())
        return digits or None


@dataclass
class ParseResult:
    """Outcome of one parse attempt."""
    transaction: ParsedTransaction | None
    skipped: bool          # True = allowlist rejected, no LLM call made
    reason: str            # human-readable reason (logged, not displayed to user)


def is_transaction_email(from_addr: str | None, subject: str | None) -> bool:
    """Cheap pre-filter (D12). Returns True only if the email looks like a
    transaction notification. Used to skip OTPs, statement-ready notices,
    marketing, etc., before spending LLM cycles on them.
    """
    from_addr = (from_addr or "").lower()
    subject = (subject or "").lower()

    # Hard reject on known non-transaction subjects.
    for kw in NON_TRANSACTION_SUBJECT_KEYWORDS:
        if kw in subject:
            return False

    # Subject keyword match wins regardless of sender.
    for kw in TRANSACTION_SUBJECT_KEYWORDS:
        if kw in subject:
            return True

    # Otherwise require a known transactional sender.
    for pat in TRANSACTION_SENDER_PATTERNS:
        if re.search(pat, from_addr):
            return True

    return False


# --- verbatim amount check ---------------------------------------------------

# Strip common currency markers and the Indian "lakhs" comma style so we can
# match numbers like "1,23,456.78" or "Rs.1,234.56" or "₹1234".
_AMOUNT_NORMALIZE_RE = re.compile(r"[₹$£]|Rs\.?|INR|USD|EUR|GBP", re.IGNORECASE)
_AMOUNT_TOKEN_RE = re.compile(r"[\d,]+(?:\.\d+)?")


def _candidate_amounts_in(body_text: str) -> set[float]:
    """Pull every number-shaped token out of body_text and normalize to float.

    Indian lakhs format: "1,23,456.78" -> 123456.78
    Western: "1,234,567.89" -> 1234567.89
    Either way we just strip commas and parse.
    """
    cleaned = _AMOUNT_NORMALIZE_RE.sub(" ", body_text)
    tokens = _AMOUNT_TOKEN_RE.findall(cleaned)
    out: set[float] = set()
    for t in tokens:
        # A bare comma-grouped number can't end in a comma and must contain a digit.
        no_commas = t.replace(",", "")
        if not no_commas or not any(c.isdigit() for c in no_commas):
            continue
        try:
            out.add(float(no_commas))
        except ValueError:
            continue
    return out


def _verbatim_check(amount: float, body_text: str) -> bool:
    """Confirm `amount` appears in `body_text` in any common format.

    This is the primary defense against LLM hallucination — the LLM might
    invent an amount, but it can't make the digits appear in the body text
    if they aren't there. Tolerate sub-cent rounding (1234.56 vs 1234.5599).
    """
    candidates = _candidate_amounts_in(body_text)
    for c in candidates:
        if abs(c - amount) < 0.01:
            return True
    return False


# Cap candidates passed to the LLM at 100 crore (1e9). Real Indian retail
# transactions rarely approach 10 crore; phone numbers (10-11 digits) and
# reference numbers exceed 1e9 and would just confuse the model.
_AMOUNT_HINT_MAX = 1_000_000_000.0


def _format_candidate(c: float) -> str:
    """Render a candidate as a plain decimal: 4000000.0 -> '4000000', 1234.56 -> '1234.56'."""
    return str(int(c)) if c == int(c) else f"{c:.2f}"


def _candidates_for_hint(body_text: str) -> str:
    """Format candidate amounts for the prompt's `{candidates}` block.

    Filters obvious non-money values (phone numbers, refs >= 1e9), sorts
    descending so the largest realistic amounts appear first. The LLM uses
    this list to anchor its `amount` field, and the `_verbatim_check` is
    still the safety net regardless of what the LLM picks.
    """
    raw = _candidate_amounts_in(body_text)
    money = sorted(
        (c for c in raw if 0 < c < _AMOUNT_HINT_MAX),
        reverse=True,
    )
    if not money:
        return "(no amount-shaped tokens found in body)"
    return ", ".join(_format_candidate(c) for c in money)


# --- main parse entry --------------------------------------------------------


def parse_email(
    *,
    from_addr: str | None,
    subject: str | None,
    body_text: str,
    ollama_call,                 # callable(prompt: str) -> str  (raw LLM response text)
    received_at: _date | None = None,  # fallback when LLM returns date=null (HDFC InstaAlerts etc.)
) -> ParseResult:
    """Synchronous parse. Returns ParseResult.

    `ollama_call` is injected so tests can stub it without touching Ollama.
    `received_at` is used as the transaction date when the email body has
    no explicit date string (real-time bank alerts).
    """
    if not is_transaction_email(from_addr, subject):
        return ParseResult(None, skipped=True, reason="allowlist rejected")

    prompt = PARSE_PROMPT.format(
        body=body_text,
        candidates=_candidates_for_hint(body_text),
    )
    raw = ollama_call(prompt)

    # LLM determined this is not a transaction. Mark as skipped (not a retry-able
    # failure) — temperature=0 means a retry will give the same answer.
    if raw is None or raw.strip().lower() in ("null", "none", ""):
        return ParseResult(None, skipped=True, reason="LLM said not a transaction")

    # Strip code fences if the model added them.
    raw_clean = raw.strip()
    if raw_clean.startswith("```"):
        raw_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_clean, flags=re.MULTILINE).strip()

    try:
        payload = json.loads(raw_clean)
    except json.JSONDecodeError as e:
        return ParseResult(None, skipped=False, reason=f"JSON parse failed: {e}")

    if payload is None:
        return ParseResult(None, skipped=True, reason="LLM said not a transaction")

    try:
        tx = ParsedTransaction.model_validate(payload)
    except ValidationError as e:
        return ParseResult(None, skipped=False, reason=f"Pydantic validation failed: {e}")

    # Real-time alerts (HDFC InstaAlerts, ICICI SMS-style) often omit a date in the
    # body because the email arrives at the moment of the transaction. Fall back
    # to received_at when the LLM correctly returned date=null.
    if tx.date is None:
        if received_at is None:
            return ParseResult(None, skipped=False, reason="no date in body and no received_at fallback")
        tx.date = received_at

    if not _verbatim_check(tx.amount, body_text):
        return ParseResult(
            None, skipped=False,
            reason=f"verbatim check failed: {tx.amount} not in body",
        )

    return ParseResult(tx, skipped=False, reason="ok")


def signed_amount(tx: ParsedTransaction) -> float:
    """Convert (amount, amount_type) to a signed float for storage.
    debit -> negative, credit -> positive.
    `amount_type` itself is NOT stored.
    """
    return tx.amount if tx.amount_type == "credit" else -tx.amount


# --- bill notification path ----------------------------------------------
#
# Bills are notifications of FUTURE obligations (postpaid mobile, electricity,
# credit-card statement, EMI reminder). They are independent of transactions:
# a bill becomes a transaction only when the user pays it (which arrives via
# a separate "payment received" email — handled by the existing transaction
# parser). The bill detector is invoked AFTER parse_email() has returned null
# for an email; the two paths never compete.

BILL_SUBJECT_KEYWORDS = [
    "bill", "due", "invoice", "statement", "reminder", "outstanding", "emi",
    "payment due", "your bill", "monthly bill",
]

# Hard rejects: subjects that look like bill keywords but are actually
# already-paid receipts. The transaction parser handles those.
NON_BILL_SUBJECT_KEYWORDS = [
    "payment received", "payment successful", "thank you for your payment",
    "payment confirmation", "your payment of",
]


def is_bill_email(from_addr: str | None, subject: str | None) -> bool:
    """Cheap pre-filter — gate the LLM call to bill-shaped emails only."""
    s = (subject or "").lower()
    for kw in NON_BILL_SUBJECT_KEYWORDS:
        if kw in s:
            return False
    for kw in BILL_SUBJECT_KEYWORDS:
        if kw in s:
            return True
    return False


BILL_PROMPT = """Extract bill-due notification details from this email.
Return JSON only, no explanation, no markdown fences.

A "bill due" email is a notification that the user OWES money in the future:
- Mobile/internet postpaid bills (Airtel, Jio, Vi, BSNL)
- Utility bills (electricity, gas, water)
- Credit card statements with a due date
- Loan EMI reminders / installment reminders
- Insurance premium reminders

If the email is NOT a bill notification, return: null

Common non-bill cases that look superficially similar:
- "Thank you for your payment of ..." → already paid → null
- "Payment received" / "Payment successful" → already paid → null
- "Bill paid" / "Payment confirmation" → already paid → null
- A bank account statement summary with no specific amount-due-by-date → null
- Marketing about a bill payment service → null
- Transaction alert ("Rs. 500 has been debited") → that's a transaction → null
- OTP / login alert → null

If it IS a bill, return JSON with EXACTLY these fields:
{{
  "biller": "the company you owe (e.g. 'Airtel', 'Tata Power', 'ICICI Credit Card', 'BMC Water'). NEVER null.",
  "description": "short context like 'April postpaid bill' or 'Mar statement total due', or null if no clear context",
  "amount": 1234.56,
  "currency": "INR" or "USD",
  "due_date": "YYYY-MM-DD"
}}

Rules:
- amount is positive (it's the total you owe).
- The amount value MUST appear verbatim somewhere in the email body. Common
  formats: "1234.56", "1,234.56", "Rs. 1234.56", "₹1,234.56", "INR 1234.56",
  Indian lakhs format "1,23,456.78".
- Pick the TOTAL amount due, NOT the minimum-due. Use minimum-due only if the
  total isn't stated.
- DATE FORMAT: Indian banks/billers use DD/MM/YYYY for bare numeric dates
  ("28/04/2026" = 28 April 2026, NOT 4 February). Named-month dates ("28-Apr-2026")
  follow the named month. ISO dates ("2026-04-28") stay as-is.
- If multiple due dates appear, pick the LATEST one (the "pay by" date, not
  the billing-period start).

Example 1 (Airtel postpaid):
Body: "Your Airtel postpaid bill of Rs. 599.00 for the period 01-Apr-2026 to 30-Apr-2026
is due on 28-Apr-2026. Pay now to avoid late fees."
Output: {{"biller":"Airtel","description":"Apr postpaid bill","amount":599,"currency":"INR","due_date":"2026-04-28"}}

Example 2 (ICICI Credit Card statement):
Body: "Your ICICI Bank Credit Card XX4400 statement is ready. Total Amount Due:
Rs. 12,400.00. Minimum Amount Due: Rs. 620.00. Due Date: 15-May-2026."
Output: {{"biller":"ICICI Credit Card","description":"Card statement total due","amount":12400,"currency":"INR","due_date":"2026-05-15"}}

Example 3 (already-paid receipt — not a bill):
Body: "Thank you for your payment of Rs. 599 towards Airtel Postpaid bill."
Output: null

Example 4 (Tata Power electricity):
Body: "Tata Power Mumbai: Bill of Rs. 3,247.00 for March 2026, due 05/05/2026.
Account: 90012345. Pay online to avoid disconnection."
Output: {{"biller":"Tata Power","description":"Mar electricity bill","amount":3247,"currency":"INR","due_date":"2026-05-05"}}

Example 5 (HDFC credit card statement, content from a PDF attachment):
Body: "Your eStatement is attached with this mail.\n\n=== PDF ATTACHMENT TEXT ===\n
[PDF: statement.pdf]\nHDFC Bank Credit Card Statement\nCard ending 8800\n
Statement Date: 15-Apr-2026\nTotal Amount Due: Rs. 47,832.18\nMinimum Amount Due: Rs. 2,400.00\n
Payment Due Date: 03-May-2026\nPlease pay by the due date to avoid late fees."
Output: {{"biller":"HDFC Credit Card","description":"Apr statement total due","amount":47832.18,"currency":"INR","due_date":"2026-05-03"}}

Note: when the email contains a "=== PDF ATTACHMENT TEXT ===" section, treat
that block as additional context for the same bill — don't try to extract two
bills from one email. Marker text like "[encrypted PDF skipped: ...]" means
the PDF couldn't be read; if the body alone has no bill data, return null.

Email:
---
{body}
---
"""


class BillNotification(BaseModel):
    biller: str
    description: str | None = None
    amount: float
    currency: str = "INR"
    due_date: _date

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v


@dataclass
class BillResult:
    """Outcome of one bill-extraction attempt."""
    bill: BillNotification | None
    skipped: bool
    reason: str


def parse_bill(
    *,
    from_addr: str | None,
    subject: str | None,
    body_text: str,
    ollama_call,
) -> BillResult:
    """Try to extract a bill notification from an email.

    Independent of parse_email() — the caller decides which path to take.
    Recommended pipeline: run parse_email first; if it returns a transaction,
    you're done. Only call parse_bill when parse_email returned null/skipped.
    """
    if not is_bill_email(from_addr, subject):
        return BillResult(None, skipped=True, reason="bill allowlist rejected")

    prompt = BILL_PROMPT.format(body=body_text)
    raw = ollama_call(prompt)

    if raw is None or raw.strip().lower() in ("null", "none", ""):
        return BillResult(None, skipped=True, reason="LLM said not a bill")

    raw_clean = raw.strip()
    if raw_clean.startswith("```"):
        raw_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_clean,
                           flags=re.MULTILINE).strip()

    try:
        payload = json.loads(raw_clean)
    except json.JSONDecodeError as e:
        return BillResult(None, skipped=False, reason=f"JSON parse failed: {e}")

    if payload is None:
        return BillResult(None, skipped=True, reason="LLM said not a bill")

    try:
        bill = BillNotification.model_validate(payload)
    except ValidationError as e:
        return BillResult(None, skipped=False, reason=f"Pydantic validation failed: {e}")

    if not _verbatim_check(bill.amount, body_text):
        return BillResult(
            None, skipped=False,
            reason=f"verbatim check failed: {bill.amount} not in body",
        )

    return BillResult(bill, skipped=False, reason="ok")

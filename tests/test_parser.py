"""Day 1 critical tests — parser.

Each test uses a stub ollama_call so the parser is exercised in isolation
from a real LLM. The point is to validate the contract (Pydantic, verbatim
check, code-path branching), not LLM accuracy. LLM accuracy is the
separate eval (TODO-4 in TODOS.md).
"""
from __future__ import annotations

import json
from pathlib import Path

import html2text

from parser import (
    ParsedTransaction,
    ParseResult,
    _candidates_for_hint,
    is_transaction_email,
    parse_email,
    signed_amount,
)


def _strip(html: str) -> str:
    h = html2text.HTML2Text()
    h.ignore_links = True
    return h.handle(html)


def _stub_ollama(payload: dict | None | str):
    """Return a callable that always returns the given response."""
    def _call(_prompt: str) -> str:
        if payload is None:
            return "null"
        if isinstance(payload, str):
            return payload
        return json.dumps(payload)
    return _call


# ---------- Test 1 — ICICI HTML email ----------------------------------------

def test_parse_html_icici_email(fixtures_dir: Path):
    html = (fixtures_dir / "icici_debit_sample.html").read_text()
    body_text = _strip(html)

    # Stub the LLM with what it SHOULD return for this fixture.
    ollama = _stub_ollama({
        "account_name": "ICICI Bank Credit Card",
        "account_last4": "4400",
        "merchant": "SWIGGY BANGALORE",
        "amount": 1234.56,
        "amount_type": "debit",
        "currency": "INR",
        "date": "2026-04-22",
        "category": "Food",
    })

    result = parse_email(
        from_addr="alerts@icicibank.com",
        subject="ICICI Bank Credit Card Transaction Alert",
        body_text=body_text,
        ollama_call=ollama,
    )

    assert result.transaction is not None, f"failed: {result.reason}"
    tx = result.transaction
    assert tx.amount == 1234.56
    assert tx.merchant == "SWIGGY BANGALORE"
    assert str(tx.date) == "2026-04-22"
    assert tx.account_last4 == "4400"
    assert signed_amount(tx) == -1234.56  # debit -> negative


# ---------- Test 2 — HDFC HTML email -----------------------------------------

def test_parse_hdfc_email(fixtures_dir: Path):
    html = (fixtures_dir / "hdfc_credit_sample.html").read_text()
    body_text = _strip(html)

    ollama = _stub_ollama({
        "account_name": "HDFC Bank Credit Card",
        "account_last4": "8800",
        "merchant": "AMAZON IN",
        "amount": 67.43,
        "amount_type": "debit",
        "currency": "INR",
        "date": "2026-04-22",
        "category": "Shopping",
    })

    result = parse_email(
        from_addr="alerts@hdfcbank.net",
        subject="HDFC Bank Credit Card Spend Alert",
        body_text=body_text,
        ollama_call=ollama,
    )

    assert result.transaction is not None, f"failed: {result.reason}"
    assert result.transaction.account_last4 == "8800"
    assert result.transaction.merchant == "AMAZON IN"


# ---------- Test 3 — verbatim check rejects hallucinated amount --------------

def test_amount_verbatim_check_fail():
    body_text = "Your ICICI account was debited Rs. 1234.56 on 22-Apr-2026 at SWIGGY."
    # LLM hallucinates a totally different amount that ISN'T in the body.
    ollama = _stub_ollama({
        "account_name": "ICICI Bank",
        "account_last4": "4400",
        "merchant": "SWIGGY",
        "amount": 9999.99,
        "amount_type": "debit",
        "currency": "INR",
        "date": "2026-04-22",
        "category": "Food",
    })

    result = parse_email(
        from_addr="alerts@icicibank.com",
        subject="Transaction alert",
        body_text=body_text,
        ollama_call=ollama,
    )

    assert result.transaction is None
    assert "verbatim" in result.reason.lower()


# ---------- Test 4 — Pydantic coerces string amount to float -----------------

def test_pydantic_coercion_string_amount():
    body_text = "Debited Rs. 1234.56 at AMAZON on 22-Apr-2026."
    # LLM returns amount as a STRING, not a float (some models do this).
    ollama = _stub_ollama({
        "account_name": "HDFC Bank",
        "account_last4": "8800",
        "merchant": "AMAZON",
        "amount": "1234.56",  # string!
        "amount_type": "debit",
        "currency": "INR",
        "date": "2026-04-22",
        "category": "Shopping",
    })

    result = parse_email(
        from_addr="alerts@hdfcbank.net",
        subject="Transaction alert",
        body_text=body_text,
        ollama_call=ollama,
    )

    assert result.transaction is not None, f"failed: {result.reason}"
    assert isinstance(result.transaction.amount, float)
    assert result.transaction.amount == 1234.56


# ---------- Test 5 — Indian lakhs format ----------------------------------------

def test_lakhs_format():
    # "1,23,456.78" lakhs notation. The verbatim check must recognize this
    # as the same value the LLM returns (123456.78).
    body_text = (
        "Your salary of INR 1,23,456.78 has been credited to your "
        "HDFC Bank Salary Account XX1234 on 25-Apr-2026."
    )
    ollama = _stub_ollama({
        "account_name": "HDFC Bank Salary Account",
        "account_last4": "1234",
        "merchant": "Employer Salary",
        "amount": 123456.78,
        "amount_type": "credit",
        "currency": "INR",
        "date": "2026-04-25",
        "category": "Salary",
    })

    result = parse_email(
        from_addr="alerts@hdfcbank.net",
        subject="Salary credited",
        body_text=body_text,
        ollama_call=ollama,
    )

    assert result.transaction is not None, f"failed: {result.reason}"
    assert result.transaction.amount == 123456.78
    assert signed_amount(result.transaction) == 123456.78  # credit -> positive


# ---------- Test 6 — 8-digit Indian-lakhs (40,00,000.00 = 40 lakhs) ----------

def test_lakhs_8_digit_format():
    """Regression: HDFC RTGS for Rs. 40,00,000.00 (40 lakhs = 4,000,000).

    qwen2.5:7b previously misread this as 4 lakhs (400000), failing the
    verbatim check. The fix: prompt example + `{candidates}` hint listing
    pre-extracted amounts so the LLM picks from a verified set.
    """
    from datetime import date

    body_text = (
        "Dear Customer,\n\n"
        "Thank you for banking with HDFC Bank.\n\n"
        "You have successfully initiated a RTGS transaction of Rs. 40,00,000.00 "
        "from your HDFC Bank A/c XX5500 for a transfer to payee BluHorizon using "
        "HDFC Bank Online Banking.\n\n"
        "Not you? Call 18002586161/SMS 'BLOCK OB' to 7308080808 from your registered "
        "mobile number."
    )
    ollama = _stub_ollama({
        "account_name": "HDFC Bank Account",
        "account_last4": "5500",
        "merchant": "BluHorizon",
        "amount": 4000000,
        "amount_type": "debit",
        "currency": "INR",
        "date": None,
        "category": "Transfer",
    })

    result = parse_email(
        from_addr="alerts@hdfcbank.bank.in",
        subject="View: Account update for your HDFC Bank A/c",
        body_text=body_text,
        ollama_call=ollama,
        received_at=date(2026, 4, 28),
    )

    assert result.transaction is not None, f"failed: {result.reason}"
    tx = result.transaction
    assert tx.amount == 4000000.0
    assert tx.merchant == "BluHorizon"
    assert tx.account_last4 == "5500"
    assert str(tx.date) == "2026-04-28"  # received_at fallback
    assert signed_amount(tx) == -4000000.0  # debit -> negative


def test_candidates_hint_filters_phone_numbers():
    """Phone numbers (10-11 digits, ≥ 1e9) and reference numbers should not
    appear in the candidates hint. Real transaction amounts should."""
    body = (
        "Rs. 40,00,000.00 from HDFC A/c XX5500. "
        "Call 18002586161/SMS to 7308080808. "
        "Reference: HDFCR50000000000000000"
    )
    hint = _candidates_for_hint(body)
    assert "4000000" in hint, hint
    assert "18002586161" not in hint, hint
    assert "7308080808" not in hint, hint


def test_candidates_hint_empty_body():
    """No amount-shaped tokens → safe placeholder, not a crash."""
    assert _candidates_for_hint("Hello, just text.") == "(no amount-shaped tokens found in body)"


# ---------- Test 7 — HDFC UPI debit, "If you did not authorize" boilerplate --

def test_hdfc_upi_debit_with_fraud_disclaimer():
    """Regression: HDFC UPI debit alerts include a boilerplate "If you did not
    authorize this transaction" footer. The LLM previously misclassified the
    whole email as a system notification and returned null. The fix: explicit
    Example 10 + boilerplate-clarification rule.
    """
    body_text = (
        "Dear Customer, Rs.5664.00 has been debited from account 5500 to VPA "
        "samplepayee@sbi SAMPLE PAYEE on 28-04-26. Your UPI transaction reference "
        "number is 300000000000. If you did not authorize this transaction, "
        "please report it immediately by calling 18002586161 Or SMS BLOCK UPI "
        "to 7308080808. Warm Regards, HDFC Bank"
    )
    ollama = _stub_ollama({
        "account_name": "HDFC Bank Account",
        "account_last4": "5500",
        "merchant": "SAMPLE PAYEE",
        "amount": 5664,
        "amount_type": "debit",
        "currency": "INR",
        "date": "2026-04-28",
        "category": "Transfer",
    })
    result = parse_email(
        from_addr="alerts@hdfcbank.bank.in",
        subject="❗  You have done a UPI txn. Check details!",
        body_text=body_text,
        ollama_call=ollama,
    )
    assert result.transaction is not None, f"failed: {result.reason}"
    tx = result.transaction
    assert tx.amount == 5664.0
    assert tx.merchant == "SAMPLE PAYEE"
    assert tx.account_last4 == "5500"
    assert str(tx.date) == "2026-04-28"


# ---------- Test 8 — SBI Card spend with "Trxn. not done by you?" disclaimer -

def test_sbi_card_spend_with_fraud_disclaimer():
    """Regression: SBI Card alerts include a "Trxn. not done by you?" disclaimer
    that previously caused null misclassification. Same fix path as Test 7."""
    body_text = (
        "Dear Cardholder, This is to inform you that, Rs.345.00 spent on your "
        "SBI Credit Card ending with 4400 at SAMPLESTORE on 27-04-26 via UPI "
        "(Ref No. 300000000001). Trxn. not done by you? Report at "
        "https://sbicard.com/Dispute. If you have not authorized this "
        "transaction please contact the SBI Card helpline."
    )
    ollama = _stub_ollama({
        "account_name": "SBI Credit Card",
        "account_last4": "4400",
        "merchant": "SAMPLESTORE",
        "amount": 345,
        "amount_type": "debit",
        "currency": "INR",
        "date": "2026-04-27",
        "category": "Shopping",
    })
    result = parse_email(
        from_addr="onlinesbicard@sbicard.com",
        subject="Transaction Alert from SBI Card",
        body_text=body_text,
        ollama_call=ollama,
    )
    assert result.transaction is not None, f"failed: {result.reason}"
    tx = result.transaction
    assert tx.amount == 345.0
    assert tx.merchant == "SAMPLESTORE"
    assert tx.account_last4 == "4400"
    assert str(tx.date) == "2026-04-27"


# ---------- Bonus: allowlist sanity -------------------------------------------

def test_allowlist_skips_otp_email():
    """Belt-and-braces: D12 allowlist should reject OTP emails before LLM."""
    result = parse_email(
        from_addr="alerts@icicibank.com",
        subject="Your OTP for login",
        body_text="Your OTP is 123456. Valid for 5 mins.",
        ollama_call=_stub_ollama({"should": "never be called"}),
    )
    assert result.skipped is True
    assert result.transaction is None


def test_allowlist_accepts_transaction_subject():
    assert is_transaction_email("alerts@icicibank.com", "Transaction debited")
    assert is_transaction_email("noreply@randombank.com", "Salary credited to your account")
    assert not is_transaction_email("offers@bank.com", "50% cashback offer this weekend")

"""
utils_reports.py
PDF statement generation for BankOS.

Generates a formatted PDF account statement using fpdf2.
Each statement includes:
  - Bank header + generation timestamp
  - Account summary (balance shown in native + converted currency)
  - Full transaction history (up to 50 most recent entries)

Requires: pip install fpdf2
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from utils_currency import format_money, format_money_dual


# ─────────────────────────────────────────────────────────────────────────────
#  PDF statement generator
# ─────────────────────────────────────────────────────────────────────────────

def generate_pdf_statement(
    conn: sqlite3.Connection,
    acc_num: int,
    out_path: str | None = None,
    tx_log_conn: sqlite3.Connection | None = None,
) -> str:
    """
    Generate a PDF account statement for acc_num.

    Args:
        conn:        Open SQLite connection to the main DB (row_factory=sqlite3.Row).
        acc_num:     Account number to generate statement for.
        out_path:    Full file path for the PDF.
                     If None, the user is prompted.
        tx_log_conn: Connection to the transaction log DB. If None, falls back to conn.

    Returns:
        Human-readable result string (success path or error message).
    """
    try:
        from fpdf import FPDF
    except ImportError:
        raise ValueError("[ERROR] fpdf2 not installed. Run: pip install fpdf2")

    # ── fetch account row ─────────────────────────────────────────────────────
    account = conn.execute(
        "SELECT * FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not account:
        raise ValueError(f"[ERROR] Account {acc_num} not found.")

    # ── fetch last 50 transactions ────────────────────────────────────────────
    tx_db = tx_log_conn if tx_log_conn is not None else conn
    transactions = tx_db.execute(
        """SELECT * FROM transaction_log
           WHERE acc_num=?
           ORDER BY timestamp DESC
           LIMIT 50""",
        (acc_num,),
    ).fetchall()

    # ── resolve output path ───────────────────────────────────────────────────
    if out_path is None:
        out_path = (
            f"statements/statement_{acc_num}_"
            f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.pdf"
        )

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    # ── build PDF ─────────────────────────────────────────────────────────────
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    _add_header(pdf, acc_num)
    _add_account_summary(pdf, account)

    if transactions:
        _add_transaction_table(pdf, transactions, account["currency"])
    else:
        pdf.set_font("Arial", "I", 10)
        pdf.cell(0, 8, "No transactions found.", ln=1)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pdf.output(out_path)
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
#  PDF section builders
# ─────────────────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    """Sanitize text to latin-1 compatible ASCII for core PDF fonts."""
    if not isinstance(text, str):
        text = str(text)
    replacements = {
        "—": "-",
        "–": "-",
        "৳": "BDT ",
        "≈": "~",
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "•": "*",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")


def _add_header(pdf, acc_num: int) -> None:
    """Bank name, statement title, and generation timestamp."""
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, _clean("BankOS - Account Statement"), ln=1, align="C")

    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, _clean(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"), ln=1, align="C")
    pdf.cell(0, 6, _clean(f"Account Number: {acc_num}"), ln=1, align="C")
    pdf.ln(6)

    # horizontal rule
    pdf.set_draw_color(100, 100, 100)
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)


def _add_account_summary(pdf, account: sqlite3.Row) -> None:
    """Account details block: type, currency, balance, credit info, vault, status."""
    currency = account["currency"]

    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Account Summary", ln=1)
    pdf.set_font("Arial", "", 10)

    rows = [
        ("Account Number",   str(account["acc_num"])),
        ("Account Type",     account["acc_type"].replace("_", " ").title()),
        ("Currency",         currency),
        ("Balance",          format_money_dual(account["acc_balance"], currency)),
        ("Status",           "FROZEN" if account["is_frozen"] else "Active"),
        ("Daily Transfer Limit", format_money_dual(account["daily_transfer_limit"], currency)),
    ]

    if account["acc_type"] == "credit_card":
        rows += [
            ("Credit Card",      _mask_card(account["credit_card_num"])),
            ("Credit Limit",     format_money_dual(account["credit_card_limit"], currency)),
            ("Credit Used",      format_money_dual(account["credit_used"], currency)),
            ("Credit Available", format_money_dual(
                account["credit_card_limit"] - account["credit_used"], currency
            )),
        ]

    if account["vault_no"]:
        rows += [
            ("Vault Number",  account["vault_no"]),
            ("Vault Balance", format_money_dual(account["vault_balance"], currency)),
        ]

    for label, value in rows:
        pdf.set_font("Arial", "B", 10)
        pdf.cell(60, 7, f"{label}:", border=0)
        pdf.set_font("Arial", "", 10)
        pdf.cell(0, 7, _clean(value), ln=1)

    pdf.ln(4)


def _add_transaction_table(pdf, transactions: list[sqlite3.Row], native_currency: str) -> None:
    """Render the transaction history table."""
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, f"Transaction History (last {len(transactions)})", ln=1)
    pdf.ln(2)

    # table header
    col_widths = [35, 25, 35, 20, 20, 25, 30]
    headers    = ["Timestamp", "Type", "Amount", "Currency", "Category", "Status", "Balance After"]

    pdf.set_font("Arial", "B", 8)
    pdf.set_fill_color(220, 220, 220)
    for header, w in zip(headers, col_widths):
        pdf.cell(w, 7, header, border=1, fill=True)
    pdf.ln()

    # table rows
    pdf.set_font("Arial", "", 8)
    for i, tx in enumerate(transactions):
        fill = (i % 2 == 0)
        if fill:
            pdf.set_fill_color(245, 245, 245)
        else:
            pdf.set_fill_color(255, 255, 255)

        row_data = [
            tx["timestamp"],
            tx["type"],
            format_money(tx["amount"], tx["currency"]),
            tx["currency"],
            tx["category"],
            tx["status"].upper(),
            format_money(tx["balance_after"], native_currency),
        ]
        for value, w in zip(row_data, col_widths):
            pdf.cell(w, 6, _clean(str(value)), border=1, fill=True)
        pdf.ln()

    pdf.ln(4)
    pdf.set_font("Arial", "I", 8)
    pdf.cell(0, 6, "* Amounts shown in transaction currency. Balance After in account native currency.", ln=1)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mask_card(card_num: str | None) -> str:
    """Return masked card number showing only last 4 digits."""
    if not card_num or len(card_num) < 4:
        return "N/A"
    return f"**** **** **** {card_num[-4:]}"
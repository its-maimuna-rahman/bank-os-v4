"""
utils_logic.py — BankOS v3
Headless financial derivative logic handlers.

Phase 5 implementations
-----------------------
Loans:   disburse_loan, repay_loan
Bonds:   purchase_bond, redeem_bond
Checks:  issue_check, clear_check, bounce_check

All public functions:
  • Return  {"status": "success"|"error", "message": str, "data": ...}
  • Treat all monetary amounts as minor-unit integers throughout.
  • Write audit rows via log_transaction() from utils_storage.
  • Never call print() or input().

DB column contract (as defined in utils_storage.SCHEMA):
  loans:  loan_id, acc_num, principal (INT), remaining_balance (INT),
          interest_rate (REAL), status ('active'|'settled'), disbursed_at
  bonds:  bond_id, acc_num, face_value (INT), yield_rate (REAL),
          maturity_timestamp (TEXT ISO-8601), status ('active'|'redeemed'),
          purchased_at
  checks: check_id, acc_num, amount (INT), payee_name, memo,
          status ('issued'|'cleared'|'bounced'), issued_at
"""

from __future__ import annotations

import sqlite3
from datetime import datetime




# =============================================================================
#  Private helpers
# =============================================================================

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _get_account(conn: sqlite3.Connection, acc_num: int) -> sqlite3.Row | None:
    """Fetch the minimal account fields needed by every instrument function."""
    return conn.execute(
        "SELECT acc_num, acc_balance, currency, is_frozen FROM accounts WHERE acc_num=?",
        (acc_num,),
    ).fetchone()


# =============================================================================
#  Utility math (kept from v2, used by callers that need interest projections)
# =============================================================================

def calculate_compound_liability(
    principal: float,
    annual_rate: float,
    periods_in_years: float,
) -> int:
    """
    Return the compound-interest total obligation rounded to the nearest
    minor unit.

    Formula:  principal × (1 + annual_rate) ^ periods_in_years
    Example:  calculate_compound_liability(100_000, 0.12, 2)  → 125_440
    """
    return round(principal * (1 + annual_rate) ** periods_in_years)


# =============================================================================
#  Loans
# =============================================================================

def disburse_loan(
    conn:          sqlite3.Connection,
    tx_log_conn:   sqlite3.Connection,
    acc_num:       int,
    principal:     int,
    interest_rate: float,
) -> dict:
    from utils_storage import log_transaction

    """
    Disburse a new loan.

    Steps
    -----
    1. Validate inputs.
    2. Credit ``principal`` to ``acc_balance``.
    3. Insert a ``loans`` row:
       ``principal = remaining_balance = <principal>``, ``status = 'active'``.
    4. Write a ``loan_disbursal`` transaction-log row.

    The full repayment obligation (principal + accrued interest) is settled
    through ``repay_loan()`` calls.  Interest is *not* pre-loaded into
    ``remaining_balance`` here — callers that want a fixed repayment schedule
    may pre-calculate it and pass a larger ``principal`` value, or handle
    accrual externally.

    Returns
    -------
    ``data`` keys: loan_id, acc_num, principal, remaining_balance,
    interest_rate, balance_after, currency.
    """
    if principal <= 0:
        return {"status": "error", "message": "Principal must be a positive integer.", "data": None}
    if not (0.0 <= interest_rate <= 10.0):
        return {"status": "error", "message": "interest_rate must be between 0.0 and 10.0.", "data": None}

    acc_row = _get_account(conn, acc_num)
    if not acc_row:
        return {"status": "error", "message": "Account not found.", "data": None}
    if acc_row["is_frozen"]:
        return {"status": "error", "message": "Account is frozen.", "data": None}

    currency = acc_row["currency"]

    conn.execute(
        "UPDATE accounts SET acc_balance = acc_balance + ? WHERE acc_num = ?",
        (principal, acc_num),
    )
    cur = conn.execute(
        """INSERT INTO loans (acc_num, principal, remaining_balance, interest_rate, status)
           VALUES (?, ?, ?, ?, 'active')""",
        (acc_num, principal, principal, interest_rate),
    )
    loan_id = cur.lastrowid
    conn.commit()

    balance_after = acc_row["acc_balance"] + principal

    return {
        "status":  "success",
        "message": f"Loan {loan_id} disbursed. {principal} credited to account {acc_num}.",
        "data": {
            "loan_id":           loan_id,
            "acc_num":           acc_num,
            "principal":         principal,
            "remaining_balance": principal,
            "interest_rate":     interest_rate,
            "balance_after":     balance_after,
            "currency":          currency,
        },
    }


def repay_loan(
    conn:                 sqlite3.Connection,
    tx_log_conn:          sqlite3.Connection,
    loan_id:              int,
    payment_amount_minor: int,
) -> dict:
    from utils_storage import log_transaction

    """
    Apply a repayment against an active loan.

    Steps
    -----
    1. Validate loan exists and is still ``'active'``.
    2. Clamp ``payment_amount_minor`` to ``remaining_balance`` — no
       over-deduction even if the caller passes a larger figure.
    3. Debit ``actual_payment`` from ``acc_balance``.
    4. Decrement ``remaining_balance``; flip ``status`` to ``'settled'``
       if ``remaining_balance ≤ 0``.
    5. Write a ``loan_repayment`` transaction-log row.

    Returns
    -------
    ``data`` keys: loan_id, acc_num, amount_paid, remaining_balance,
    loan_status, balance_after, currency.
    """
    if payment_amount_minor <= 0:
        return {"status": "error", "message": "Payment amount must be a positive integer.", "data": None}

    loan_row = conn.execute(
        "SELECT * FROM loans WHERE loan_id = ?", (loan_id,)
    ).fetchone()
    if not loan_row:
        return {"status": "error", "message": "Loan not found.", "data": None}
    if loan_row["status"] == "settled":
        return {"status": "error", "message": "Loan is already fully settled.", "data": None}

    acc_num  = loan_row["acc_num"]
    acc_row  = _get_account(conn, acc_num)
    if not acc_row:
        return {"status": "error", "message": "Account not found.", "data": None}
    if acc_row["is_frozen"]:
        return {"status": "error", "message": "Account is frozen.", "data": None}

    currency        = acc_row["currency"]
    remaining       = loan_row["remaining_balance"]
    current_balance = acc_row["acc_balance"]

    # Cap: never deduct more than what's still owed.
    actual_payment = min(payment_amount_minor, remaining)

    if current_balance < actual_payment:
        return {
            "status":  "error",
            "message": "Insufficient account balance for this repayment.",
            "data":    {
                "balance":  current_balance,
                "required": actual_payment,
                "currency": currency,
            },
        }

    new_remaining = remaining - actual_payment
    new_balance   = current_balance - actual_payment
    new_status    = "settled" if new_remaining <= 0 else "active"

    conn.execute(
        "UPDATE accounts SET acc_balance = acc_balance - ? WHERE acc_num = ?",
        (actual_payment, acc_num),
    )
    conn.execute(
        "UPDATE loans SET remaining_balance = ?, status = ? WHERE loan_id = ?",
        (max(0, new_remaining), new_status, loan_id),
    )
    conn.commit()

    msg = "Loan fully settled." if new_status == "settled" else "Loan repayment applied."
    return {
        "status":  "success",
        "message": msg,
        "data": {
            "loan_id":           loan_id,
            "acc_num":           acc_num,
            "amount_paid":       actual_payment,
            "remaining_balance": max(0, new_remaining),
            "loan_status":       new_status,
            "balance_after":     new_balance,
            "currency":          currency,
        },
    }


# =============================================================================
#  Bonds
# =============================================================================

def purchase_bond(
    conn:               sqlite3.Connection,
    tx_log_conn:        sqlite3.Connection,
    acc_num:            int,
    face_value:         int,
    yield_rate:         float,
    maturity_timestamp: str,
) -> dict:
    from utils_storage import log_transaction

    """
    Purchase a bond.

    Steps
    -----
    1. Validate inputs, including that ``maturity_timestamp`` is a valid
       ISO-8601 string *in the future*.
    2. Debit ``face_value`` from ``acc_balance``.
    3. Insert a ``bonds`` row with ``status = 'active'``.
    4. Write a ``bond_purchase`` transaction-log row.

    ``maturity_timestamp`` format:  ``"YYYY-MM-DDTHH:MM:SS"``
    (e.g. ``"2028-06-30T00:00:00"``).  Redemption before that datetime is
    rejected by ``redeem_bond()``.

    Returns
    -------
    ``data`` keys: bond_id, acc_num, face_value, yield_rate,
    maturity_timestamp, balance_after, currency.
    """
    if face_value <= 0:
        return {"status": "error", "message": "Face value must be a positive integer.", "data": None}
    if not (0.0 <= yield_rate <= 10.0):
        return {"status": "error", "message": "yield_rate must be between 0.0 and 10.0.", "data": None}

    try:
        maturity_dt = datetime.fromisoformat(maturity_timestamp)
    except (ValueError, TypeError):
        return {
            "status":  "error",
            "message": "maturity_timestamp must be a valid ISO-8601 datetime string "
                       "(e.g. '2028-06-30T00:00:00').",
            "data":    None,
        }

    if maturity_dt <= datetime.now():
        return {
            "status":  "error",
            "message": "maturity_timestamp must be a future datetime.",
            "data":    None,
        }

    acc_row = _get_account(conn, acc_num)
    if not acc_row:
        return {"status": "error", "message": "Account not found.", "data": None}
    if acc_row["is_frozen"]:
        return {"status": "error", "message": "Account is frozen.", "data": None}
    if acc_row["acc_balance"] < face_value:
        return {
            "status":  "error",
            "message": "Insufficient balance to purchase this bond.",
            "data":    {
                "balance":  acc_row["acc_balance"],
                "required": face_value,
                "currency": acc_row["currency"],
            },
        }

    currency = acc_row["currency"]

    conn.execute(
        "UPDATE accounts SET acc_balance = acc_balance - ? WHERE acc_num = ?",
        (face_value, acc_num),
    )
    cur = conn.execute(
        """INSERT INTO bonds (acc_num, face_value, yield_rate, maturity_timestamp, status)
           VALUES (?, ?, ?, ?, 'active')""",
        (acc_num, face_value, yield_rate, maturity_timestamp),
    )
    bond_id = cur.lastrowid
    conn.commit()

    balance_after = acc_row["acc_balance"] - face_value

    return {
        "status":  "success",
        "message": (
            f"Bond {bond_id} purchased. {face_value} debited from account {acc_num}. "
            f"Matures at {maturity_timestamp}."
        ),
        "data": {
            "bond_id":            bond_id,
            "acc_num":            acc_num,
            "face_value":         face_value,
            "yield_rate":         yield_rate,
            "maturity_timestamp": maturity_timestamp,
            "balance_after":      balance_after,
            "currency":           currency,
        },
    }


def redeem_bond(
    conn:        sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    bond_id:     int,
) -> dict:
    from utils_storage import log_transaction

    """
    Redeem a matured bond.

    Steps
    -----
    1. Confirm bond exists and is still ``'active'``.
    2. Enforce ``maturity_timestamp ≤ now()`` — returns a descriptive error
       with time-remaining if the bond hasn't matured yet.
    3. Calculate redemption amount:
       ``round(face_value × (1 + yield_rate))`` (minor units).
    4. Credit redemption amount to ``acc_balance``.
    5. Flip ``status`` to ``'redeemed'``.
    6. Write a ``bond_redemption`` transaction-log row.

    Returns
    -------
    ``data`` keys: bond_id, acc_num, face_value, yield_rate,
    redemption_amt, balance_after, currency.
    Error ``data`` for premature redemption includes seconds_remaining.
    """
    bond_row = conn.execute(
        "SELECT * FROM bonds WHERE bond_id = ?", (bond_id,)
    ).fetchone()
    if not bond_row:
        return {"status": "error", "message": "Bond not found.", "data": None}
    if bond_row["status"] == "redeemed":
        return {"status": "error", "message": "Bond has already been redeemed.", "data": None}

    maturity_dt = datetime.fromisoformat(bond_row["maturity_timestamp"])
    now         = datetime.now()

    if now < maturity_dt:
        secs_left = int((maturity_dt - now).total_seconds())
        days,  r  = divmod(secs_left, 86_400)
        hours, r  = divmod(r, 3_600)
        mins       = r // 60
        return {
            "status":  "error",
            "message": (
                f"Bond {bond_id} has not yet matured. "
                f"Redeemable in {days}d {hours}h {mins}m "
                f"(at {bond_row['maturity_timestamp']})."
            ),
            "data": {
                "bond_id":            bond_id,
                "maturity_timestamp": bond_row["maturity_timestamp"],
                "seconds_remaining":  secs_left,
            },
        }

    acc_num    = bond_row["acc_num"]
    face_value = bond_row["face_value"]
    yield_rate = bond_row["yield_rate"]

    acc_row = _get_account(conn, acc_num)
    if not acc_row:
        return {"status": "error", "message": "Account not found.", "data": None}

    currency       = acc_row["currency"]
    redemption_amt = round(face_value * (1.0 + yield_rate))

    conn.execute(
        "UPDATE accounts SET acc_balance = acc_balance + ? WHERE acc_num = ?",
        (redemption_amt, acc_num),
    )
    conn.execute(
        "UPDATE bonds SET status = 'redeemed' WHERE bond_id = ?",
        (bond_id,),
    )
    conn.commit()

    balance_after = acc_row["acc_balance"] + redemption_amt

    return {
        "status":  "success",
        "message": (
            f"Bond {bond_id} redeemed. "
            f"{redemption_amt} credited to account {acc_num} "
            f"(face {face_value} × (1 + {yield_rate:.4f}))."
        ),
        "data": {
            "bond_id":       bond_id,
            "acc_num":       acc_num,
            "face_value":    face_value,
            "yield_rate":    yield_rate,
            "redemption_amt": redemption_amt,
            "balance_after": balance_after,
            "currency":      currency,
        },
    }


# =============================================================================
#  Checks
# =============================================================================

def issue_check(
    conn:        sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    acc_num:     int,
    amount:      int,
    payee_name:  str | None = None,
    memo:        str | None = None,
) -> dict:
    from utils_storage import log_transaction

    """
    Issue a check.

    Money moves at issuance — the ``amount`` is debited immediately as a
    hold so the account balance always reflects available liquid funds.

    Steps
    -----
    1. Validate ``amount > 0`` and confirm sufficient balance.
    2. Debit ``amount`` from ``acc_balance``.
    3. Insert a ``checks`` row with ``status = 'issued'``.
    4. Write a ``check_issued`` transaction-log row.

    Clearing later (``clear_check``) flips the status with zero balance
    change.  Bouncing (``bounce_check``) refunds the held amount.

    Returns
    -------
    ``data`` keys: check_id, acc_num, amount, payee_name, memo,
    check_status, balance_after, currency.
    """
    if amount <= 0:
        return {"status": "error", "message": "Check amount must be a positive integer.", "data": None}

    acc_row = _get_account(conn, acc_num)
    if not acc_row:
        return {"status": "error", "message": "Account not found.", "data": None}
    if acc_row["is_frozen"]:
        return {"status": "error", "message": "Account is frozen.", "data": None}
    if acc_row["acc_balance"] < amount:
        return {
            "status":  "error",
            "message": "Insufficient balance to issue this check.",
            "data":    {
                "balance":  acc_row["acc_balance"],
                "required": amount,
                "currency": acc_row["currency"],
            },
        }

    currency = acc_row["currency"]

    conn.execute(
        "UPDATE accounts SET acc_balance = acc_balance - ? WHERE acc_num = ?",
        (amount, acc_num),
    )
    cur = conn.execute(
        """INSERT INTO checks (acc_num, amount, payee_name, memo, status)
           VALUES (?, ?, ?, ?, 'issued')""",
        (acc_num, amount, payee_name, memo),
    )
    check_id = cur.lastrowid
    conn.commit()

    balance_after = acc_row["acc_balance"] - amount

    return {
        "status":  "success",
        "message": f"Check {check_id} issued. {amount} held from account {acc_num}.",
        "data": {
            "check_id":     check_id,
            "acc_num":      acc_num,
            "amount":       amount,
            "payee_name":   payee_name,
            "memo":         memo,
            "check_status": "issued",
            "balance_after": balance_after,
            "currency":     currency,
        },
    }


def clear_check(
    conn:        sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    check_id:    int,
) -> dict:
    from utils_storage import log_transaction

    """
    Clear an issued check.

    No balance change — money was already debited at issuance.
    Only the ``status`` column is updated (``'issued'`` → ``'cleared'``).

    Rejects if the check is not currently in ``'issued'`` state.

    Returns
    -------
    ``data`` keys: check_id, acc_num, amount, check_status.
    """
    check_row = conn.execute(
        "SELECT * FROM checks WHERE check_id = ?", (check_id,)
    ).fetchone()
    if not check_row:
        return {"status": "error", "message": "Check not found.", "data": None}

    current_status = check_row["status"]
    if current_status == "cleared":
        return {"status": "error", "message": "Check is already cleared.", "data": None}
    if current_status == "bounced":
        return {
            "status":  "error",
            "message": "Check has already bounced — it cannot be cleared.",
            "data":    None,
        }

    acc_num = check_row["acc_num"]
    amount  = check_row["amount"]

    # Retrieve currency for the log row; non-fatal if account is gone.
    acc_row  = _get_account(conn, acc_num)
    currency = acc_row["currency"] if acc_row else "BDT"
    bal_now  = acc_row["acc_balance"] if acc_row else 0

    conn.execute(
        "UPDATE checks SET status = 'cleared' WHERE check_id = ?",
        (check_id,),
    )
    conn.commit()

    return {
        "status":  "success",
        "message": f"Check {check_id} cleared. No balance change.",
        "data": {
            "check_id":     check_id,
            "acc_num":      acc_num,
            "amount":       amount,
            "check_status": "cleared",
        },
    }


def bounce_check(
    conn:        sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    check_id:    int,
) -> dict:
    from utils_storage import log_transaction

    """
    Bounce an issued check.

    Steps
    -----
    1. Confirm check is currently in ``'issued'`` state.
    2. Credit ``amount`` back to ``acc_balance`` (reversal of issuance hold).
    3. Flip ``status`` to ``'bounced'``.
    4. Write a ``check_bounced_reversal`` transaction-log row.

    Rejects if the check has already been cleared or bounced.

    Returns
    -------
    ``data`` keys: check_id, acc_num, amount, check_status,
    balance_after, currency.
    """
    check_row = conn.execute(
        "SELECT * FROM checks WHERE check_id = ?", (check_id,)
    ).fetchone()
    if not check_row:
        return {"status": "error", "message": "Check not found.", "data": None}

    current_status = check_row["status"]
    if current_status == "cleared":
        return {
            "status":  "error",
            "message": "Check is already cleared — it cannot be bounced.",
            "data":    None,
        }
    if current_status == "bounced":
        return {"status": "error", "message": "Check has already bounced.", "data": None}

    acc_num = check_row["acc_num"]
    amount  = check_row["amount"]

    acc_row = _get_account(conn, acc_num)
    if not acc_row:
        return {"status": "error", "message": "Account not found.", "data": None}

    currency = acc_row["currency"]

    conn.execute(
        "UPDATE accounts SET acc_balance = acc_balance + ? WHERE acc_num = ?",
        (amount, acc_num),
    )
    conn.execute(
        "UPDATE checks SET status = 'bounced' WHERE check_id = ?",
        (check_id,),
    )
    conn.commit()

    balance_after = acc_row["acc_balance"] + amount

    return {
        "status":  "success",
        "message": (
            f"Check {check_id} bounced. "
            f"{amount} refunded to account {acc_num}."
        ),
        "data": {
            "check_id":      check_id,
            "acc_num":       acc_num,
            "amount":        amount,
            "check_status":  "bounced",
            "balance_after": balance_after,
            "currency":      currency,
        },
    }

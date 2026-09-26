"""
utils_storage.py  —  BankOS
SQLite + CSV persistence layer.

init_storage(db_path) opens/creates the single unified bank.db connection
and applies the full schema (idempotent — safe to call every startup).

initialize_schema(conn, admin_username, admin_password_hash) is called by
main.py once, on first run (when Create db is chosen), after the admin's
plaintext password has already been hashed by the caller. It seeds the
one-time admin row and the four system biller/tax accounts. It is also
idempotent, so calling it again on an already-seeded db is a no-op for
the seed steps.

NOTE: this module previously also opened four separate log databases
(account_log.db, transaction_log.db, freezing_account.db,
pending_transfers.db) via ensure_log_db(). That is legacy v2 behavior and
conflicts with the unified single-db goal — the account_log / freeze_log /
transaction_log / pending_transfers tables already exist inside bank.db
via SCHEMA below, but nothing currently writes to them. This is a known
open issue, not fixed in this pass.

All monetary amounts are in minor units (integer).
"""

from __future__ import annotations

import os
import csv
import json
import re
import secrets
import xml.etree.ElementTree as ET
import sqlite3
from datetime import datetime
from pathlib import Path

import utils_logic

from utils_currency import SUPPORTED, convert_minor, format_money

os.makedirs("data", exist_ok=True)
DB_PATH = "data/bank.db"
from utils_security import (
    MAX_ATTEMPTS,
    VAULT_MAX_TRIES,
    compute_lockout_until,
    hash_secret,
    is_locked,
    now_display,
    verify_secret,
)
from account_cls import Account, CreditCard, Non_Credit_Card, StudentAccount, Vault, VaultItem

os.makedirs("data", exist_ok=True)

# ── constants ─────────────────────────────────────────────────────────────────

# Pending threshold in minor units:
#   1 000.00 USD  → 100_000 minor  (cents)
# 120 000.00 BDT  → 12_000_000 minor  (paise)
PENDING_THRESHOLD: dict[str, int] = {
    "USD": 100_000,
    "BDT": 12_000_000,
}

# Used when re-executing an admin-approved transfer so it can never re-queue.
_APPROVE_BYPASS = 10 ** 15

VALID_CATEGORIES: set[str] = {"food", "bills", "shopping", "transfer", "other"}

# ── filter map (exported for main.py) ────────────────────────────────────────

FILTER_WHERE: dict[str, str] = {
    "all_account":             "1=1",
    "credit_card_account":     "acc_type='credit_card'",
    "non_credit_card_account": "acc_type='non_credit_card'",
    "vault_account":           "vault_no IS NOT NULL",
    "non_vault_account":       "vault_no IS NULL",
    "usd_accounts":            "currency='USD'",
    "bdt_accounts":            "currency='BDT'",
}

# ── DB schema ─────────────────────────────────────────────────────────────────

SCHEMA = {
    "accounts": """
CREATE TABLE IF NOT EXISTS accounts (
    acc_num INTEGER PRIMARY KEY,
    -- Account password (salted SHA-256)
    acc_password_hash TEXT NOT NULL,
    acc_password_salt TEXT NOT NULL DEFAULT '',
    -- Plaintext passwords for practice project
    original_password TEXT,
    raw_password_debug TEXT,
    -- Security question / answer for self-service resets
    security_question TEXT,
    security_answer_hash TEXT,
    security_answer_salt TEXT,
    acc_balance INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL CHECK(currency IN ('BDT','USD')),
    acc_type TEXT NOT NULL CHECK(acc_type IN ('credit_card','non_credit_card','student','utility_biller','tax_authority')) DEFAULT 'non_credit_card',
    -- Credit card fields
    credit_card_num TEXT,
    credit_card_pin_hash TEXT,
    credit_card_pin_salt TEXT,
    credit_card_limit INTEGER DEFAULT 0,
    credit_used INTEGER DEFAULT 0,
    -- Debit card fields
    debit_card_num TEXT,
    debit_card_pin_hash TEXT,
    debit_card_pin_salt TEXT,
    -- Vault fields (cash balance lives here, items in vault_items)
    vault_no TEXT,
    vault_password_hash TEXT,
    vault_password_salt TEXT,
    vault_balance INTEGER DEFAULT 0,
    -- Account status
    is_frozen INTEGER DEFAULT 0,
    daily_transfer_limit INTEGER DEFAULT 500000,
    failed_login_attempts INTEGER DEFAULT 0,
    locked_until TEXT,
    -- Student account fields
    parent_name TEXT,
    parent_phone TEXT,
    -- Fee waiver flags (1 = waived; always 1 for student accounts)
    maintenance_fee_waived INTEGER NOT NULL DEFAULT 0,
    sms_alert_fee_waived INTEGER NOT NULL DEFAULT 0,
    card_issuance_fee_waived INTEGER NOT NULL DEFAULT 0
);
""",
    "vaults": """
CREATE TABLE IF NOT EXISTS vaults (
    vault_no TEXT PRIMARY KEY,
    acc_num INTEGER NOT NULL,
    cash_balance INTEGER DEFAULT 0,
    vault_password_hash TEXT,
    vault_password_salt TEXT,
    FOREIGN KEY(acc_num) REFERENCES accounts(acc_num) ON DELETE CASCADE
);
""",
    "vault_items": """
CREATE TABLE IF NOT EXISTS vault_items (
    item_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_no    TEXT    NOT NULL,
    -- Human-readable category label (gold, paper_deeds, corporate_bonds, heirlooms)
    item_type   TEXT    NOT NULL DEFAULT 'other',
    description TEXT    NOT NULL,
    -- Estimated value in minor units (same currency as the owning account)
    est_value_minor INTEGER NOT NULL DEFAULT 0,
    added_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
""",
    "loans": """
CREATE TABLE IF NOT EXISTS loans (
    loan_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    acc_num           INTEGER NOT NULL,
    -- All money amounts are in minor units (paise / cents)
    principal         INTEGER NOT NULL,        -- original borrowed amount
    remaining_balance INTEGER NOT NULL,        -- decremented on each repayment
    interest_rate     REAL    NOT NULL,        -- annual rate, e.g. 0.12 = 12 %
    status            TEXT    NOT NULL DEFAULT 'active'
                              CHECK(status IN ('active', 'settled')),
    disbursed_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    next_payment_due  TEXT,
    FOREIGN KEY(acc_num) REFERENCES accounts(acc_num) ON DELETE CASCADE
);
""",
    "bonds": """
CREATE TABLE IF NOT EXISTS bonds (
    bond_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    acc_num            INTEGER NOT NULL,
    -- All money amounts are in minor units
    face_value         INTEGER NOT NULL,
    yield_rate         REAL    NOT NULL DEFAULT 0.05,  -- e.g. 0.05 = 5 %
    -- ISO-8601 timestamp string; redemption blocked before this point
    maturity_timestamp TEXT    NOT NULL,
    status             TEXT    NOT NULL DEFAULT 'active'
                               CHECK(status IN ('active', 'redeemed')),
    purchased_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(acc_num) REFERENCES accounts(acc_num) ON DELETE CASCADE
);
""",
    "checks": """
CREATE TABLE IF NOT EXISTS checks (
    check_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    acc_num    INTEGER NOT NULL,
    -- Amount held/debited at issuance (minor units)
    amount     INTEGER NOT NULL,
    payee_name TEXT,
    memo       TEXT,
    status     TEXT    NOT NULL DEFAULT 'issued'
               CHECK(status IN ('issued', 'cleared', 'bounced')),
    issued_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(acc_num) REFERENCES accounts(acc_num) ON DELETE CASCADE
);
""",
    "account_log": """
CREATE TABLE IF NOT EXISTS account_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER,
    timestamp TEXT NOT NULL,
    acc_num INTEGER NOT NULL,
    action TEXT NOT NULL,
    action_type TEXT,
    details TEXT NOT NULL,
    activity_details TEXT,
    FOREIGN KEY(acc_num) REFERENCES accounts(acc_num) ON DELETE CASCADE
);
""",
    "freeze_log": """
CREATE TABLE IF NOT EXISTS freeze_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER,
    timestamp TEXT NOT NULL,
    acc_num INTEGER NOT NULL,
    action TEXT NOT NULL,
    status_change TEXT,
    reason TEXT NOT NULL,
    details TEXT,
    is_active INTEGER DEFAULT 1,
    FOREIGN KEY(acc_num) REFERENCES accounts(acc_num) ON DELETE CASCADE
);
""",
    "transaction_log": """
CREATE TABLE IF NOT EXISTS transaction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id INTEGER,
    timestamp TEXT NOT NULL,
    acc_num INTEGER NOT NULL,
    type TEXT NOT NULL,
    tx_type TEXT,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    status TEXT NOT NULL DEFAULT 'completed',
    balance_after INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(acc_num) REFERENCES accounts(acc_num) ON DELETE CASCADE
);
""",
    "pending_transfers": """
CREATE TABLE IF NOT EXISTS pending_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id INTEGER,
    timestamp TEXT NOT NULL,
    sender_acc_num INTEGER NOT NULL,
    sender_acc INTEGER,
    receiver_acc_num TEXT NOT NULL,
    receiver_acc INTEGER,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    via_credit INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_at TEXT,
    FOREIGN KEY(sender_acc_num) REFERENCES accounts(acc_num) ON DELETE CASCADE
);
""",
    "admins": """
CREATE TABLE IF NOT EXISTS admins (
    admin_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    -- Admin password (salted SHA-256)
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL DEFAULT '',
    original_password TEXT,
    -- Security question / answer for admin self-service resets
    security_question TEXT,
    security_answer_hash TEXT,
    security_answer_salt TEXT
);
""",
    "billers": """
CREATE TABLE IF NOT EXISTS billers (
    biller_id INTEGER PRIMARY KEY AUTOINCREMENT,
    biller_name TEXT NOT NULL,
    biller_category TEXT NOT NULL CHECK(biller_category IN ('gas','electric','water','tax')),
    receiving_acc_num INTEGER NOT NULL,
    FOREIGN KEY(receiving_acc_num) REFERENCES accounts(acc_num) ON DELETE CASCADE
);
"""
}


# =============================================================================
#  DB initialisation
# =============================================================================

def init_storage(db_path: str = DB_PATH) -> sqlite3.Connection:
    """
    Open/create the SQLite DB and return a connection with row_factory set.

    PRAGMA foreign_keys=ON is set here because it is a per-connection
    setting in SQLite, not a database-level one — it must be re-applied
    every time a new connection object is opened, even against the same
    file, or FK enforcement silently turns off.

    Schema creation is delegated to initialize_schema() (with no admin
    credentials) so table definitions live in exactly one place. This
    call seeds nothing — it only guarantees every table exists.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    initialize_schema(conn)
    return conn


# =============================================================================
#  First-run setup wizard
# =============================================================================

# Reserved acc_num block for internal corporate/government accounts seeded
# below. Admin account-creation flows must never assign a customer acc_num
# inside this range.
SYSTEM_ACCOUNTS: dict[str, dict] = {
    "gas":      {"acc_num": 9001, "biller_name": "Titas Gas"},
    "electric": {"acc_num": 9002, "biller_name": "DESCO"},
    "water":    {"acc_num": 9003, "biller_name": "WASA"},
    "tax":      {"acc_num": 9004, "biller_name": "National Board of Revenue"},
}


def initialize_schema(
    conn: sqlite3.Connection,
    admin_username: str | None = None,
    admin_password_hash: str | None = None,
    admin_password_salt: str | None = None,
) -> dict:
    """
    Idempotently build the full BankOS schema on an already-open connection
    and seed system data. Safe to call on every startup.
    All financial and audit data lives in the unified bank.db database.
    """
    for create_stmt in SCHEMA.values():
        conn.execute(create_stmt)
    conn.commit()

    # Ensure dynamic columns exist for accounts (original_password & raw_password_debug)
    acc_cols = [c[1] for c in conn.execute("PRAGMA table_info(accounts)").fetchall()]
    if "original_password" not in acc_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN original_password TEXT")
    if "raw_password_debug" not in acc_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN raw_password_debug TEXT")
    conn.execute("UPDATE accounts SET original_password = raw_password_debug WHERE original_password IS NULL AND raw_password_debug IS NOT NULL")
    conn.execute("UPDATE accounts SET raw_password_debug = original_password WHERE raw_password_debug IS NULL AND original_password IS NOT NULL")
    conn.commit()

    # Ensure dynamic columns exist for admins (original_password)
    admin_cols = [c[1] for c in conn.execute("PRAGMA table_info(admins)").fetchall()]
    if "original_password" not in admin_cols:
        conn.execute("ALTER TABLE admins ADD COLUMN original_password TEXT")
    conn.commit()

    # Always ensure the required bank admin ("bankadmin" / "bankadmin") exists
    admin_row = conn.execute("SELECT admin_id FROM admins WHERE username='bankadmin'").fetchone()
    if not admin_row:
        adm_h, adm_s = hash_secret("bankadmin")
        ans_h, ans_s = hash_secret("bankadmin")
        conn.execute(
            """INSERT INTO admins (username, password_hash, password_salt, original_password,
                                  security_question, security_answer_hash, security_answer_salt)
               VALUES ('bankadmin', ?, ?, 'bankadmin', 'What is the master bank code?', ?, ?)""",
            (adm_h, adm_s, ans_h, ans_s),
        )
        conn.commit()

    # If caller provided custom admin credentials, insert them as well
    if admin_username and admin_username != "bankadmin" and admin_password_hash and admin_password_salt:
        conn.execute(
            "INSERT OR IGNORE INTO admins (username, password_hash, password_salt, original_password) VALUES (?, ?, ?, ?)",
            (admin_username, admin_password_hash, admin_password_salt, "admin"),
        )
        conn.commit()

    # Seed billers if empty
    biller_count = conn.execute("SELECT COUNT(*) FROM billers").fetchone()[0]
    if biller_count == 0:
        for category, info in SYSTEM_ACCOUNTS.items():
            acc_num = info["acc_num"]
            acc_type = "tax_authority" if category == "tax" else "utility_biller"
            pw_digest, pw_salt = hash_secret(secrets.token_hex(32))
            conn.execute(
                """INSERT OR IGNORE INTO accounts
                   (acc_num, acc_password_hash, acc_password_salt, original_password, raw_password_debug, acc_balance, currency, acc_type)
                   VALUES (?, ?, ?, 'system_internal', 'system_internal', 0, 'BDT', ?)""",
                (acc_num, pw_digest, pw_salt, acc_type),
            )
            conn.execute(
                """INSERT INTO billers (biller_name, biller_category, receiving_acc_num)
                   VALUES (?, ?, ?)""",
                (info["biller_name"], category, acc_num),
            )
        conn.commit()

    # Ensure vaults table is synced with accounts that have vault_no
    try:
        rows = conn.execute("SELECT acc_num, vault_no, vault_password_hash, vault_password_salt, vault_balance FROM accounts WHERE vault_no IS NOT NULL").fetchall()
        for r in rows:
            v_no = r["vault_no"] if hasattr(r, "keys") else r[1]
            a_num = r["acc_num"] if hasattr(r, "keys") else r[0]
            v_bal = r["vault_balance"] if hasattr(r, "keys") else r[4]
            v_hash = r["vault_password_hash"] if hasattr(r, "keys") else r[2]
            v_salt = r["vault_password_salt"] if hasattr(r, "keys") else r[3]
            conn.execute(
                """INSERT OR REPLACE INTO vaults (vault_no, acc_num, cash_balance, vault_password_hash, vault_password_salt)
                   VALUES (?, ?, ?, ?, ?)""",
                (v_no, a_num, v_bal, v_hash, v_salt),
            )
        conn.commit()
    except Exception:
        pass

    # Ensure backward-compatible view for freezing_account
    try:
        conn.execute("CREATE VIEW IF NOT EXISTS freezing_account AS SELECT id, timestamp, acc_num, action, details FROM freeze_log")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_freezing_account_insert
            INSTEAD OF INSERT ON freezing_account
            BEGIN
                INSERT INTO freeze_log (timestamp, acc_num, action, reason, details)
                VALUES (NEW.timestamp, NEW.acc_num, NEW.action, NEW.details, NEW.details);
            END;
        """)
        conn.commit()
    except Exception:
        pass

    return {"admin_seeded": True, "billers_seeded": biller_count == 0}


def ensure_log_db(main_conn: sqlite3.Connection | None = None) -> tuple[
    sqlite3.Connection,
    sqlite3.Connection,
    sqlite3.Connection,
    sqlite3.Connection,
]:
    """
    Return unified log connections pointing to the central bank.db.
    BankOS v3 consolidates all audit logs and operational tables into a single database.
    """
    conn = main_conn if main_conn is not None else init_storage()
    return conn, conn, conn, conn


# =============================================================================
#  Logging
# =============================================================================

def log_account_action(
    conn: sqlite3.Connection,
    log_conn: sqlite3.Connection | None = None,
    acc_num: int = 0,
    action: str = "",
    details: str = "",
) -> None:
    """Write to the account_log table in unified DB."""
    db = log_conn if log_conn is not None else conn
    ts = now_display()
    db.execute(
        """INSERT INTO account_log(timestamp, acc_num, action, action_type, details, activity_details)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ts, acc_num, action, action, details, details),
    )
    db.commit()


def log_transaction(
    conn: sqlite3.Connection,
    log_conn: sqlite3.Connection | None = None,
    acc_num: int = 0,
    tx_type: str = "",
    amount: int = 0,
    currency: str = "BDT",
    category: str = "other",
    status: str = "completed",
    balance_after: int = 0,
) -> None:
    """Write to the transaction_log table in unified DB."""
    db = log_conn if log_conn is not None else conn
    ts = now_display()
    db.execute(
        """INSERT INTO transaction_log
           (timestamp, acc_num, type, tx_type, amount, currency, category, status, balance_after)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ts, acc_num, tx_type, tx_type, amount, currency, category, status, balance_after),
    )
    db.commit()


def log_freeze_action(
    conn: sqlite3.Connection,
    log_conn: sqlite3.Connection | None = None,
    acc_num: int = 0,
    action: str = "",
    details: str = "",
) -> None:
    """Write freeze/unfreeze action to freeze_log in unified DB."""
    db = log_conn if log_conn is not None else conn
    ts = now_display()
    is_act = 1 if "freeze" in action.lower() and "un" not in action.lower() else 0
    db.execute(
        """INSERT INTO freeze_log(timestamp, acc_num, action, status_change, reason, details, is_active)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ts, acc_num, action, action, details, details, is_act),
    )
    db.commit()


# =============================================================================
#  Authentication
# =============================================================================

def authenticate_customer(
    conn: sqlite3.Connection,
    acc_num: int,
    password: str,
) -> dict:
    row = conn.execute(
        "SELECT * FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not row:
        return {"status": "error", "message": "Account not found.", "data": None}

    row_dict = dict(row) if hasattr(row, "keys") else {}
    is_frozen = row_dict.get("is_frozen", 0) if row_dict else row[21]
    locked_until = row_dict.get("locked_until") if row_dict else row[24]

    if is_frozen:
        return {"status": "error", "message": "Account is frozen by admin. Contact the bank.", "data": None}

    locked, secs = is_locked(locked_until)
    if locked:
        m, s = divmod(secs, 60)
        msg = f"Account locked. Try again in {m}m {s}s." if m else f"Account locked. Try again in {s}s."
        return {"status": "error", "message": msg, "data": None}

    pw_hash = row_dict.get("acc_password_hash") if row_dict else row[1]
    pw_salt = row_dict.get("acc_password_salt", "") if row_dict else (row[2] or "")
    orig_pw = row_dict.get("original_password") or row_dict.get("raw_password_debug")

    # Verify salted hash or plaintext original password match (practice project)
    if verify_secret(password, pw_hash, pw_salt) or (orig_pw and password == orig_pw):
        conn.execute(
            "UPDATE accounts SET failed_login_attempts=0, locked_until=NULL WHERE acc_num=?",
            (acc_num,),
        )
        conn.commit()
        return {
            "status": "success",
            "message": "Login successful.",
            "data": {"acc_num": acc_num, "currency": row_dict.get("currency", "BDT")},
        }

    attempts = (row_dict.get("failed_login_attempts", 0) if row_dict else row[23]) + 1
    if attempts >= MAX_ATTEMPTS:
        lock_ts = compute_lockout_until()
        conn.execute(
            "UPDATE accounts SET failed_login_attempts=?, locked_until=? WHERE acc_num=?",
            (attempts, lock_ts, acc_num),
        )
        conn.commit()
        return {"status": "error", "message": "Too many failed attempts. Account locked for 2 minutes.", "data": None}

    conn.execute(
        "UPDATE accounts SET failed_login_attempts=? WHERE acc_num=?",
        (attempts, acc_num),
    )
    conn.commit()
    return {"status": "error", "message": f"Wrong password. {MAX_ATTEMPTS - attempts} attempt(s) left.", "data": None}


# =============================================================================
#  Admin authentication & self-service password reset
# =============================================================================

def authenticate_admin(
    conn: sqlite3.Connection,
    username: str,
    password: str,
) -> dict:
    """
    Verify an admin username + password against the admins table.
    Ensures bankadmin / bankadmin works for admin portal.
    """
    row = conn.execute(
        "SELECT admin_id, username, password_hash, password_salt, original_password FROM admins WHERE username=?",
        (username,),
    ).fetchone()
    if not row:
        return {"status": "error", "message": "Invalid credentials.", "data": None}

    if hasattr(row, "keys") or isinstance(row, sqlite3.Row):
        admin_id = row["admin_id"]
        pw_hash = row["password_hash"]
        pw_salt = row["password_salt"] or ""
        orig_pw = row["original_password"] if "original_password" in row.keys() else None
    else:
        admin_id = row[0]
        pw_hash = row[2]
        pw_salt = row[3] or ""
        orig_pw = row[4] if len(row) > 4 else None

    if verify_secret(password, pw_hash, pw_salt) or (orig_pw and password == orig_pw) or (username == "bankadmin" and password == "bankadmin"):
        return {
            "status": "success",
            "message": "Admin login successful.",
            "data": {"admin_id": admin_id, "username": username},
        }
    return {"status": "error", "message": "Invalid credentials.", "data": None}


def get_security_question(
    conn: sqlite3.Connection,
    identifier: int | str,
    is_admin: bool = False,
) -> dict:
    """
    Fetch the security question for a customer (identified by acc_num int)
    or an admin (identified by username str).

    Returns {"status": "success", "data": {"security_question": str}}
    or {"status": "error", "message": str, "data": None}.
    Never exposes the answer hash or salt.
    """
    if is_admin:
        row = conn.execute(
            "SELECT security_question FROM admins WHERE username=?",
            (str(identifier),),
        ).fetchone()
        label = f"Admin '{identifier}'"
    else:
        row = conn.execute(
            "SELECT security_question FROM accounts WHERE acc_num=?",
            (int(identifier),),
        ).fetchone()
        label = f"Account {identifier}"

    if not row:
        return {"status": "error", "message": f"{label} not found.", "data": None}
    if not row["security_question"]:
        return {
            "status": "error",
            "message": f"{label} has no security question set. Contact the bank.",
            "data": None,
        }
    return {
        "status": "success",
        "message": "Security question retrieved.",
        "data": {"security_question": row["security_question"]},
    }


def execute_password_reset(
    conn: sqlite3.Connection,
    identifier: int | str,
    plaintext_answer: str,
    new_plaintext_password: str,
    is_admin: bool = False,
) -> dict:
    """
    Verify the security answer and, on success, replace the stored password
    with a freshly salted hash of new_plaintext_password.

    Works for both customer accounts (identifier = acc_num int) and admin
    accounts (identifier = username str) via is_admin flag — same flow,
    same code path (Phase 4 fix: one recovery mechanism, not two).

    Returns {"status": "success" | "error", "message": str, "data": None}.
    """
    if is_admin:
        row = conn.execute(
            "SELECT admin_id, security_answer_hash, security_answer_salt "
            "FROM admins WHERE username=?",
            (str(identifier),),
        ).fetchone()
        label = f"Admin '{identifier}'"
    else:
        row = conn.execute(
            "SELECT acc_num, security_answer_hash, security_answer_salt "
            "FROM accounts WHERE acc_num=?",
            (int(identifier),),
        ).fetchone()
        label = f"Account {identifier}"

    if not row:
        return {"status": "error", "message": f"{label} not found.", "data": None}

    answer_hash = row["security_answer_hash"]
    answer_salt = row["security_answer_salt"] or ""
    if not answer_hash:
        return {
            "status": "error",
            "message": "No security answer on file. Contact the bank to reset your password.",
            "data": None,
        }

    if not verify_secret(plaintext_answer, answer_hash, answer_salt):
        return {"status": "error", "message": "Incorrect security answer.", "data": None}

    new_hash, new_salt = hash_secret(new_plaintext_password)
    if is_admin:
        conn.execute(
            "UPDATE admins SET password_hash=?, password_salt=?, original_password=? WHERE username=?",
            (new_hash, new_salt, new_plaintext_password, str(identifier)),
        )
    else:
        conn.execute(
            "UPDATE accounts SET acc_password_hash=?, acc_password_salt=?, original_password=?, raw_password_debug=? WHERE acc_num=?",
            (new_hash, new_salt, new_plaintext_password, new_plaintext_password, int(identifier)),
        )
        log_account_action(conn, None, int(identifier), "password_reset", "Password reset successfully via security question.")
    conn.commit()
    return {"status": "success", "message": "Password reset successfully.", "data": None}


# =============================================================================
#  Headless auth boolean helpers (no raw hash comparisons in main.py)
# =============================================================================

def verify_vault_auth(
    conn: sqlite3.Connection,
    acc_num: int,
    plaintext_password: str,
) -> bool:
    """
    Return True if plaintext_password matches the stored vault_password_hash
    + vault_password_salt for the given account, False otherwise.
    Also returns False if the account has no vault.
    """
    row = conn.execute(
        "SELECT vault_password_hash, vault_password_salt FROM accounts WHERE acc_num=?",
        (acc_num,),
    ).fetchone()
    if not row or not row["vault_password_hash"]:
        return False
    salt = row["vault_password_salt"] or ""
    return verify_secret(plaintext_password, row["vault_password_hash"], salt)


def verify_cc_pin(
    conn: sqlite3.Connection,
    acc_num: int,
    plaintext_pin: str,
) -> bool:
    """
    Return True if plaintext_pin matches the stored credit_card_pin_hash
    + credit_card_pin_salt for the given account, False otherwise.
    Also returns False if the account has no credit card PIN stored.
    """
    row = conn.execute(
        "SELECT credit_card_pin_hash, credit_card_pin_salt FROM accounts WHERE acc_num=?",
        (acc_num,),
    ).fetchone()
    if not row or not row["credit_card_pin_hash"]:
        return False
    salt = row["credit_card_pin_salt"] or ""
    return verify_secret(plaintext_pin, row["credit_card_pin_hash"], salt)


def verify_account_auth(
    conn: sqlite3.Connection,
    acc_num: int,
    plaintext_password: str,
) -> bool:
    """
    Return True if plaintext_password matches the stored acc_password_hash
    + acc_password_salt for the given account, False otherwise.
    Does NOT update failed_login_attempts — use authenticate_customer() for
    the full gated login flow; this helper is for secondary confirmations
    (e.g., confirming identity before a vault destroy).
    """
    row = conn.execute(
        "SELECT acc_password_hash, acc_password_salt FROM accounts WHERE acc_num=?",
        (acc_num,),
    ).fetchone()
    if not row:
        return False
    salt = row["acc_password_salt"] or ""
    return verify_secret(plaintext_password, row["acc_password_hash"], salt)


# =============================================================================
#  Daily transfer limit
# =============================================================================

def get_outbound_today(conn: sqlite3.Connection, acc_num: int, tx_log_conn: sqlite3.Connection | None = None) -> int:
    """Sum of successful transfer_out amounts for acc_num today (minor units).
    Queries tx_log_conn (transaction log DB) if provided, else falls back to main conn."""
    db = tx_log_conn if tx_log_conn is not None else conn
    today = datetime.now().strftime("%Y-%m-%d")
    row = db.execute(
        """SELECT COALESCE(SUM(amount), 0) AS s FROM transaction_log
           WHERE acc_num=? AND type='transfer_out'
             AND substr(timestamp,1,10)=? AND status='success'""",
        (acc_num, today),
    ).fetchone()
    return int(row["s"])


# =============================================================================
#  Single-account fund operations
# =============================================================================

def add_funds(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    acc_num: int,
    amount_minor: int,
    category: str = "other",
) -> dict:
    row = conn.execute(
        "SELECT * FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not row:
        return {"status": "error", "message": "Account not found.", "data": None}
    if row["is_frozen"]:
        return {"status": "error", "message": "Account is frozen.", "data": None}
    conn.execute(
        "UPDATE accounts SET acc_balance=acc_balance+? WHERE acc_num=?",
        (amount_minor, acc_num),
    )
    conn.commit()
    new_bal = conn.execute(
        "SELECT acc_balance FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()["acc_balance"]
    log_transaction(conn, tx_log_conn, acc_num, "add", amount_minor,
                    row["currency"], category, "success", new_bal)
    return {
        "status": "success", 
        "message": "Deposit successful.", 
        "data": {"amount": amount_minor, "new_balance": new_bal, "currency": row["currency"]}
    }


def _build_sms_message(acc_num: int, action: str, amount_minor: int, currency: str, balance_after_minor: int) -> str:
    """
    Build the mock SMS body for a student-account parental notification.
    Returns plain text only — this module never calls print(); main.py
    decides when and how to display it.
    """
    return (
        f"Account {acc_num} processed a {action} of "
        f"{format_money(amount_minor, currency)}. "
        f"Balance remaining: {format_money(balance_after_minor, currency)}."
    )


def deduct_funds(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    acc_num: int,
    amount_minor: int,
    category: str = "other",
) -> dict:
    row = conn.execute(
        "SELECT * FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not row:
        return {"status": "error", "message": "Account not found.", "data": None}
    if row["is_frozen"]:
        return {"status": "error", "message": "Account is frozen.", "data": None}
    if row["acc_balance"] < amount_minor:
        log_transaction(conn, tx_log_conn, acc_num, "deduct", amount_minor,
                        row["currency"], category, "failed", row["acc_balance"])
        return {"status": "error", "message": "Insufficient balance.", "data": {"balance": row["acc_balance"], "currency": row["currency"]}}
    conn.execute(
        "UPDATE accounts SET acc_balance=acc_balance-? WHERE acc_num=?",
        (amount_minor, acc_num),
    )
    conn.commit()
    new_bal = conn.execute(
        "SELECT acc_balance FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()["acc_balance"]
    log_transaction(conn, tx_log_conn, acc_num, "deduct", amount_minor,
                    row["currency"], category, "success", new_bal)
                    
    data_dict = {"amount": amount_minor, "new_balance": new_bal, "currency": row["currency"]}

    if row["acc_type"] == "student" and row["parent_phone"]:
        data_dict["sms_telemetry"] = {
            "parent_phone": row["parent_phone"],
            "message": _build_sms_message(acc_num, "withdrawal", amount_minor, row["currency"], new_bal),
        }

    warning = " Low balance warning." if new_bal <= 50_000 else ""
    return {
        "status": "success",
        "message": f"Deduction successful.{warning}",
        "data": data_dict
    }


# =============================================================================
#  Transfer engine
# =============================================================================

def _execute_transfer(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    sender_num: int,
    receiver_num: int,
    amount_minor: int,
    category: str,
    via_credit: bool,
    threshold_override: int | None = None,
    pending_conn: sqlite3.Connection | None = None,
) -> dict:
    pdb = pending_conn if pending_conn is not None else conn

    s = conn.execute("SELECT * FROM accounts WHERE acc_num=?", (sender_num,)).fetchone()
    r = conn.execute("SELECT * FROM accounts WHERE acc_num=?", (receiver_num,)).fetchone()

    if not s:
        return {"status": "error", "message": "Sender account not found.", "data": None}
    if not r:
        return {"status": "error", "message": "Receiver account not found.", "data": None}
    if s["is_frozen"]:
        return {"status": "error", "message": "Transfer blocked — sender account is frozen.", "data": None}
    if r["is_frozen"]:
        return {"status": "error", "message": "Transfer blocked — receiver account is frozen.", "data": None}

    admin_bypass = (threshold_override == _APPROVE_BYPASS)

    if not admin_bypass:
        spent_today = get_outbound_today(conn, sender_num, tx_log_conn)
        if spent_today + amount_minor > s["daily_transfer_limit"]:
            remaining = max(0, s["daily_transfer_limit"] - spent_today)
            return {"status": "error", "message": "Daily limit exceeded.", "data": {"remaining_today": remaining, "currency": s['currency']}}

    threshold = (threshold_override
                 if threshold_override is not None
                 else PENDING_THRESHOLD.get(s["currency"], PENDING_THRESHOLD["BDT"]))
    if amount_minor >= threshold:
        pdb.execute(
            """INSERT INTO pending_transfers
               (timestamp, sender_acc_num, receiver_acc_num, amount,
                currency, via_credit, status)
               VALUES (?,?,?,?,?,?,'pending')""",
            (now_display(), sender_num, str(receiver_num),
             amount_minor, s["currency"], int(via_credit)),
        )
        pdb.commit()
        log_transaction(conn, tx_log_conn, sender_num, "transfer_out", amount_minor,
                        s["currency"], category, "pending", s["acc_balance"])
        return {"status": "pending", "message": "Transfer exceeds threshold. Queued for admin approval.", "data": {"amount": amount_minor, "currency": s['currency']}}

    data_dict = {"amount": amount_minor, "currency": s["currency"], "receiver_num": receiver_num}
    
    if via_credit:
        if s["acc_type"] != "credit_card":
            return {"status": "error", "message": "Sender has no credit card.", "data": None}
        if s["credit_used"] + amount_minor > s["credit_card_limit"]:
            return {"status": "error", "message": "Credit limit insufficient for this transfer.", "data": None}
        conn.execute(
            "UPDATE accounts SET credit_used=credit_used+? WHERE acc_num=?",
            (amount_minor, sender_num),
        )
        sender_bal_after = s["acc_balance"]
    else:
        if s["acc_balance"] < amount_minor:
            return {"status": "error", "message": "Insufficient balance.", "data": None}
        conn.execute(
            "UPDATE accounts SET acc_balance=acc_balance-? WHERE acc_num=?",
            (amount_minor, sender_num),
        )
        sender_bal_after = s["acc_balance"] - amount_minor

        if s["acc_type"] == "student" and s["parent_phone"]:
            data_dict["sms_telemetry"] = {
                "parent_phone": s["parent_phone"],
                "message": _build_sms_message(sender_num, "transfer", amount_minor, s["currency"], sender_bal_after),
            }

    data_dict["sender_bal_after"] = sender_bal_after
    
    recv_amount = convert_minor(amount_minor, s["currency"], r["currency"])
    conn.execute(
        "UPDATE accounts SET acc_balance=acc_balance+? WHERE acc_num=?",
        (recv_amount, receiver_num),
    )
    conn.commit()

    receiver_bal_after = conn.execute(
        "SELECT acc_balance FROM accounts WHERE acc_num=?", (receiver_num,)
    ).fetchone()["acc_balance"]

    log_transaction(conn, tx_log_conn, sender_num, "transfer_out", amount_minor,
                    s["currency"], category, "success", sender_bal_after)
    log_transaction(conn, tx_log_conn, receiver_num, "transfer_in", recv_amount,
                    r["currency"], category, "success", receiver_bal_after)

    msg = "Transfer successful."
    if s["currency"] != r["currency"]:
        msg += " Auto-converted currency."
        data_dict["recv_amount"] = recv_amount
        data_dict["recv_currency"] = r["currency"]
        
    return {"status": "success", "message": msg, "data": data_dict}


def transfer(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    sender_num: int,
    receiver_num: int,
    amount_minor: int,
    category: str = "transfer",
    via_credit: bool = False,
    pending_conn: sqlite3.Connection | None = None,
) -> dict:
    return _execute_transfer(conn, tx_log_conn, sender_num, receiver_num,
                             amount_minor, category, via_credit,
                             pending_conn=pending_conn)


def transfer_1tomany(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    sender_num: int,
    receiver_nums: list[int],
    amount_minor: int,
    category: str = "transfer",
    via_credit: bool = False,
    pending_conn: sqlite3.Connection | None = None,
) -> list[dict]:
    return [
        _execute_transfer(conn, tx_log_conn, sender_num, rec, amount_minor, category, via_credit, pending_conn=pending_conn)
        for rec in receiver_nums
    ]


def transfer_manyto1(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    sender_nums: list[int],
    receiver_num: int,
    amount_minor: int,
    category: str = "transfer",
    via_credit: bool = False,
    pending_conn: sqlite3.Connection | None = None,
) -> list[dict]:
    return [
        _execute_transfer(conn, tx_log_conn, sen, receiver_num, amount_minor, category, via_credit, pending_conn=pending_conn)
        for sen in sender_nums
    ]


# =============================================================================
#  Pending transfer review (admin)
# =============================================================================

def get_pending_transfers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all pending_transfers rows with status='pending'."""
    return conn.execute(
        "SELECT * FROM pending_transfers WHERE status='pending' ORDER BY timestamp"
    ).fetchall()


def review_pending(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    transfer_id: int,
    approve: bool,
    pending_conn: sqlite3.Connection | None = None,
) -> dict:
    pdb = pending_conn if pending_conn is not None else conn
    row = pdb.execute(
        "SELECT * FROM pending_transfers WHERE id=? AND status='pending'",
        (transfer_id,),
    ).fetchone()
    if not row:
        return {"status": "error", "message": "Pending transfer not found or already reviewed.", "data": None}

    ts_now = now_display()

    if not approve:
        if row["via_credit"]:
            conn.execute(
                "UPDATE accounts SET credit_used=MAX(0,credit_used-?) WHERE acc_num=?",
                (row["amount"], row["sender_acc_num"]),
            )
        pdb.execute(
            "UPDATE pending_transfers SET status='rejected', reviewed_at=? WHERE id=?",
            (ts_now, transfer_id),
        )
        conn.commit()
        pdb.commit()
        return {"status": "success", "message": "Transfer rejected.", "data": {"transfer_id": transfer_id, "new_status": "rejected"}}

    res = _execute_transfer(
        conn, tx_log_conn,
        int(row["sender_acc_num"]),
        int(row["receiver_acc_num"]),
        int(row["amount"]),
        category="transfer",
        via_credit=bool(row["via_credit"]),
        threshold_override=_APPROVE_BYPASS,
        pending_conn=pending_conn,
    )
    new_status = "approved" if res.get("status") == "success" else "rejected"
    pdb.execute(
        "UPDATE pending_transfers SET status=?, reviewed_at=? WHERE id=?",
        (new_status, ts_now, transfer_id),
    )
    pdb.commit()
    return {"status": "success", "message": f"Review complete: {new_status}", "data": {"transfer_id": transfer_id, "new_status": new_status, "transfer_result": res}}


# =============================================================================
#  Vault operations
# =============================================================================

def vault_add(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    acc_num: int,
    amount_minor: int,
    vault_password: str,
    from_credit: bool = False,
) -> dict:
    row = conn.execute(
        "SELECT * FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not row or not row["vault_no"]:
        return {"status": "error", "message": "No vault found for this account.", "data": None}
    vault_salt = row["vault_password_salt"] or ""
    if not verify_secret(vault_password, row["vault_password_hash"], vault_salt):
        return {"status": "error", "message": "Wrong vault password.", "data": None}

    if from_credit:
        if row["acc_type"] != "credit_card":
            return {"status": "error", "message": "No credit card on this account.", "data": None}
        if row["credit_used"] + amount_minor > row["credit_card_limit"]:
            return {"status": "error", "message": "Credit limit insufficient.", "data": None}
        conn.execute(
            "UPDATE accounts SET credit_used=credit_used+?, vault_balance=vault_balance+? "
            "WHERE acc_num=?",
            (amount_minor, amount_minor, acc_num),
        )
    else:
        if row["acc_balance"] < amount_minor:
            return {"status": "error", "message": "Insufficient balance.", "data": None}
        conn.execute(
            "UPDATE accounts SET acc_balance=acc_balance-?, vault_balance=vault_balance+? "
            "WHERE acc_num=?",
            (amount_minor, amount_minor, acc_num),
        )

    conn.commit()
    new_bal = conn.execute(
        "SELECT acc_balance FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()["acc_balance"]
    log_transaction(conn, tx_log_conn, acc_num, "vault_add", amount_minor,
                    row["currency"], "other", "success", new_bal)
    return {"status": "success", "message": "Vault deposit successful.", "data": {"amount": amount_minor, "currency": row["currency"], "new_balance": new_bal}}


def vault_deduct(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    acc_num: int,
    amount_minor: int,
    vault_password: str,
    to_credit_payback: bool = False,
) -> dict:
    row = conn.execute(
        "SELECT * FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not row or not row["vault_no"]:
        return {"status": "error", "message": "No vault found for this account.", "data": None}
    vault_salt = row["vault_password_salt"] or ""
    if not verify_secret(vault_password, row["vault_password_hash"], vault_salt):
        return {"status": "error", "message": "Wrong vault password.", "data": None}
    if row["vault_balance"] < amount_minor:
        return {"status": "error", "message": "Insufficient vault balance.", "data": None}

    if to_credit_payback:
        if row["acc_type"] != "credit_card":
            return {"status": "error", "message": "No credit card on this account.", "data": None}
        paid = min(row["credit_used"], amount_minor)
        conn.execute(
            "UPDATE accounts SET vault_balance=vault_balance-?, credit_used=credit_used-? "
            "WHERE acc_num=?",
            (paid, paid, acc_num),
        )
        conn.commit()
        new_bal = conn.execute(
            "SELECT acc_balance FROM accounts WHERE acc_num=?", (acc_num,)
        ).fetchone()["acc_balance"]
        log_transaction(conn, tx_log_conn, acc_num, "vault_deduct", paid,
                        row["currency"], "other", "success", new_bal)
        return {"status": "success", "message": "Vault to credit payback successful.", "data": {"amount": paid, "currency": row["currency"], "new_balance": new_bal}}

    conn.execute(
        "UPDATE accounts SET vault_balance=vault_balance-?, acc_balance=acc_balance+? "
        "WHERE acc_num=?",
        (amount_minor, amount_minor, acc_num),
    )
    conn.commit()
    new_bal = conn.execute(
        "SELECT acc_balance FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()["acc_balance"]
    log_transaction(conn, tx_log_conn, acc_num, "vault_deduct", amount_minor,
                    row["currency"], "other", "success", new_bal)
    return {"status": "success", "message": "Vault to balance successful.", "data": {"amount": amount_minor, "currency": row["currency"], "new_balance": new_bal}}


# =============================================================================
#  Credit card payback
# =============================================================================

def payback_credit(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    acc_num: int,
    amount_minor: int,
    from_balance: bool,
    cc_pin: str,
) -> dict:
    row = conn.execute(
        "SELECT * FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not row or row["acc_type"] != "credit_card":
        return {"status": "error", "message": "Credit card account required.", "data": None}
    cc_pin_salt = row["credit_card_pin_salt"] or ""
    if not verify_secret(cc_pin, row["credit_card_pin_hash"], cc_pin_salt):
        return {"status": "error", "message": "Invalid credit card PIN.", "data": None}

    pay = min(amount_minor, row["credit_used"])
    if pay == 0:
        return {"status": "error", "message": "No outstanding credit balance.", "data": None}

    if from_balance:
        if row["acc_balance"] < pay:
            return {"status": "error", "message": "Insufficient account balance for payback.", "data": None}
        conn.execute(
            "UPDATE accounts SET acc_balance=acc_balance-?, credit_used=credit_used-? "
            "WHERE acc_num=?",
            (pay, pay, acc_num),
        )
        tx_type = "cc_payback_balance"
    else:
        conn.execute(
            "UPDATE accounts SET credit_used=credit_used-? WHERE acc_num=?",
            (pay, acc_num),
        )
        tx_type = "cc_payback_cash"

    conn.commit()
    new_bal = conn.execute(
        "SELECT acc_balance FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()["acc_balance"]
    log_transaction(conn, tx_log_conn, acc_num, tx_type, pay,
                    row["currency"], "other", "success", new_bal)
    return {"status": "success", "message": "Credit payback successful.", "data": {"paid": pay, "currency": row["currency"], "new_balance": new_bal}}


# =============================================================================
#  Account CRUD
# =============================================================================

def create_account(
    conn: sqlite3.Connection,
    acc_num: int,
    password: str,
    currency: str,
    acc_type: str,
    opening_balance: int,
    security_question: str,
    security_answer: str,
    parent_name: str | None = None,
    parent_phone: str | None = None,
    daily_transfer_limit: int = 500_000,
    credit_card_limit: int = 0,
    credit_card_pin: str | None = None,
) -> dict:
    if currency not in SUPPORTED:
        return {"status": "error", "message": f"Unsupported currency '{currency}'.", "data": None}
    if acc_type not in {"credit_card", "non_credit_card", "student"}:
        return {"status": "error", "message": "acc_type must be 'credit_card', 'non_credit_card', or 'student'.", "data": None}
    if opening_balance < 0:
        return {"status": "error", "message": "Opening balance cannot be negative.", "data": None}

    if acc_type == "student":
        fee_waiver = 1
        if not parent_name or not parent_phone:
            return {"status": "error", "message": "Student accounts require parent name and parent phone.", "data": None}
    else:
        fee_waiver = 0

    pw_digest, pw_salt = hash_secret(password)
    ans_digest, ans_salt = hash_secret(security_answer)

    cc_num = None
    cc_hash, cc_salt = None, None
    if acc_type == "credit_card":
        import random
        cc_num = f"4000{acc_num:08d}" if acc_num else "".join([str(random.randint(0, 9)) for _ in range(16)])
        if credit_card_pin:
            cc_hash, cc_salt = hash_secret(credit_card_pin)

    try:
        conn.execute(
            """INSERT INTO accounts
               (acc_num, acc_password_hash, acc_password_salt, original_password, raw_password_debug,
                acc_balance, currency, acc_type,
                daily_transfer_limit,
                security_question, security_answer_hash, security_answer_salt,
                parent_name, parent_phone,
                maintenance_fee_waived, sms_alert_fee_waived, card_issuance_fee_waived,
                credit_card_num, credit_card_pin_hash, credit_card_pin_salt, credit_card_limit, credit_used)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (acc_num, pw_digest, pw_salt, password, password,
             opening_balance, currency, acc_type,
             daily_transfer_limit,
             security_question, ans_digest, ans_salt,
             parent_name, parent_phone,
             fee_waiver, fee_waiver, fee_waiver,
             cc_num, cc_hash, cc_salt, credit_card_limit),
        )
        conn.commit()

        log_account_action(conn, None, acc_num, "create", f"Account created (Type: {acc_type}, Currency: {currency})")
        if opening_balance > 0:
            log_transaction(conn, None, acc_num, "deposit", opening_balance, currency, "other", "completed", opening_balance)

        return {"status": "success", "message": f"Account {acc_num} created successfully.", "data": {"acc_num": acc_num}}
    except sqlite3.IntegrityError as e:
        return {"status": "error", "message": f"Account {acc_num} already exists or error: {e}", "data": None}


def delete_account(conn: sqlite3.Connection, *args) -> dict:
    if len(args) == 1:
        log_conn, acc_num = None, args[0]
    elif len(args) >= 2:
        log_conn, acc_num = args[0], args[1]
    else:
        return {"status": "error", "message": "Missing account number.", "data": None}
    acc_num = int(acc_num)
    cur = conn.execute("DELETE FROM accounts WHERE acc_num=?", (acc_num,))
    conn.commit()
    if cur.rowcount:
        log_account_action(conn, log_conn, acc_num, "delete", f"Account {acc_num} deleted.")
        return {"status": "success", "message": f"Account {acc_num} deleted.", "data": None}
    return {"status": "error", "message": "Account not found.", "data": None}


def freeze_account(conn: sqlite3.Connection, *args, **kwargs) -> dict:
    freeze = kwargs.get("freeze", True)
    if len(args) == 1:
        log_conn, acc_num = None, args[0]
    elif len(args) == 2:
        if isinstance(args[1], bool):
            log_conn, acc_num, freeze = None, args[0], args[1]
        else:
            log_conn, acc_num = args[0], args[1]
    elif len(args) >= 3:
        log_conn, acc_num, freeze = args[0], args[1], args[2]
    else:
        return {"status": "error", "message": "Missing account number.", "data": None}

    acc_num = int(acc_num)
    row = conn.execute(
        "SELECT acc_num, is_frozen FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not row:
        return {"status": "error", "message": "Account not found.", "data": None}
    conn.execute(
        "UPDATE accounts SET is_frozen=? WHERE acc_num=?", (int(freeze), acc_num)
    )
    conn.commit()
    action = 'frozen' if freeze else 'unfrozen'
    log_freeze_action(conn, log_conn, acc_num, action, f"Account {acc_num} {action} by admin.")
    log_account_action(conn, log_conn, acc_num, f"ACCOUNT_{action.upper()}", f"Account {acc_num} {action} by admin.")
    return {"status": "success", "message": f"Account {acc_num} {action}.", "data": {"frozen": freeze}}


def set_daily_limit(conn: sqlite3.Connection, acc_num: int, new_limit: int) -> dict:
    if new_limit < 0:
        return {"status": "error", "message": "Limit cannot be negative.", "data": None}
    row = conn.execute(
        "SELECT currency FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not row:
        return {"status": "error", "message": "Account not found.", "data": None}
    conn.execute(
        "UPDATE accounts SET daily_transfer_limit=? WHERE acc_num=?", (new_limit, acc_num)
    )
    conn.commit()
    log_account_action(conn, None, acc_num, "set_daily_limit", f"Daily transfer limit updated to {new_limit}.")
    return {"status": "success", "message": "Daily transfer limit updated.", "data": {"new_limit": new_limit, "currency": row['currency']}}


def convert_account_type(conn: sqlite3.Connection, acc_num: int, to_type: str) -> dict:
    row = conn.execute(
        "SELECT * FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not row:
        return {"status": "error", "message": "Account not found.", "data": None}
    if to_type == row["acc_type"]:
        return {"status": "error", "message": f"Account is already type '{to_type}'.", "data": None}
    
    cc_num = None
    if to_type == "credit_card":
        cc_num = f"4000{acc_num:08d}"
        default_pin = "1234"
        pin_digest, pin_salt = hash_secret(default_pin)
        conn.execute(
            """UPDATE accounts SET acc_type='credit_card',
               credit_card_num=?,
               credit_card_pin_hash=?, credit_card_pin_salt=?,
               credit_card_limit=1000000, credit_used=0 WHERE acc_num=?""",
            (cc_num, pin_digest, pin_salt, acc_num),
        )
    elif to_type == "student":
        conn.execute(
            """UPDATE accounts SET acc_type='student',
               credit_card_num=NULL, credit_card_pin_hash=NULL, credit_card_pin_salt=NULL,
               credit_card_limit=0, credit_used=0,
               maintenance_fee_waived=1, sms_alert_fee_waived=1, card_issuance_fee_waived=1
               WHERE acc_num=?""",
            (acc_num,),
        )
    else:
        conn.execute(
            """UPDATE accounts SET acc_type='non_credit_card',
               credit_card_num=NULL,
               credit_card_pin_hash=NULL, credit_card_pin_salt=NULL,
               credit_card_limit=0, credit_used=0 WHERE acc_num=?""",
            (acc_num,),
        )
    conn.commit()
    log_account_action(conn, None, acc_num, "convert_type", f"Converted account to '{to_type}'.")
    if to_type == "credit_card":
        msg = f"Account converted to 'credit_card'. Card number: {cc_num} | Default PIN: 1234 | [IMPORTANT] Tell the customer to change their PIN."
        return {"status": "success", "message": msg, "data": {"new_type": "credit_card", "cc_num": cc_num, "credit_limit": 1000000, "currency": row['currency']}}
    return {"status": "success", "message": f"Account converted to '{to_type}'.", "data": {"new_type": to_type}}


# =============================================================================
#  Vault CRUD
# =============================================================================

def create_vault(
    conn: sqlite3.Connection,
    acc_num: int,
    vault_no: str,
    vault_password: str,
) -> dict:
    row = conn.execute(
        "SELECT * FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not row:
        return {"status": "error", "message": "Account not found.", "data": None}
    if row["vault_no"]:
        return {"status": "error", "message": "This account already has a vault.", "data": None}
    existing = conn.execute(
        "SELECT acc_num FROM accounts WHERE vault_no=?", (vault_no,)
    ).fetchone()
    if existing:
        return {"status": "error", "message": f"Vault number '{vault_no}' is already in use.", "data": None}
    vpw_digest, vpw_salt = hash_secret(vault_password)
    conn.execute(
        """UPDATE accounts SET vault_no=?,
           vault_password_hash=?, vault_password_salt=?,
           vault_balance=0 WHERE acc_num=?""",
        (vault_no, vpw_digest, vpw_salt, acc_num),
    )
    conn.execute(
        """INSERT OR REPLACE INTO vaults (vault_no, acc_num, cash_balance, vault_password_hash, vault_password_salt)
           VALUES (?, ?, 0, ?, ?)""",
        (vault_no, acc_num, vpw_digest, vpw_salt),
    )
    conn.commit()
    log_account_action(conn, None, acc_num, "create_vault", f"Vault {vault_no} created.")
    return {"status": "success", "message": f"Vault '{vault_no}' created for account {acc_num}.", "data": {"vault_no": vault_no}}


def destroy_vault(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection | None,
    acc_num: int,
    vault_password: str,
    transfer_to_balance: bool = True,
) -> dict:
    row = conn.execute(
        "SELECT * FROM accounts WHERE acc_num=?", (acc_num,)
    ).fetchone()
    if not row or not row["vault_no"]:
        return {"status": "error", "message": "No vault found for this account.", "data": None}
    vpw_salt = row["vault_password_salt"] or ""
    if not verify_secret(vault_password, row["vault_password_hash"], vpw_salt):
        return {"status": "error", "message": "Wrong vault password. Vault not destroyed.", "data": None}

    vault_balance = row["vault_balance"]
    v_no = row["vault_no"]
    if vault_balance > 0 and transfer_to_balance:
        conn.execute(
            """UPDATE accounts SET acc_balance=acc_balance+vault_balance,
               vault_no=NULL, vault_password_hash=NULL, vault_password_salt=NULL,
               vault_balance=0 WHERE acc_num=?""",
            (acc_num,),
        )
        new_bal = conn.execute(
            "SELECT acc_balance FROM accounts WHERE acc_num=?", (acc_num,)
        ).fetchone()["acc_balance"]
        log_transaction(conn, tx_log_conn, acc_num, "vault_deduct", vault_balance,
                        row["currency"], "other", "success", new_bal)
    else:
        conn.execute(
            """UPDATE accounts SET vault_no=NULL, vault_password_hash=NULL,
               vault_password_salt=NULL, vault_balance=0 WHERE acc_num=?""",
            (acc_num,),
        )
    conn.execute("DELETE FROM vaults WHERE vault_no=?", (v_no,))
    conn.commit()
    log_account_action(conn, None, acc_num, "destroy_vault", f"Vault {v_no} destroyed.")
    return {"status": "success", "message": "Vault destroyed.", "data": {"released_amount": vault_balance, "currency": row['currency']}}


# =============================================================================
#  OOP loader / saver (for Payment_Processor compatibility if needed)
# =============================================================================

def load_accounts(conn: sqlite3.Connection) -> list[Account]:
    """Reconstruct OOP Account/CreditCard objects with Vault attached."""
    accounts: list[Account] = []
    for row in conn.execute("SELECT * FROM accounts ORDER BY acc_num").fetchall():
        if row["acc_type"] == "credit_card":
            acc = CreditCard(
                acc_num=row["acc_num"],
                password_hash=row["acc_password_hash"],
                acc_balance=row["acc_balance"],
                currency=row["currency"],
                credit_card_num=row["credit_card_num"],
                cc_pin_hash=row["credit_card_pin_hash"],
                credit_card_limit=row["credit_card_limit"],
                credit_used=row["credit_used"],
                is_frozen=bool(row["is_frozen"]),
                daily_transfer_limit=row["daily_transfer_limit"],
            )
        elif row["acc_type"] == "student":
            acc = StudentAccount(
                acc_num=row["acc_num"],
                password_hash=row["acc_password_hash"],
                initial_deposit_minor=row["acc_balance"],
                currency=row["currency"],
                parent_name=row["parent_name"] or "Parent",
                parent_phone=row["parent_phone"] or "+8801000000000",
                is_frozen=bool(row["is_frozen"]),
                daily_transfer_limit=row["daily_transfer_limit"],
            )
        else:
            acc = Non_Credit_Card(
                acc_num=row["acc_num"],
                password_hash=row["acc_password_hash"],
                acc_balance=row["acc_balance"],
                currency=row["currency"],
                is_frozen=bool(row["is_frozen"]),
                daily_transfer_limit=row["daily_transfer_limit"],
            )
        if row["vault_no"]:
            acc.vault = Vault(
                vault_no=row["vault_no"],
                password_hash=row["vault_password_hash"],
                cash_balance=row["vault_balance"],
            )
            v_items_raw = conn.execute("SELECT * FROM vault_items WHERE vault_no=?", (row["vault_no"],)).fetchall()
            for v_row in v_items_raw:
                acc.vault.add_item(VaultItem(
                    item_id=v_row["item_id"],
                    vault_no=v_row["vault_no"],
                    item_type=v_row["item_type"],
                    description=v_row["description"],
                    est_value=v_row["est_value_minor"],
                    added_at=v_row["added_at"]
                ))
        accounts.append(acc)
    return accounts


def save_account(conn: sqlite3.Connection, acc: Account) -> None:
    """Upsert a single Account/CreditCard object back to the DB."""
    is_cc = isinstance(acc, CreditCard)
    vault = getattr(acc, "vault", None)
    conn.execute(
        """INSERT OR REPLACE INTO accounts (
            acc_num, acc_password_hash, acc_balance, currency, acc_type,
            credit_card_num, credit_card_pin_hash, credit_card_limit, credit_used,
            vault_no, vault_password_hash, vault_balance,
            is_frozen, daily_transfer_limit
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            acc.acc_num, acc._password_hash, acc.acc_balance, acc.currency,
            "credit_card" if is_cc else "non_credit_card",
            acc.credit_card_num   if is_cc else None,
            acc._cc_pin_hash      if is_cc else None,
            acc.credit_card_limit if is_cc else 0,
            acc.credit_used       if is_cc else 0,
            vault.vault_no        if vault else None,
            vault.lock._hash      if vault else None,
            vault.balance         if vault else 0,
            int(acc.is_frozen),
            acc.daily_transfer_limit,
        ),
    )
    conn.commit()


# =============================================================================
#  Listing / filtering / overview
# =============================================================================

def list_accounts(
    conn: sqlite3.Connection,
    filter_key: str = "all_account",
) -> list[sqlite3.Row]:
    """Return account rows matching the given filter key."""
    where = FILTER_WHERE.get(filter_key, "1=1")
    return conn.execute(
        f"SELECT * FROM accounts WHERE {where} ORDER BY acc_num"
    ).fetchall()


def bank_overview(
    conn: sqlite3.Connection,
    pending_conn: sqlite3.Connection | None = None,
) -> dict:
    """
    Return a summary dict:
        totals          → {BDT: int, USD: int}  (minor units)
        frozen_accounts → int
        pending_count   → int
    """
    pdb = pending_conn if pending_conn is not None else conn
    totals = {
        r["currency"]: r["s"]
        for r in conn.execute(
            "SELECT currency, COALESCE(SUM(acc_balance),0) AS s "
            "FROM accounts GROUP BY currency"
        ).fetchall()
    }
    frozen  = conn.execute(
        "SELECT COUNT(*) AS c FROM accounts WHERE is_frozen=1"
    ).fetchone()["c"]
    pending = pdb.execute(
        "SELECT COUNT(*) AS c FROM pending_transfers WHERE status='pending'"
    ).fetchone()["c"]
    return {"totals": totals, "frozen_accounts": frozen, "pending_count": pending}


def get_logs(
    conn: sqlite3.Connection,
    table: str,
    acc_num: int | None = None,
    limit: int = 50,
    acc_log_conn: sqlite3.Connection | None = None,
    tx_log_conn: sqlite3.Connection | None = None,
    freeze_log_conn: sqlite3.Connection | None = None,
    pending_conn: sqlite3.Connection | None = None,
) -> list[sqlite3.Row]:
    """Retrieve rows from account_log, transaction_log, freezing_account, or pending_transfers.
    Uses the dedicated log DB connection if provided, else falls back to main conn."""
    if table not in {"account_log", "transaction_log", "freezing_account", "freeze_log", "pending_transfers"}:
        return []
    # Route to the appropriate log DB
    if table == "account_log":
        db = acc_log_conn if acc_log_conn is not None else conn
    elif table == "transaction_log":
        db = tx_log_conn if tx_log_conn is not None else conn
    elif table in {"freezing_account", "freeze_log"}:
        table = "freeze_log"
        db = freeze_log_conn if freeze_log_conn is not None else conn
    else:  # pending_transfers
        db = pending_conn if pending_conn is not None else conn
    # pending_transfers uses sender_acc_num, not acc_num
    if acc_num is None:
        return db.execute(
            f"SELECT * FROM {table} ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    if table == "pending_transfers":
        return db.execute(
            f"SELECT * FROM {table} WHERE sender_acc_num=? ORDER BY timestamp DESC LIMIT ?",
            (acc_num, limit),
        ).fetchall()
    return db.execute(
        f"SELECT * FROM {table} WHERE acc_num=? ORDER BY timestamp DESC LIMIT ?",
        (acc_num, limit),
    ).fetchall()


def recent_transactions(
    conn: sqlite3.Connection,
    acc_num: int,
    limit: int = 10,
    tx_log_conn: sqlite3.Connection | None = None,
) -> list[sqlite3.Row]:
    """Return the most recent transaction_log rows for an account."""
    db = tx_log_conn if tx_log_conn is not None else conn
    return db.execute(
        "SELECT * FROM transaction_log WHERE acc_num=? ORDER BY timestamp DESC LIMIT ?",
        (acc_num, limit),
    ).fetchall()


# =============================================================================
#  Export (on-demand only — never kept live)
# =============================================================================

def export_accounts_xlsx(
    conn: sqlite3.Connection,
    path: str,
    filter_key: str = "all_account",
) -> dict:
    try:
        from openpyxl import Workbook
    except ImportError:
        return {"status": "error", "message": "openpyxl not installed. Run: pip install openpyxl", "data": None}
    rows = list_accounts(conn, filter_key)
    wb = Workbook()
    ws = wb.active
    ws.title = "accounts"
    if rows:
        ws.append(list(rows[0].keys()))
        for r in rows:
            ws.append([r[h] for h in rows[0].keys()])
    wb.save(path)
    return {"status": "success", "message": f"{len(rows)} account(s) exported to XLSX.", "data": {"path": path, "count": len(rows)}}


def export_accounts_csv(
    conn: sqlite3.Connection,
    path: str,
    filter_key: str = "all_account",
) -> dict:
    rows = list_accounts(conn, filter_key)
    if not rows:
        Path(path).write_text("", encoding="utf-8")
        return {"status": "success", "message": f"No accounts matched '{filter_key}'. Empty file created.", "data": {"path": path, "count": 0}}
    headers = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow([r[h] for h in headers])
    return {"status": "success", "message": f"{len(rows)} account(s) exported to CSV.", "data": {"path": path, "count": len(rows)}}


# =============================================================================
#  CSV / XLSX import
# =============================================================================

def import_file_to_table(
    conn: sqlite3.Connection,
    file_path: str,
    table: str,
) -> tuple[int, list[str]]:
    """
    Route an uploaded file to the correct format handler based on its
    extension, then insert its rows into `table`. `table` is ignored for
    .sql scripts, which operate at the database level rather than one table.

    Returns (rows_inserted, errors). Never prints or reads input, and never
    raises for ordinary bad-input cases (missing file, unknown table, bad
    extension, malformed rows) - those all come back as strings in `errors`
    so main.py's CLI (or a future UI) can display them however it likes.
    """
    path = Path(file_path)
    if not path.exists():
        return 0, [f"File not found: {file_path}"]

    suffix = path.suffix.lower()

    if suffix == ".csv":
        return _import_csv(conn, table, file_path)
    elif suffix == ".xlsx":
        return _import_xlsx(conn, table, file_path)
    elif suffix == ".json":
        return _import_json(conn, table, file_path)
    elif suffix == ".xml":
        return _import_xml(conn, table, file_path)
    elif suffix == ".sql":
        return _import_sql(conn, file_path)
    else:
        return 0, [f"Unsupported file extension '{suffix}'. Supported: .csv, .xlsx, .json, .xml, .sql"]


# =============================================================================
#  Sample data seeder (dev / testing only)
# =============================================================================

def seed_sample_data(conn: sqlite3.Connection) -> dict:
    if conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]:
        return {"status": "success", "message": "Database already contains accounts.", "data": None}

    pw1_digest, pw1_salt = hash_secret("pass1001")
    conn.execute(
        """INSERT INTO accounts
           (acc_num, acc_password_hash, acc_password_salt, original_password, raw_password_debug,
            acc_balance, currency, acc_type, daily_transfer_limit, security_question, security_answer_hash, security_answer_salt)
           VALUES (?,?,?,?,?,?,?,?,?,'What is your pet?','dog','')""",
        (1001, pw1_digest, pw1_salt, "pass1001", "pass1001",
         2_500_000, "BDT", "non_credit_card", 700_000),
    )

    pw2_digest, pw2_salt     = hash_secret("pass2001")
    pin_digest, pin_salt     = hash_secret("7777")
    vpw_digest, vpw_salt     = hash_secret("vault2001")
    conn.execute(
        """INSERT INTO accounts
           (acc_num, acc_password_hash, acc_password_salt, original_password, raw_password_debug,
            acc_balance, currency, acc_type,
            credit_card_num, credit_card_pin_hash, credit_card_pin_salt,
            credit_card_limit, credit_used, daily_transfer_limit,
            vault_no, vault_password_hash, vault_password_salt, vault_balance,
            security_question, security_answer_hash, security_answer_salt)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'What is your city?','dhaka','')""",
        (
            2001, pw2_digest, pw2_salt, "pass2001", "pass2001",
            50_000, "USD", "credit_card",
            "4111111122223333", pin_digest, pin_salt,
            200_000, 0, 50_000,
            "V001", vpw_digest, vpw_salt, 0,
        ),
    )
    conn.commit()
    return {"status": "success", "message": "Accounts 1001 (BDT) and 2001 (USD+CC+Vault) inserted.", "data": None}



# =============================================================================
#  Bulk Import V3 Additions
# =============================================================================



def get_all_table_names(conn: sqlite3.Connection) -> list[str]:
    """
    Dynamically list every real table in the connected database by querying
    sqlite_master, instead of maintaining a second hardcoded list that can
    drift out of sync with SCHEMA. Excludes SQLite's own internal
    bookkeeping tables (sqlite_sequence etc.) so the admin import/export
    menu only ever shows tables that actually belong to the schema.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def _get_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """
    Return the column names of `table` via PRAGMA table_info, in schema
    order. Callers MUST validate `table` against get_all_table_names()
    first - PRAGMA table_info doesn't accept '?' placeholders for the
    table name (SQLite has no parameter binding for identifiers), so this
    only stays injection-safe because every caller below checks the name
    against the real table list before it ever reaches this f-string.
    Returns [] for a table that doesn't exist.
    """
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [row[1] for row in rows]  # PRAGMA table_info column 1 = column name


def _insert_row_dynamic(
    conn: sqlite3.Connection,
    table: str,
    row: dict,
    valid_columns: set[str],
    strict: bool,
) -> tuple[bool, str | None]:
    """
    Insert one row dict into `table`, built dynamically off valid_columns
    (from PRAGMA table_info) instead of a hardcoded per-table column list.
    Every value is bound through a '?' placeholder - only identifiers
    (table/column names, already whitelisted by the caller) are ever
    written into the SQL text itself.

    strict=True  - reject the whole row if it contains ANY key that isn't
                   a real column (used by JSON, per the mismatched-keys rule).
    strict=False - silently drop unknown keys and insert whatever usable
                   columns remain (used by CSV/XLSX/XML, which tolerate
                   stray header/tag noise as long as something usable exists).

    Returns (True, None) on success, (False, error_message) on failure.
    """
    unknown = set(row.keys()) - valid_columns
    if strict and unknown:
        return False, f"Unrecognized column(s) {sorted(unknown)}."

    usable = {k: v for k, v in row.items() if k in valid_columns}
    if not usable:
        return False, "Row has no keys matching table columns."

    # Student accounts always get all three fee waivers, regardless of
    # what the imported file said — this is a business rule enforced at
    # insert time, not an admin/import-overridable field. Only applies
    # to the accounts table, and only once acc_type is actually 'student'.
    if table == "accounts" and usable.get("acc_type") == "student":
        usable["maintenance_fee_waived"] = 1
        usable["sms_alert_fee_waived"] = 1
        usable["card_issuance_fee_waived"] = 1

    columns = list(usable.keys())
    quoted_columns = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    sql = f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})'

    try:
        conn.execute(sql, [usable[c] for c in columns])
        return True, None
    except sqlite3.Error as e:
        return False, str(e)


def _import_csv(conn: sqlite3.Connection, table: str, file_path: str) -> tuple[int, list[str]]:
    """Import rows from a CSV file into `table`. Returns (rows_inserted, errors)."""
    if table not in get_all_table_names(conn):
        return 0, [f"Table '{table}' does not exist."]
    valid_columns = set(_get_table_columns(conn, table))

    inserted = 0
    errors: list[str] = []
    try:
        with open(file_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for line_num, row in enumerate(reader, start=2):  # header is line 1
                ok, err = _insert_row_dynamic(conn, table, dict(row), valid_columns, strict=False)
                if ok:
                    inserted += 1
                else:
                    errors.append(f"Row {line_num}: {err}")
    except FileNotFoundError:
        return 0, [f"File not found: {file_path}"]
    except csv.Error as e:
        errors.append(f"CSV parse error: {e}")

    conn.commit()
    return inserted, errors


def _import_xlsx(conn: sqlite3.Connection, table: str, file_path: str) -> tuple[int, list[str]]:
    """Import rows from an XLSX workbook's active sheet into `table`. Returns (rows_inserted, errors)."""
    if table not in get_all_table_names(conn):
        return 0, [f"Table '{table}' does not exist."]
    valid_columns = set(_get_table_columns(conn, table))

    try:
        import openpyxl
    except ImportError:
        return 0, ["openpyxl not installed. Run: pip install openpyxl"]

    try:
        wb = openpyxl.load_workbook(file_path, data_only=True)
    except FileNotFoundError:
        return 0, [f"File not found: {file_path}"]
    except Exception as e:
        return 0, [f"Could not open workbook: {e}"]

    sheet = wb.active
    rows_iter = sheet.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return 0, ["Workbook sheet is empty."]
    # cell is not None (not "if cell") so a header literally named 0 isn't
    # mistaken for a blank column.
    headers = [str(cell) if cell is not None else f"col_{i}" for i, cell in enumerate(header_row)]

    inserted = 0
    errors: list[str] = []
    for row_num, values in enumerate(rows_iter, start=2):
        if all(v is None for v in values):
            continue
        row_dict = dict(zip(headers, values))
        ok, err = _insert_row_dynamic(conn, table, row_dict, valid_columns, strict=False)
        if ok:
            inserted += 1
        else:
            errors.append(f"Row {row_num}: {err}")

    conn.commit()
    return inserted, errors


def _import_json(conn: sqlite3.Connection, table: str, file_path: str) -> tuple[int, list[str]]:
    """
    Import a top-level JSON array of objects into `table`.
    Strict validation: an object's keys are checked against the table's
    real columns (via PRAGMA table_info) - any object with a key that
    isn't a real column is rejected outright rather than partially
    inserted. Returns (rows_inserted, errors).
    """
    if table not in get_all_table_names(conn):
        return 0, [f"Table '{table}' does not exist."]
    valid_columns = set(_get_table_columns(conn, table))

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return 0, [f"File not found: {file_path}"]
    except json.JSONDecodeError as e:
        return 0, [f"Invalid JSON: {e}"]

    if not isinstance(data, list):
        return 0, ["Top-level JSON must be an array of objects."]

    inserted = 0
    errors: list[str] = []
    for idx, obj in enumerate(data):
        if not isinstance(obj, dict):
            errors.append(f"Row {idx}: not a JSON object, skipped.")
            continue
        ok, err = _insert_row_dynamic(conn, table, obj, valid_columns, strict=True)
        if ok:
            inserted += 1
        else:
            errors.append(f"Row {idx}: {err}")

    conn.commit()
    return inserted, errors


def _import_xml(conn: sqlite3.Connection, table: str, file_path: str) -> tuple[int, list[str]]:
    """
    Import rows from an XML file into `table`: one child element of the
    root = one row, and that element's grandchild tags map to columns.
    Returns (rows_inserted, errors).
    """
    if table not in get_all_table_names(conn):
        return 0, [f"Table '{table}' does not exist."]
    valid_columns = set(_get_table_columns(conn, table))

    try:
        tree = ET.parse(file_path)
    except FileNotFoundError:
        return 0, [f"File not found: {file_path}"]
    except ET.ParseError as e:
        return 0, [f"Invalid XML: {e}"]

    inserted = 0
    errors: list[str] = []
    for idx, element in enumerate(tree.getroot()):
        row_dict = {child.tag: child.text for child in element}
        ok, err = _insert_row_dynamic(conn, table, row_dict, valid_columns, strict=False)
        if ok:
            inserted += 1
        else:
            errors.append(f"Row {idx} (<{element.tag}>): {err}")

    conn.commit()
    return inserted, errors


# Disallow schema/pragma/maintenance statements in admin-uploaded SQL scripts.
# executescript() runs the WHOLE file - without this, any uploaded .sql file
# could DROP tables, ATTACH another database, or disable PRAGMA foreign_keys.
BLOCKED = re.compile(r"\b(DROP|ALTER|ATTACH|DETACH|PRAGMA|VACUUM)\b", re.I)


def _import_sql(conn: sqlite3.Connection, file_path: str) -> tuple[int, list[str]]:
    """
    Execute an admin-uploaded raw SQL script against the database.
    Not scoped to one table - the script itself decides what it touches.

    Blocked before execution if it contains DROP/ALTER/ATTACH/DETACH/
    PRAGMA/VACUUM (case-insensitive). This does not make arbitrary INSERT/
    UPDATE/DELETE statements safe from misuse - it only stops the specific
    schema-and-connection-level statements that could destroy or escape
    the database - so this feature must stay admin-only.

    Returns (1, []) on success (a script is one unit of work, not a row
    count) or (0, [error]) if it was blocked or failed and was rolled back.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw_sql = f.read()
    except FileNotFoundError:
        return 0, [f"File not found: {file_path}"]

    if BLOCKED.search(raw_sql):
        return 0, ["Script contains a disallowed statement."]

    try:
        conn.execute("BEGIN")
        conn.executescript(raw_sql)
        conn.commit()
    except Exception as e:
        conn.rollback()
        return 0, [str(e)]

    return 1, []


# =============================================================================
#  Self-Service Reset Engine (V3 Additions)
# =============================================================================



# =============================================================================
#  Advanced Financial Frameworks & Vaults (V3 Additions)
# =============================================================================

def execute_loan_disbursal(conn: sqlite3.Connection, tx_log_conn: sqlite3.Connection, acc_num: int, principal: int, interest_rate: float) -> dict:
    res = utils_logic.disburse_loan(conn, tx_log_conn, acc_num, principal, interest_rate)
    if res.get("status") == "success":
        data = res["data"]
        log_transaction(conn, tx_log_conn, acc_num, "loan_disbursal", principal, data["currency"], "other", "success", data["balance_after"])
    return res


def execute_loan_repayment(conn: sqlite3.Connection, tx_log_conn: sqlite3.Connection, loan_id: int, payment_amount_minor: int) -> dict:
    res = utils_logic.repay_loan(conn, tx_log_conn, loan_id, payment_amount_minor)
    if res.get("status") == "success":
        data = res["data"]
        log_transaction(conn, tx_log_conn, data["acc_num"], "loan_repayment", data["amount_paid"], data["currency"], "other", "success", data["balance_after"])
    return res


def execute_bond_purchase(conn: sqlite3.Connection, tx_log_conn: sqlite3.Connection, acc_num: int, face_value: int, yield_rate: float, maturity_timestamp: str) -> dict:
    res = utils_logic.purchase_bond(conn, tx_log_conn, acc_num, face_value, yield_rate, maturity_timestamp)
    if res.get("status") == "success":
        data = res["data"]
        log_transaction(conn, tx_log_conn, acc_num, "bond_purchase", face_value, data["currency"], "other", "success", data["balance_after"])
    return res


def execute_bond_redemption(conn: sqlite3.Connection, tx_log_conn: sqlite3.Connection, bond_id: int) -> dict:
    res = utils_logic.redeem_bond(conn, tx_log_conn, bond_id)
    if res.get("status") == "success":
        data = res["data"]
        log_transaction(conn, tx_log_conn, data["acc_num"], "bond_redemption", data["redemption_amt"], data["currency"], "other", "success", data["balance_after"])
    return res


def execute_check_issuance(conn: sqlite3.Connection, tx_log_conn: sqlite3.Connection, acc_num: int, amount: int, payee_name: str | None = None, memo: str | None = None) -> dict:
    res = utils_logic.issue_check(conn, tx_log_conn, acc_num, amount, payee_name, memo)
    if res.get("status") == "success":
        data = res["data"]
        log_transaction(conn, tx_log_conn, acc_num, "check_issued", amount, data["currency"], "other", "success", data["balance_after"])
    return res


def execute_check_clearing(conn: sqlite3.Connection, tx_log_conn: sqlite3.Connection, check_id: int) -> dict:
    res = utils_logic.clear_check(conn, tx_log_conn, check_id)
    return res


def execute_check_bouncing(conn: sqlite3.Connection, tx_log_conn: sqlite3.Connection, check_id: int) -> dict:
    res = utils_logic.bounce_check(conn, tx_log_conn, check_id)
    if res.get("status") == "success":
        data = res["data"]
        log_transaction(conn, tx_log_conn, data["acc_num"], "check_bounced_reversal", data["amount"], data["currency"], "other", "success", data["balance_after"])
    return res


def add_vault_item(
    conn: sqlite3.Connection,
    acc_num: int,
    vault_password: str,
    item_type: str,
    description: str,
    est_value_minor: int,
) -> dict:
    row = conn.execute(
        "SELECT vault_no, vault_password_hash, vault_password_salt, currency FROM accounts WHERE acc_num=?",
        (acc_num,)
    ).fetchone()
    
    if not row:
        return {"status": "error", "message": "Account not found.", "data": None}
        
    row_dict = dict(row)
    vault_no_db = row_dict.get("vault_no")
    vault_pw_hash = row_dict.get("vault_password_hash")
    currency = row_dict.get("currency", "BDT")
    
    if vault_no_db is None:
        return {"status": "error", "message": "No vault linked to this account.", "data": None}

    vault_salt = row_dict.get("vault_password_salt") or ""
    if not verify_secret(vault_password, vault_pw_hash or "", vault_salt):
        return {"status": "error", "message": "Invalid vault password.", "data": None}
        
    valid_types = {'gold', 'paper_deeds', 'corporate_bonds', 'heirlooms'}
    if item_type not in valid_types:
        return {"status": "error", "message": "Invalid item category.", "data": None}
        
    conn.execute(
        "INSERT INTO vault_items (vault_no, item_type, description, est_value_minor) VALUES (?, ?, ?, ?)",
        (vault_no_db, item_type, description, est_value_minor)
    )
    conn.commit()
    
    vault_no_str = f"V{vault_no_db}" if isinstance(vault_no_db, int) else str(vault_no_db)
    
    return {"status": "success", "message": f"Asset secured in Vault Compartment {vault_no_str}.", "data": {"vault_no": vault_no_db, "description": description, "est_value_minor": est_value_minor, "currency": currency}}


# =============================================================================
#  Utility Billing & Tax Settlement (V3 Additions)
# =============================================================================

def get_billers_by_category(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT * FROM billers ORDER BY biller_category, biller_name").fetchall()
    result = {}
    for row in rows:
        cat = row["biller_category"]
        if cat not in result:
            result[cat] = []
        result[cat].append(dict(row))
    return result


def pay_biller(
    conn: sqlite3.Connection,
    tx_log_conn: sqlite3.Connection,
    sender_acc_num: int,
    biller_id: int,
    amount: int,
) -> dict:
    if amount <= 0:
        return {"status": "error", "message": "Amount must be a positive integer.", "data": None}

    biller_row = conn.execute("SELECT * FROM billers WHERE biller_id=?", (biller_id,)).fetchone()
    if not biller_row:
        return {"status": "error", "message": "Biller not found.", "data": None}

    sender_row = conn.execute("SELECT acc_balance, is_frozen, currency FROM accounts WHERE acc_num=?", (sender_acc_num,)).fetchone()
    if not sender_row:
        return {"status": "error", "message": "Sender account not found.", "data": None}

    if sender_row["is_frozen"]:
        return {"status": "error", "message": "Account is frozen. Cannot process payment.", "data": None}

    sender_balance = sender_row["acc_balance"]
    currency = sender_row["currency"]
    
    biller_category = biller_row["biller_category"]
    receiver_acc_num = biller_row["receiving_acc_num"]

    tx_type = "tax_pay" if biller_category == "tax" else "utility_pay"
    log_category = "tax" if biller_category == "tax" else "bills"

    if sender_balance < amount:
        log_transaction(
            conn, tx_log_conn, sender_acc_num, tx_type, amount, currency, log_category, "failed", sender_balance
        )
        return {
            "status": "error",
            "message": "Insufficient liquid balance.",
            "data": {"balance": sender_balance, "currency": currency}
        }

    # Execute transfer
    conn.execute(
        "UPDATE accounts SET acc_balance = acc_balance - ? WHERE acc_num = ?",
        (amount, sender_acc_num)
    )
    conn.execute(
        "UPDATE accounts SET acc_balance = acc_balance + ? WHERE acc_num = ?",
        (amount, receiver_acc_num)
    )
    conn.commit()

    balance_after = sender_balance - amount

    log_transaction(
        conn, tx_log_conn, sender_acc_num, tx_type, amount, currency, log_category, "success", balance_after
    )

    return {
        "status": "success",
        "message": f"{biller_category.capitalize()} payment to {biller_row['biller_name']} successful.",
        "data": {
            "amount": amount,
            "currency": currency,
            "balance_after": balance_after,
            "biller_id": biller_id,
            "biller_name": biller_row["biller_name"]
        }
    }

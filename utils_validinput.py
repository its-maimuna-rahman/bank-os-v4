"""
checks each and every valid input of the main function
"""

from __future__ import annotations

import re
import sqlite3
from typing import Collection

from utils_currency import SUPPORTED, parse_amount_input

# ── constants (re-exported so callers need only import this module) ────────────

VALID_CATEGORIES: frozenset[str] = frozenset(
    {"food", "bills", "shopping", "transfer", "other"}
)
VALID_ACC_TYPES: frozenset[str] = frozenset(
    {"credit_card", "non_credit_card", "student"}
)

# Minimum password length enforced by prompt_password (override via min_len=).
DEFAULT_MIN_PASSWORD_LEN: int = 1   # permissive default; set higher as needed

# ── internal helper ────────────────────────────────────────────────────────────

def _read(prompt: str) -> str:
    """Strip whitespace from raw input."""
    return input(prompt).strip()


# =============================================================================
#  Pure validators  (no I/O — raise ValueError on bad input)
# =============================================================================

def validate_amount_minor(raw: str) -> int:
    """
    Parse a major-unit amount string and return minor units (int).

    Raises ValueError if the string is not a positive number.

    Examples:
        validate_amount_minor("125.50")  →  12550
        validate_amount_minor("0")       →  ValueError
        validate_amount_minor("abc")     →  ValueError
    """
    minor = parse_amount_input(raw)
    if minor is None:
        raise ValueError(
            f"Invalid amount {raw!r}. Enter a positive number (e.g. 100 or 120.50)."
        )
    return minor


def validate_positive_int(raw: str) -> int:
    """
    Parse a strictly positive integer string.

    Raises ValueError for non-integers or values ≤ 0.
    """
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"Expected a whole number, got {raw!r}.")
    if value <= 0:
        raise ValueError(f"Value must be greater than 0, got {value}.")
    return value


def validate_int(raw: str) -> int:
    """
    Parse any integer string (may be zero or negative).

    Raises ValueError for non-integers.
    """
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Expected a whole number, got {raw!r}.")


def validate_yes_no(raw: str) -> bool:
    """
    Parse a yes/no confirmation.

    Accepted truthy  values: yes, y  (case-insensitive)
    Accepted falsy   values: no,  n  (case-insensitive)

    Raises ValueError for anything else.
    """
    normalised = raw.strip().lower()
    if normalised in ("yes", "y"):
        return True
    if normalised in ("no", "n"):
        return False
    raise ValueError(
        f"Expected yes or no, got {raw!r}."
    )


def validate_currency(raw: str) -> str:
    """
    Validate and normalise a currency code.

    Returns the uppercased code if supported.
    Raises ValueError for unsupported codes.
    """
    code = raw.strip().upper()
    if code not in SUPPORTED:
        raise ValueError(
            f"Unsupported currency {raw!r}. Choose from: {', '.join(sorted(SUPPORTED))}."
        )
    return code


def validate_acc_type(raw: str) -> str:
    """
    Validate an account-type string.

    Raises ValueError if not one of the three supported types.
    """
    cleaned = raw.strip().lower()
    if cleaned not in VALID_ACC_TYPES:
        raise ValueError(
            f"Invalid account type {raw!r}. "
            f"Choose from: {', '.join(sorted(VALID_ACC_TYPES))}."
        )
    return cleaned


def validate_category(raw: str) -> str:
    """
    Validate a transaction category string.

    Raises ValueError for unknown categories.
    """
    cleaned = raw.strip().lower()
    if cleaned not in VALID_CATEGORIES:
        raise ValueError(
            f"Invalid category {raw!r}. "
            f"Choose from: {', '.join(sorted(VALID_CATEGORIES))}."
        )
    return cleaned


def validate_menu_choice(raw: str, valid: Collection[str]) -> str:
    """
    Validate that raw is one of the values in `valid`.

    Raises ValueError with the list of accepted choices.
    """
    if raw in valid:
        return raw
    raise ValueError(
        f"Invalid choice {raw!r}. Options: {', '.join(sorted(valid))}."
    )


def validate_password(raw: str, *, min_len: int = DEFAULT_MIN_PASSWORD_LEN) -> str:
    """
    Validate a password / PIN / vault-password string.

    Rules:
      • Must not be empty (or whitespace-only).
      • Must contain no spaces.
      • Must be at least `min_len` characters long.

    Raises ValueError with a descriptive message on failure.
    """
    if not raw:
        raise ValueError("Password cannot be empty.")
    if " " in raw:
        raise ValueError("Password must not contain spaces.")
    if len(raw) < min_len:
        raise ValueError(
            f"Password must be at least {min_len} character(s) long "
            f"(got {len(raw)})."
        )
    return raw


def validate_pin(raw: str, *, length: int = 4) -> str:
    """
    Validate a numeric PIN of exactly `length` digits.

    Raises ValueError if the PIN contains non-digits or is the wrong length.
    """
    cleaned = raw.strip()
    if not cleaned.isdigit():
        raise ValueError(f"PIN must contain digits only, got {raw!r}.")
    if len(cleaned) != length:
        raise ValueError(
            f"PIN must be exactly {length} digit(s) long (got {len(cleaned)})."
        )
    return cleaned


def validate_vault_number(raw: str) -> int:
    """
    Validate a vault number.

    Rules:
      • Must be a positive integer (e.g. 1001, 42).

    Raises ValueError if the input is not a positive integer.
    """
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"Vault number must be a positive integer, got {raw!r}.")
    if value <= 0:
        raise ValueError(f"Vault number must be greater than 0, got {value}.")
    return value

def validate_account_number(
    raw: str,
    *,
    conn: sqlite3.Connection | None = None,
    must_exist: bool = False,
    must_not_exist: bool = False,
) -> int:
    """
    Validate an account number string.

    Steps:
      1. Must parse as a positive integer.
      2. If `must_exist=True`   and `conn` is provided → account must be in DB.
      3. If `must_not_exist=True` and `conn` is provided → account must NOT be in DB.

    Raises ValueError with a descriptive message on any failure.
    """
    acc_num = validate_positive_int(raw)

    if conn is not None:
        row = conn.execute(
            "SELECT acc_num FROM accounts WHERE acc_num=?", (acc_num,)
        ).fetchone()
        if must_exist and not row:
            raise ValueError(f"Account {acc_num} does not exist.")
        if must_not_exist and row:
            raise ValueError(f"Account {acc_num} already exists.")

    return acc_num


def validate_filename(raw: str, *, allowed_suffixes: Collection[str] | None = None) -> str:
    """
    Validate a filename / path string.

    Rules:
      • Must not be empty.
      • If `allowed_suffixes` is provided, the filename must end with one of them
        (e.g. [".csv", ".xlsx"]).

    Raises ValueError on failure.
    """
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("Filename cannot be empty.")
    if allowed_suffixes:
        lower = cleaned.lower()
        if not any(lower.endswith(s.lower()) for s in allowed_suffixes):
            raise ValueError(
                f"Filename {raw!r} must end with one of: "
                f"{', '.join(allowed_suffixes)}."
            )
    return cleaned


def validate_filter_key(raw: str, filter_map: dict) -> str:
    """
    Validate a filter key string or 1-based index string against `filter_map`.

    Accepts:
      • An exact key from filter_map (e.g. "usd_accounts").
      • A 1-based integer index corresponding to the ordered key list.

    Raises ValueError if neither matches.
    """
    keys = list(filter_map.keys())
    if raw in filter_map:
        return raw
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(keys):
            return keys[idx]
    except ValueError:
        pass
    raise ValueError(
        f"Invalid filter {raw!r}. "
        f"Enter a filter name or number (1–{len(keys)})."
    )


# =============================================================================
#  Prompting helpers  (loop until valid input, print error messages)
# =============================================================================

def prompt_amount_minor(prompt: str = "  Amount (major units, e.g. 100.00): ") -> int:
    """Loop until the user enters a valid positive amount. Returns minor units."""
    while True:
        raw = _read(prompt)
        try:
            return validate_amount_minor(raw)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_positive_int(prompt: str = "  Enter a positive integer: ") -> int:
    """Loop until the user enters a strictly positive integer."""
    while True:
        raw = _read(prompt)
        try:
            return validate_positive_int(raw)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_int(prompt: str = "  Enter a whole number: ") -> int:
    """Loop until the user enters any integer (including zero or negative)."""
    while True:
        raw = _read(prompt)
        try:
            return validate_int(raw)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_yes_no(prompt: str = "  [yes/no]: ") -> bool:
    """Loop until the user enters yes or no. Returns bool."""
    while True:
        raw = _read(prompt)
        try:
            return validate_yes_no(raw)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_currency(prompt: str = "  Currency (BDT/USD): ") -> str:
    """Loop until the user enters a supported currency code."""
    while True:
        raw = _read(prompt)
        try:
            return validate_currency(raw)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_acc_type(
    prompt: str = "  Account type (credit_card / non_credit_card): ",
) -> str:
    """Loop until the user enters a valid account type."""
    while True:
        raw = _read(prompt)
        try:
            return validate_acc_type(raw)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_category(
    prompt: str = "  Category (food/bills/shopping/transfer/other): ",
) -> str:
    """Loop until the user enters a valid transaction category."""
    while True:
        raw = _read(prompt)
        try:
            return validate_category(raw)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_menu_choice(prompt: str, valid: Collection[str]) -> str:
    """Loop until the user picks one of the values in `valid`."""
    while True:
        raw = _read(prompt)
        try:
            return validate_menu_choice(raw, valid)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_password(
    prompt: str = "  Password: ",
    *,
    min_len: int = DEFAULT_MIN_PASSWORD_LEN,
) -> str:
    """Loop until the user enters a non-empty password of at least `min_len` chars."""
    while True:
        raw = _read(prompt)
        try:
            return validate_password(raw, min_len=min_len)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_pin(
    prompt: str = "  PIN: ",
    *,
    length: int = 4,
) -> str:
    """Loop until the user enters a numeric PIN of exactly `length` digits."""
    while True:
        raw = _read(prompt)
        try:
            return validate_pin(raw, length=length)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_vault_number(prompt: str = "  Vault number (e.g. 1001): ") -> int:
    """Loop until the user enters a valid integer vault number."""
    while True:
        raw = _read(prompt)
        try:
            return validate_vault_number(raw)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_account_number(
    prompt: str = "  Account number: ",
    *,
    conn: sqlite3.Connection | None = None,
    must_exist: bool = False,
    must_not_exist: bool = False,
) -> int:
    """
    Loop until the user enters a valid account number.
    Optionally verifies existence against the DB if `conn` is supplied.
    """
    while True:
        raw = _read(prompt)
        try:
            return validate_account_number(
                raw,
                conn=conn,
                must_exist=must_exist,
                must_not_exist=must_not_exist,
            )
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_filename(
    prompt: str = "  Filename: ",
    *,
    allowed_suffixes: Collection[str] | None = None,
) -> str:
    """Loop until the user enters a non-empty filename with an acceptable suffix."""
    while True:
        raw = _read(prompt)
        try:
            return validate_filename(raw, allowed_suffixes=allowed_suffixes)
        except ValueError as exc:
            print(f"  [!] {exc}")


def prompt_filter_key(
    prompt: str = "  Filter: ",
    *,
    filter_map: dict,
) -> str:
    """
    Display the available filter options, then loop until the user makes a
    valid selection (by name or 1-based index).
    """
    keys = list(filter_map.keys())
    print("  Filter options:")
    for i, k in enumerate(keys, 1):
        print(f"    [{i}] {k}")
    while True:
        raw = _read(prompt)
        try:
            return validate_filter_key(raw, filter_map)
        except ValueError as exc:
            print(f"  [!] {exc}")
"""
utils_currency.py
Currency conversion and amount formatting for BankOS.

All internal amounts are stored as integers in the smallest unit
(paise for BDT, cents for USD).  Human-readable conversion happens
here, at display time only.

The USD<->BDT exchange rate is no longer a hardcoded constant. It is
read live from config.json (the same file utils_storage.py uses for
active_database_path), so an admin can update the rate on disk and have
every subsequent conversion pick it up without restarting the program.
If config.json is missing, unreadable, or the key is bad, a safe
default rate is used instead and the failure is logged.
"""

from __future__ import annotations

import json
import logging

# NOTE: utils_validinput imports utils_currency, so we do NOT import it here
# to avoid a circular dependency.  choose_currency() keeps its own loop.

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

CONFIG_PATH: str = "config.json"
DEFAULT_USD_TO_BDT: float = 120.0      # safety fallback only, not a live rate

SUPPORTED: set[str] = {"BDT", "USD"}

CURRENCY_SYMBOLS: dict[str, str] = {
    "BDT": "৳",
    "USD": "$",
}

# 100 BDT in minor units — the minimum initial deposit floor for a new
# StudentAccount. See min_deposit_for() below for the currency-aware version.
MIN_DEPOSIT_BDT_MINOR: int = 10000


# ── live rate lookup ─────────────────────────────────────────────────────────

def get_usd_to_bdt_rate() -> float:
    """
    Read the current USD -> BDT rate from config.json.

    Always re-reads from disk so the rate is "live" - if an admin edits
    config.json while the program is running, the next conversion call
    picks up the new value.

    Falls back to DEFAULT_USD_TO_BDT and logs an error if:
      - config.json does not exist
      - config.json is not valid JSON
      - the "usd_to_bdt_rate" key is missing
      - the "usd_to_bdt_rate" value can't be cast to float
    Never raises - callers can always trust a usable float comes back.
    """
    try:
        with open(CONFIG_PATH, "r") as config_file:
            config = json.load(config_file)
        rate = float(config["usd_to_bdt_rate"])
        return rate
    except FileNotFoundError:
        logger.error(
            "config.json not found at %r; falling back to default rate %.2f",
            CONFIG_PATH, DEFAULT_USD_TO_BDT,
        )
    except json.JSONDecodeError:
        logger.error(
            "config.json is not valid JSON; falling back to default rate %.2f",
            DEFAULT_USD_TO_BDT,
        )
    except KeyError:
        logger.error(
            "'usd_to_bdt_rate' missing from config.json; falling back to default rate %.2f",
            DEFAULT_USD_TO_BDT,
        )
    except (ValueError, TypeError):
        logger.error(
            "'usd_to_bdt_rate' in config.json is not a valid number; falling back to default rate %.2f",
            DEFAULT_USD_TO_BDT,
        )
    return DEFAULT_USD_TO_BDT


# ── minor ↔ major conversion ─────────────────────────────────────────────────

def to_minor(amount: float) -> int:
    """
    Convert a human-entered major amount to the smallest unit.
    e.g.  to_minor(12.50) → 1250
    Always rounds to the nearest integer to avoid floating-point drift.
    """
    return int(round(amount * 100))


def from_minor(amount: int) -> float:
    """
    Convert an internal minor-unit amount to a human-readable major amount.
    e.g.  from_minor(1250) → 12.5
    """
    return amount / 100.0


# ── formatting ────────────────────────────────────────────────────────────────

def format_money(amount_minor: int, currency: str) -> str:
    """
    Format a minor-unit amount with currency symbol and comma separators.

    Examples:
        format_money(1250000, "BDT")  →  "৳12,500.00 BDT"
        format_money(10000,   "USD")  →  "$100.00 USD"
    """
    symbol = CURRENCY_SYMBOLS.get(currency, currency)
    return f"{symbol}{from_minor(amount_minor):,.2f} {currency}"


def format_money_dual(amount_minor: int, currency: str) -> str:
    """
    Format a minor-unit amount showing the native value and its converted equivalent.

    Examples:
        format_money_dual(1200000, "BDT")  →  "৳12,000.00 BDT (≈ $100.00 USD)"
        format_money_dual(10000,   "USD")  →  "$100.00 USD (≈ ৳12,000.00 BDT)"
    """
    other = "USD" if currency == "BDT" else "BDT"
    converted = convert_minor(amount_minor, currency, other)
    return f"{format_money(amount_minor, currency)} (≈ {format_money(converted, other)})"


# ── conversion ────────────────────────────────────────────────────────────────

def convert_minor(amount_minor: int, from_currency: str, to_currency: str) -> int:
    """
    Convert amount_minor from one currency to another, returning minor units.

    Same-currency:  returns amount_minor unchanged.
    Cross-currency: applies the live rate from get_usd_to_bdt_rate().

    Raises ValueError for unsupported currencies.
    """
    if from_currency not in SUPPORTED or to_currency not in SUPPORTED:
        raise ValueError(
            f"Unsupported currency pair: {from_currency!r} → {to_currency!r}. "
            f"Supported: {', '.join(sorted(SUPPORTED))}"
        )

    if from_currency == to_currency:
        return amount_minor

    usd_to_bdt_rate = get_usd_to_bdt_rate()
    amount_major = from_minor(amount_minor)

    if from_currency == "USD" and to_currency == "BDT":
        converted = amount_major * usd_to_bdt_rate
    else:  # BDT → USD
        converted = amount_major / usd_to_bdt_rate

    return to_minor(converted)


def min_deposit_for(currency: str, usd_to_bdt_rate: float) -> int:
    """
    Return the minimum initial deposit for a student account, in the
    given currency's minor units.

    BDT: returns the flat MIN_DEPOSIT_BDT_MINOR (100 BDT).
    USD: converts that same 100 BDT floor into USD minor units using
         usd_to_bdt_rate, rounded to the nearest integer — so a student
         account opened in USD enforces an equivalent floor rather than
         a flat 100 regardless of currency.

    Raises ValueError for an unsupported currency.
    """
    if currency not in SUPPORTED:
        raise ValueError(
            f"Unsupported currency: {currency!r}. "
            f"Supported: {', '.join(sorted(SUPPORTED))}"
        )

    if currency == "BDT":
        return MIN_DEPOSIT_BDT_MINOR

    bdt_major = from_minor(MIN_DEPOSIT_BDT_MINOR)
    usd_major = bdt_major / usd_to_bdt_rate
    return to_minor(usd_major)


# ── input helpers ─────────────────────────────────────────────────────────────

def parse_amount_input(raw: str) -> int | None:
    """
    Parse a user-entered amount string (major units, e.g. "125.50") into minor units.
    Returns None if the input is invalid or non-positive.

    Examples:
        parse_amount_input("125.50")  →  12550
        parse_amount_input("-1")      →  None
        parse_amount_input("abc")     →  None
    """
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    if value <= 0:
        return None
    return to_minor(value)




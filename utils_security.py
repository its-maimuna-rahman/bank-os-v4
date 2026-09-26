"""
utils_security.py
Security helpers for BankOS: salted hashing, lockout.

Phase 4 upgrade: hash_password() (a single unsalted SHA-256 digest) has
been removed. Every stored secret (account password, vault password,
card PINs, admin password, security answers) is now hashed with
hash_secret(), which generates a fresh random salt on every call, and
verified with verify_secret(). Callers must store BOTH the returned
digest and the salt — a lone hash column can no longer be verified
against, since the salt is required to reproduce it.

Admin authentication is no longer a hardcoded module-level constant.
Admins are rows in the `admins` table (see utils_storage.py's
authenticate_admin(), Part 2), authenticated the same digest+salt way
as everything else instead of a single baked-in password.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

# ── constants ─────────────────────────────────────────────────────────────────

LOCKOUT_MINUTES = 2
MAX_ATTEMPTS    = 3
VAULT_MAX_TRIES = 3  # vault password attempts before cancellation


# ── salted hashing ────────────────────────────────────────────────────────────

def hash_secret(plaintext: str) -> tuple[str, str]:
    """
    Hash plaintext with a fresh random salt.

    Returns (digest, salt). Both must be stored — digest alone cannot be
    verified against later, since the salt is needed to reproduce it.
    """
    salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + plaintext).encode("utf-8")).hexdigest()
    return digest, salt


def verify_secret(plaintext: str, digest: str, salt: str) -> bool:
    """
    Verify plaintext against a previously stored (digest, salt) pair.
    Re-hashes plaintext with the given salt and compares to digest.
    """
    return hashlib.sha256((salt + plaintext).encode("utf-8")).hexdigest() == digest


# ── timestamps ────────────────────────────────────────────────────────────────

def now_iso() -> str:
    """Current datetime as ISO 8601 string (seconds precision)."""
    return datetime.now().isoformat(timespec="seconds")


def now_display() -> str:
    """Current datetime formatted for log display: YYYY-MM-DD HH:MM."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def compute_lockout_until() -> str:
    """
    Return an ISO timestamp LOCKOUT_MINUTES from now.
    Stored in DB `locked_until` column on the 3rd failed login.
    """
    return (datetime.now() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat(timespec="seconds")


# ── lockout checks ────────────────────────────────────────────────────────────

def is_locked(locked_until: str | None) -> tuple[bool, int]:
    """
    Check whether a lockout timestamp is still active.

    Returns:
        (True, seconds_remaining)  — if still locked
        (False, 0)                 — if not locked or lock has expired
    """
    if not locked_until:
        return False, 0
    try:
        lock_dt   = datetime.fromisoformat(locked_until)
        remaining = int((lock_dt - datetime.now()).total_seconds())
        if remaining > 0:
            return True, remaining
    except ValueError:
        pass  # malformed timestamp — treat as unlocked
    return False, 0


def format_lockout_message(seconds: int) -> str:
    """Human-readable lockout message for the login screen."""
    minutes, secs = divmod(seconds, 60)
    if minutes > 0:
        return f"Account locked. Try again in {minutes}m {secs}s."
    return f"Account locked. Try again in {secs}s."

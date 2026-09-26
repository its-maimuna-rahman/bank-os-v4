"""
account_cls.py
Core OOP models for BankOS v3.

Phase 5 changes
---------------
- ``Vault`` refactored:
    * ``cash_balance`` replaces the old ``balance`` alias (cash still lives on
      ``accounts.vault_balance`` in the DB; this object mirrors that integer).
    * ``items`` — an in-memory list of ``VaultItem`` named-tuples that mirrors
      the ``vault_items`` table rows loaded from the DB.  The list is populated
      by ``utils_storage.load_accounts()`` after the Vault is constructed.
    * ``add_item`` / ``remove_item`` helpers let callers mutate the local cache
      without touching the DB (persistence is always the storage layer's job).
    * ``total_estimated_value`` — cash_balance + sum of all item est_values.
- ``Lock.check``, ``Account.check_password``, ``CreditCard.check_cc_pin`` now
  accept an optional ``salt`` argument (Phase 4 migration — empty-string default
  keeps them backward-compatible with unsalted in-memory objects).
- No ``print()`` or ``input()`` anywhere in this file.
"""

from __future__ import annotations

from typing import NamedTuple

from utils_security import verify_secret
from utils_currency import format_money, convert_minor


# =============================================================================
#  VaultItem — immutable value object for physical vault assets
# =============================================================================

class VaultItem(NamedTuple):
    """
    Represents one row from the ``vault_items`` table loaded into memory.

    All fields correspond directly to DB columns so the in-memory state and
    the on-disk state stay structurally identical.  Amounts are in minor units.

    ``item_id`` is ``None`` for items that have been constructed locally but
    not yet persisted (i.e., between the admin entering the details and the
    INSERT completing).  After the INSERT, ``utils_storage`` should rebuild the
    object with the real ``item_id`` from ``cursor.lastrowid``.
    """
    item_id:     int | None   # None until persisted
    vault_no:    str          # e.g. "V001"
    item_type:   str          # 'gold' | 'paper_deeds' | 'corporate_bonds' | 'heirlooms'
    description: str          # free-text description entered by admin
    est_value:   int          # estimated value in minor units
    added_at:    str          # ISO-8601 datetime string from DB

    # ── convenience ──────────────────────────────────────────────────────────

    @property
    def formatted_type(self) -> str:
        return self.item_type.replace("_", " ").title()

    def __str__(self) -> str:
        return (
            f"VaultItem(id={self.item_id}, type={self.item_type!r}, "
            f"desc={self.description!r}, est_value={self.est_value})"
        )


# Valid item_type values — enforced by storage-layer callers, not the ORM.
VALID_ITEM_TYPES: frozenset[str] = frozenset(
    {"gold", "paper_deeds", "corporate_bonds", "heirlooms"}
)


# =============================================================================
#  Lock — password guard for Vault objects
# =============================================================================

class Lock:
    """
    Holds a hashed vault password.  Plaintext never stored after construction.

    ``check()`` accepts an optional salt so callers can pass the DB-stored salt
    for salted rows (Phase 4); omitting it or passing ``""`` falls back to
    treating the digest as an unsalted hash — used by legacy rows and tests.
    """

    def __init__(self, password_hash: str) -> None:
        self._hash = password_hash

    def check(self, plaintext: str, salt: str = "") -> bool:
        """Return True if ``plaintext`` matches the stored digest."""
        return verify_secret(plaintext, self._hash, salt)

    @property
    def hash(self) -> str:
        """Read-only access to the raw digest (needed by save_account)."""
        return self._hash

    def __repr__(self) -> str:
        return "Lock(****)"


# =============================================================================
#  Vault — cash + physical asset container
# =============================================================================

class Vault:
    """
    Secure vault attached to an account.

    Design (Phase 5 refactor)
    -------------------------
    * ``cash_balance`` — integer in minor units, mirrors ``accounts.vault_balance``
      in the DB.  This is the only form of *currency* stored in the vault.
    * ``items``       — list of ``VaultItem`` named-tuples, mirrors
      ``vault_items`` rows.  Populated by the storage layer after load; never
      written to the DB from inside this class.
    * ``lock``        — ``Lock`` instance for password-gate checks.

    The ``balance`` property is kept as an alias for ``cash_balance`` so
    existing code (e.g., ``save_account``) does not break.
    """

    def __init__(
        self,
        vault_no:      str,
        password_hash: str,
        cash_balance:  int = 0,
        items:         list[VaultItem] | None = None,
    ) -> None:
        self._vault_no      = str(vault_no)
        self._cash_balance  = int(cash_balance)
        self.lock           = Lock(password_hash)
        self._items: list[VaultItem] = list(items) if items else []

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def vault_no(self) -> str:
        return self._vault_no

    @property
    def cash_balance(self) -> int:
        """Cash held in the vault (minor units)."""
        return self._cash_balance

    @property
    def balance(self) -> int:
        """Alias for ``cash_balance`` — keeps legacy call sites working."""
        return self._cash_balance

    @property
    def items(self) -> list[VaultItem]:
        """Read-only view of in-memory vault items."""
        return list(self._items)

    @property
    def total_estimated_value(self) -> int:
        """
        Sum of cash_balance and the estimated values of all physical items,
        all in minor units.
        """
        return self._cash_balance + sum(item.est_value for item in self._items)

    # ── cash movement ─────────────────────────────────────────────────────────

    def add_cash(self, amount: int) -> None:
        """Add ``amount`` minor units to cash_balance.  Must be positive."""
        if amount <= 0:
            raise ValueError("Amount must be positive.")
        self._cash_balance += amount

    def deduct_cash(self, amount: int) -> bool:
        """
        Deduct ``amount`` minor units from cash_balance.
        Returns ``False`` if funds are insufficient; never raises.
        """
        if amount <= 0:
            raise ValueError("Amount must be positive.")
        if amount > self._cash_balance:
            return False
        self._cash_balance -= amount
        return True

    # ── item management ───────────────────────────────────────────────────────

    def add_item(self, item: VaultItem) -> None:
        """
        Append a ``VaultItem`` to the in-memory list.
        Persistance (INSERT into vault_items) is the storage layer's job.
        """
        if not isinstance(item, VaultItem):
            raise TypeError(f"Expected VaultItem, got {type(item).__name__}.")
        self._items.append(item)

    def remove_item(self, item_id: int) -> VaultItem | None:
        """
        Remove and return the item with the given ``item_id``, or ``None`` if
        not found.  Deletion from the DB is the storage layer's job.
        """
        for i, item in enumerate(self._items):
            if item.item_id == item_id:
                return self._items.pop(i)
        return None

    def get_item(self, item_id: int) -> VaultItem | None:
        """Return the item with the given ``item_id``, or ``None``."""
        for item in self._items:
            if item.item_id == item_id:
                return item
        return None

    # ── info ──────────────────────────────────────────────────────────────────

    @property
    def vault_info(self) -> dict:
        return {
            "vault_no":              self._vault_no,
            "cash_balance_minor":    self._cash_balance,
            "item_count":            len(self._items),
            "total_est_value_minor": self.total_estimated_value,
        }

    # ── dunder ────────────────────────────────────────────────────────────────

    def __str__(self) -> str:
        return (
            f"Vault(no={self._vault_no!r}, "
            f"cash={self._cash_balance}, items={len(self._items)})"
        )

    def __repr__(self) -> str:
        return f"Vault({self._vault_no!r}, cash={self._cash_balance})"


# =============================================================================
#  Base Account
# =============================================================================

class Account:
    """
    Base account class.

    All monetary amounts are integers in the smallest currency unit
    (paise for BDT, cents for USD).  Passwords are stored as SHA-256
    digests — plaintext never lives inside this class.

    ``check_password`` accepts an optional ``salt`` so callers can pass the
    DB-stored salt for salted rows (Phase 4); omitting it falls back to
    treating the digest as unsalted.
    """

    def __init__(
        self,
        acc_num:              int,
        password_hash:        str,
        acc_balance:          int,
        currency:             str,
        is_frozen:            bool = False,
        daily_transfer_limit: int  = 500_000,
    ) -> None:
        self._acc_num              = int(acc_num)
        self._password_hash        = str(password_hash)
        self._acc_balance          = int(acc_balance)
        self._currency             = str(currency).upper()
        self._is_frozen            = bool(is_frozen)
        self._daily_transfer_limit = int(daily_transfer_limit)
        self.vault: Vault | None   = None

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def acc_num(self) -> int:
        return self._acc_num

    @property
    def acc_balance(self) -> int:
        return self._acc_balance

    @property
    def currency(self) -> str:
        return self._currency

    @property
    def is_frozen(self) -> bool:
        return self._is_frozen

    @is_frozen.setter
    def is_frozen(self, value: bool) -> None:
        self._is_frozen = bool(value)

    @property
    def daily_transfer_limit(self) -> int:
        return self._daily_transfer_limit

    @daily_transfer_limit.setter
    def daily_transfer_limit(self, value: int) -> None:
        if int(value) < 0:
            raise ValueError("Daily transfer limit cannot be negative.")
        self._daily_transfer_limit = int(value)

    # ── authentication ────────────────────────────────────────────────────────

    def check_password(self, plaintext: str, salt: str = "") -> bool:
        """
        Verify ``plaintext`` against the stored digest.
        Pass the DB ``acc_password_salt`` value for Phase 4 salted rows.
        """
        return verify_secret(plaintext, self._password_hash, salt)

    # ── fund movement ─────────────────────────────────────────────────────────

    def add_to_balance(self, amount: int) -> None:
        """Direct credit to account balance (minor units)."""
        if amount <= 0:
            raise ValueError("Amount must be positive.")
        self._acc_balance += amount

    def deduct_from_balance(self, amount: int) -> bool:
        """
        Deduct from balance.  Returns ``False`` if insufficient funds;
        never raises for ordinary insufficient-funds cases.
        """
        if amount <= 0:
            raise ValueError("Amount must be positive.")
        if amount > self._acc_balance:
            return False
        self._acc_balance -= amount
        return True

    def can_deduct_from_balance(self, amount: int) -> bool:
        return self._acc_balance >= amount

    # ── fee calculations ──────────────────────────────────────────────────────

    def get_monthly_maintenance_fee(self) -> int:
        """Base monthly maintenance fee in minor units."""
        return 50_000

    def get_debit_card_issuance_fee(self) -> int:
        """Base debit card issuance fee in minor units."""
        return 100_000

    # ── dunder ────────────────────────────────────────────────────────────────

    def __iadd__(self, amount: int) -> Account:
        self.add_to_balance(amount)
        return self

    def __isub__(self, amount: int) -> Account:
        self.deduct_from_balance(amount)
        return self

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Account):
            return self._acc_num == other._acc_num
        return False

    def __hash__(self) -> int:
        return hash(self._acc_num)

    def __repr__(self) -> str:
        return (
            f"Account({self._acc_num}, {self._currency}, "
            f"balance_minor={self._acc_balance}, frozen={self._is_frozen})"
        )

    def __str__(self) -> str:
        return str(self.account_info)

    # ── info ──────────────────────────────────────────────────────────────────

    @property
    def account_info(self) -> dict:
        vault_detail = (
            self.vault.vault_info
            if self.vault else "no vault"
        )
        return {
            "acc_num":           self._acc_num,
            "acc_type":          "account",
            "currency":          self._currency,
            "acc_balance_minor": self._acc_balance,
            "is_frozen":         self._is_frozen,
            "daily_limit_minor": self._daily_transfer_limit,
            "vault":             vault_detail,
        }


# =============================================================================
#  Non-Credit Card Account
# =============================================================================

class Non_Credit_Card(Account):
    """Standard account with no credit card facility."""

    @property
    def account_info(self) -> dict:
        info = super().account_info
        info["acc_type"] = "non_credit_card"
        return info

    def __repr__(self) -> str:
        return (
            f"Non_Credit_Card({self._acc_num}, {self._currency}, "
            f"balance_minor={self._acc_balance})"
        )

    def __str__(self) -> str:
        return str(self.account_info)


# =============================================================================
#  Student Account
# =============================================================================

class StudentAccount(Non_Credit_Card):
    """
    Tiered account profile for students with mandatory parent info and
    fee waivers.

    Minimum-deposit validation is intentionally absent here.  The caller
    (``main.py``'s account-creation flow) must check the deposit against
    ``utils_currency.min_deposit_for(currency, rate)`` *before* constructing
    this object, because the 100 BDT floor depends on the live exchange rate —
    something this class should not know about.
    """

    def __init__(
        self,
        acc_num:              int,
        password_hash:        str,
        initial_deposit_minor: int,
        currency:             str,
        parent_name:          str,
        parent_phone:         str,
        is_frozen:            bool = False,
        daily_transfer_limit: int  = 500_000,
    ) -> None:
        if not parent_name or not str(parent_name).strip():
            raise ValueError("Student accounts require a non-empty parent_name.")
        if not parent_phone or not str(parent_phone).strip():
            raise ValueError("Student accounts require a non-empty parent_phone.")

        super().__init__(
            acc_num,
            password_hash,
            initial_deposit_minor,
            currency,
            is_frozen,
            daily_transfer_limit,
        )
        self.parent_name  = str(parent_name).strip()
        self.parent_phone = str(parent_phone).strip()

    def get_monthly_maintenance_fee(self) -> int:
        return 0  # waived for student accounts

    def get_debit_card_issuance_fee(self) -> int:
        return 0  # waived for student accounts

    @property
    def account_info(self) -> dict:
        info = super().account_info
        info["acc_type"]    = "student"
        info["parent_name"] = self.parent_name
        info["parent_phone"] = self.parent_phone
        return info

    def __repr__(self) -> str:
        return (
            f"StudentAccount({self._acc_num}, {self._currency}, "
            f"balance_minor={self._acc_balance}, parent={self.parent_name!r})"
        )

    def __str__(self) -> str:
        return str(self.account_info)


# =============================================================================
#  Credit Card Account
# =============================================================================

class CreditCard(Account):
    """
    Account with an attached credit card.

    ``credit_used`` is loaded from the DB on every instantiation — never reset
    to 0 — so in-memory state accurately reflects outstanding credit.

    ``check_cc_pin`` accepts an optional ``salt`` for Phase 4 salted rows.
    """

    def __init__(
        self,
        acc_num:              int,
        password_hash:        str,
        acc_balance:          int,
        currency:             str,
        credit_card_num:      str,
        cc_pin_hash:          str,
        credit_card_limit:    int,
        credit_used:          int  = 0,
        is_frozen:            bool = False,
        daily_transfer_limit: int  = 500_000,
    ) -> None:
        super().__init__(
            acc_num, password_hash, acc_balance, currency,
            is_frozen, daily_transfer_limit,
        )
        self._credit_card_num   = str(credit_card_num)
        self._cc_pin_hash       = str(cc_pin_hash)
        self._credit_card_limit = int(credit_card_limit)
        self._credit_used       = int(credit_used)

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def credit_card_num(self) -> str:
        return self._credit_card_num

    @property
    def credit_card_limit(self) -> int:
        return self._credit_card_limit

    @credit_card_limit.setter
    def credit_card_limit(self, value: int) -> None:
        if int(value) < 0:
            raise ValueError("Credit limit cannot be negative.")
        self._credit_card_limit = int(value)

    @property
    def credit_used(self) -> int:
        return self._credit_used

    @property
    def credit_available(self) -> int:
        return self._credit_card_limit - self._credit_used

    # ── authentication ────────────────────────────────────────────────────────

    def check_cc_pin(self, plaintext: str, salt: str = "") -> bool:
        """
        Verify ``plaintext`` PIN against the stored digest.
        Pass the DB ``credit_card_pin_salt`` value for Phase 4 salted rows.
        """
        return verify_secret(plaintext, self._cc_pin_hash, salt)

    # ── credit operations ─────────────────────────────────────────────────────

    def can_charge_credit(self, amount: int) -> bool:
        return self._credit_used + amount <= self._credit_card_limit

    def charge_credit(self, amount: int) -> bool:
        """
        Charge ``amount`` to the credit card.
        Returns ``False`` if the limit would be exceeded; never raises.
        """
        if amount <= 0:
            raise ValueError("Amount must be positive.")
        if not self.can_charge_credit(amount):
            return False
        self._credit_used += amount
        return True

    def payback_credit(self, amount: int) -> int:
        """
        Reduce ``credit_used`` by ``amount`` (or by the full outstanding
        balance if ``amount`` > ``credit_used``).
        Returns the actual amount paid back (≤ ``amount``).
        """
        if amount <= 0:
            raise ValueError("Amount must be positive.")
        paid = min(self._credit_used, amount)
        self._credit_used -= paid
        return paid

    # ── info ──────────────────────────────────────────────────────────────────

    @property
    def account_info(self) -> dict:
        info = super().account_info
        info.update({
            "acc_type":          "credit_card",
            "credit_card_num":   self._credit_card_num,
            "credit_card_limit": self._credit_card_limit,
            "credit_used":       self._credit_used,
            "credit_available":  self.credit_available,
        })
        return info

    # ── dunder ────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        masked = self._credit_card_num[-4:] if self._credit_card_num else "????"
        return (
            f"CreditCard({self._acc_num}, {self._currency}, "
            f"balance_minor={self._acc_balance}, cc_last4={masked})"
        )

    def __str__(self) -> str:
        return str(self.account_info)


# =============================================================================
#  Payment Processor (in-memory OOP path — DB persistence is utils_storage)
# =============================================================================

class Payment_Processor:
    """
    Handles in-memory fund movement between Account objects.

    This class operates purely on OOP objects.  DB persistence is the
    responsibility of ``utils_storage``.  No ``print()`` or ``input()`` calls.
    All amounts are in minor units (paise / cents).
    Cross-currency transfers auto-convert via ``convert_minor()``.
    """

    # ── single-account operations ─────────────────────────────────────────────

    def add_funds(self, account: Account, amount: int) -> bool:
        """Direct deposit to account balance.  Returns True on success."""
        if account.is_frozen:
            return False
        account.add_to_balance(amount)
        return True

    def deduct_funds(self, account: Account, amount: int) -> bool:
        """
        Direct deduction from account balance.
        Returns False if frozen or insufficient funds.
        """
        if account.is_frozen:
            return False
        return account.deduct_from_balance(amount)

    def deduct_via_credit(self, cc: CreditCard, amount: int) -> bool:
        """Charge ``amount`` to a credit card.  Returns False if limit exceeded."""
        if cc.is_frozen:
            return False
        return cc.charge_credit(amount)

    # ── transfer helpers ──────────────────────────────────────────────────────

    def _execute_send(
        self,
        sender:     Account,
        amount:     int,
        via_credit: bool,
    ) -> bool:
        """Deduct from sender (balance or credit).  Returns success."""
        if via_credit:
            if not isinstance(sender, CreditCard):
                return False
            return self.deduct_via_credit(sender, amount)
        return self.deduct_funds(sender, amount)

    def _execute_receive(
        self,
        receiver:        Account,
        amount:          int,
        sender_currency: str,
    ) -> None:
        """Credit receiver, auto-converting currency if needed."""
        recv_amount = convert_minor(amount, sender_currency, receiver.currency)
        receiver.add_to_balance(recv_amount)

    # ── 1-to-1 transfer ───────────────────────────────────────────────────────

    def transfer_1to1(
        self,
        sender:     Account,
        receiver:   Account,
        amount:     int,
        via_credit: bool = False,
    ) -> bool:
        """Transfer a fixed amount from one account to another."""
        if sender.is_frozen or receiver.is_frozen:
            return False
        ok = self._execute_send(sender, amount, via_credit)
        if ok:
            self._execute_receive(receiver, amount, sender.currency)
        return ok

    # ── 1-to-many transfer ────────────────────────────────────────────────────

    def transfer_1tomany(
        self,
        sender:     Account,
        receivers:  list[Account],
        amount:     int,
        via_credit: bool = False,
    ) -> bool:
        """Transfer the same amount from one sender to multiple receivers."""
        if sender.is_frozen:
            return False
        for rec in receivers:
            if not self.transfer_1to1(sender, rec, amount, via_credit):
                return False
        return True

    # ── many-to-1 transfer ────────────────────────────────────────────────────

    def transfer_manyto1(
        self,
        senders:    list[Account],
        receiver:   Account,
        amount:     int,
        via_credit: bool = False,
    ) -> bool:
        """Each sender sends the same amount to one receiver."""
        if receiver.is_frozen:
            return False
        for sen in senders:
            if not self.transfer_1to1(sen, receiver, amount, via_credit):
                return False
        return True

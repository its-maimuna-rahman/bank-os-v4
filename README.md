# 🏦 BankOS v4 — Enterprise Financial System (Unified Storage + Dual Front Ends)

BankOS v4 keeps all operational and audit data in one SQLite database (`data/bank.db`) and exposes two interfaces over the same core modules:

- **Terminal CLI** in `main.py`
- **Streamlit web app** in `app.py`

Core banking features include account lifecycle operations, vault cash + physical asset handling, credit-card workflows, frozen-account controls, pending transfer approval, utility/tax settlement, loans/bonds/checks, password reset, ingestion/export, and PDF statements.

> **v4 documentation refresh:** this README updates stale v3 wording and aligns schema/flows/menu labels with the current implementation.

---

## 📁 Core System Architecture

```
bank_system/
├── config.json              # Active DB path + live usd_to_bdt_rate
├── main.py                  # Terminal CLI (admin/customer flows)
├── app.py                   # Streamlit web interface (admin/customer portals)
├── account_cls.py           # OOP account/vault/payment objects used by storage flows
├── utils_storage.py         # Unified SQLite schema, auth, CRUD, transfers, logs,
│                            # pending approvals, import/export, billers, vault ops
├── utils_logic.py           # Loan/bond/check business logic handlers
├── utils_currency.py        # BDT/USD conversion, minor-unit helpers, live rate lookup
├── utils_security.py        # Salted hashing + lockout timing helpers
├── utils_reports.py         # PDF statement generator (fpdf2)
├── utils_validinput.py      # CLI validators and prompt wrappers
├── data/bank.db             # Central persistent SQLite database
└── statements/              # Generated PDF statements
```

### Architectural notes (current behavior)

- Storage is **unified** in one database file (`data/bank.db`), not split per log table.
- `ensure_log_db()` currently returns `(conn, conn, conn, conn)` to that same central DB.
- Some comments/docstrings still mention legacy multi-DB naming (`acc`, `tx`, `freeze`, `pending`) for compatibility, but writes/reads target unified tables in `bank.db`.
- The codebase is modular, but the system is not fully “UI-last/fully decoupled” in a strict architectural sense; both front ends call shared storage functions directly.

---

## 📊 Relational Database Schema (Unified `data/bank.db`)

All business data, audit logs, and transfer-review state are persisted in a single SQLite file.
`PRAGMA foreign_keys = ON` is set on each connection in `init_storage()`.

```
                         ┌───────────────┐
                         │    admins     │
                         │ (PK) admin_id │
                         └───────────────┘

                   ┌────────────────────────┐
                   │        accounts        │
                   │      (PK) acc_num      │
                   └───────────┬────────────┘
                               │
    ┌──────────┬───────────────┼───────────────┬──────────────┬────────────┐
    ▼          ▼               ▼               ▼              ▼            ▼
 ┌────────┐ ┌───────┐   ┌──────────────┐ ┌──────────────┐ ┌─────────┐ ┌────────────┐
 │ vaults │ │ loans │   │    bonds     │ │    checks    │ │ billers │ │ account_log│
 └───┬────┘ └───────┘   └──────────────┘ └──────────────┘ │(FK→acc) │ └────────────┘
     │                                                     └─────────┘
     ▼
 ┌──────────────┐   ┌──────────────┐   ┌───────────────────┐   ┌──────────────────┐
 │ vault_items  │   │  freeze_log  │   │  transaction_log   │   │ pending_transfers │
 └──────────────┘   └──────────────┘   └───────────────────┘   └──────────────────┘
```

### 0. `admins`

- `admin_id` (PK autoincrement), `username` (unique)
- `password_hash`, `password_salt`, optional `original_password`
- `security_question`, `security_answer_hash`, `security_answer_salt`

`initialize_schema()` ensures a default `bankadmin` admin row exists.

### 1. Core customer/system entity tables

#### `accounts`

Key columns:

- Identity/auth: `acc_num`, `acc_password_hash`, `acc_password_salt`, security-question fields
- Money/currency: `acc_balance` (minor-unit integer), `currency` (`BDT`/`USD`)
- Type: `acc_type` (`credit_card`, `non_credit_card`, `student`, `utility_biller`, `tax_authority`)
- Controls: `daily_transfer_limit`, `is_frozen`, `failed_login_attempts`, `locked_until`
- Card fields: debit and credit card numbers + salted PIN hashes
- Credit tracking: `credit_card_limit`, `credit_used`
- Vault linkage: `vault_no`, `vault_password_hash`, `vault_password_salt`, `vault_balance`
- Student fields: `parent_name`, `parent_phone`, fee waiver flags

All balances/limits are stored in minor units (paisa/cents).

#### `billers`

- `biller_id` (PK), `biller_name`, `biller_category` (`gas`, `electric`, `water`, `tax`)
- `receiving_acc_num` (FK to `accounts`)

Seeded system receiving accounts include Titas Gas, DESCO, WASA, and National Board of Revenue.

#### `vaults`

- `vault_no` (PK), `acc_num` (FK), `cash_balance`
- `vault_password_hash`, `vault_password_salt`

#### `vault_items`

- `item_id` (PK), `vault_no`
- `item_type` (`gold`, `paper_deeds`, `corporate_bonds`, `heirlooms`)
- `description`, `est_value_minor`, `added_at`

### 2. Financial instrument tables

#### `loans`

- `loan_id` (PK), `acc_num` (FK)
- `principal`, `remaining_balance` (minor units)
- `interest_rate`, `status` (`active`, `settled`), `disbursed_at`, `next_payment_due`

Loan disbursal credits account balance; repayment debits balance and reduces remaining balance.

#### `bonds`

- `bond_id` (PK), `acc_num` (FK)
- `face_value`, `yield_rate`, `maturity_timestamp`, `status` (`active`, `redeemed`), `purchased_at`

Purchase debits face value; redemption after maturity credits `round(face_value * (1 + yield_rate))`.

#### `checks`

- `check_id` (PK), `acc_num` (FK)
- `amount`, `payee_name`, `memo`, `status` (`issued`, `cleared`, `bounced`), `issued_at`

Issued checks debit funds immediately as held amount. Clearing updates status only; bouncing refunds.

### 3. Audit and operational logs

#### `account_log`

- `id` (PK), optional `log_id`, `timestamp`, `acc_num`, `action`, `action_type`, `details`, `activity_details`

#### `freeze_log`

- `id` (PK), optional `log_id`, `timestamp`, `acc_num`, `action`, `status_change`, `reason`, `details`, `is_active`

#### `transaction_log`

- `id` (PK), optional `tx_id`, `timestamp`, `acc_num`
- `type`/`tx_type`, `amount`, `currency`, `category`, `status`, `balance_after`

#### `pending_transfers`

- `id` (PK), optional `transfer_id`, `timestamp`
- `sender_acc_num`, `receiver_acc_num`, `amount`, `currency`, `via_credit`
- `status` (`pending`, `approved`, `rejected`), `reviewed_at`

Large transfers are queued here for admin review based on currency-specific thresholds.

---

## 🔐 Security Notes

- Passwords, security answers, vault passwords, and card PINs use salted SHA-256 (`hash_secret()` / `verify_secret()`).
- Random salt is generated per secret (`secrets.token_hex(16)`).
- Customer login lockout is enforced after 3 failed attempts (`MAX_ATTEMPTS = 3`) for 2 minutes (`LOCKOUT_MINUTES = 2`).
- Vault operation gating supports bounded attempts (`VAULT_MAX_TRIES = 3`) in CLI flow.
- Self-service password reset exists for both customers and admins using security-question verification.
- `freeze_account()` state blocks restricted operations (transfers/withdrawals/payment flows).

### SQL import safety

`.sql` ingestion is blocked if script content includes `DROP`, `ALTER`, `ATTACH`, `DETACH`, `PRAGMA`, or `VACUUM`.
Allowed scripts run in a transaction with rollback on failure.

---

## 🛠️ Operational Interfaces (v4)

Both interfaces run against the same storage layer (`utils_storage.py`) and central DB.

### 1) Terminal CLI (`main.py`)

#### Login menu

```text
──────────  BankOS — Login  ──────────
  [1] Admin login
  [2] Customer login
  [3] Forgot Password
  [4] Exit
────────────────────────────────────────
```

#### Admin menu (9 operations + logout)

```text
──────────  Admin Menu  ──────────
  [1] Account Management
  [2] Vault Management
  [3] Show Accounts
  [4] View Logs
  [5] Bank Overview
  [6] Freeze / Unfreeze Account
  [7] Pending Transfer Approval
  [8] Generate PDF Statement
  [9] Database Management
  [0] Logout
────────────────────────────────────────
```

Admin Database Management submenu:

```text
  [1] Export accounts (XLSX / CSV)
  [2] View all tables (row counts)
  [3] Seed sample data
  [4] Import data in the existing tables
  [0] Back
```

#### Customer menu (11 operations)

```text
──────────  Customer Menu — Acc <id>  ──────────
  [1] View account details
  [2] Add / Deduct balance
  [3] Transfer
  [4] Manage vault
  [5] Payback credit card
  [6] Last 10 transactions
  [7] Generate PDF statement
  [8] Financial Instruments
  [9] Utility Bill Payment
  [10] Government Tax Settlement
  [11] Logout
────────────────────────────────────────
```

Notable CLI submenus:

- Transfer modes: `1→1`, `1→many`, `many→1`
- Financial instruments: loan disbursal/repayment, bond purchase/redemption, check issue/clear/bounce
- Utility/tax routing via seeded billers
- Forgot password flow for customer or admin

### 2) Streamlit Web App (`app.py`)

#### Authentication tabs

- **Customer Login**
- **Bank Admin Login**
- **Reset Password** (customer/admin toggle)

#### Admin portal navigation

- `📊 Overview`
- `👥 Account Management`
- `🔐 Vault Management`
- `⏳ Pending Transfers`
- `📜 Audit & Transaction Logs`
- `🗄️ Database Management`
- `📄 Generate Statements`

#### Customer portal navigation

- `🏠 Account Overview`
- `💰 Deposit & Withdraw`
- `💸 Transfer Funds`
- `💳 Credit Card Payback` (shown for credit-card accounts)
- `🔐 Vault Operations`
- `📈 Financial Instruments`
- `💡 Utility Bill Payment`
- `🏛️ Government Tax Settlement`
- `📜 Recent Transactions`
- `📄 PDF Statement`

---

## 💱 Currency, Transfers, and Account Rules

- Supported currencies: **BDT** and **USD**.
- Monetary storage is integer minor units (paise/cents).
- Live FX conversion uses `config.json -> usd_to_bdt_rate` on each lookup.
- Cross-currency transfers auto-convert receiver credit (`convert_minor`).
- Daily outbound transfer cap enforced per account (`daily_transfer_limit`).
- Threshold-based pending review (`PENDING_THRESHOLD`):
  - USD: `100_000` minor (1,000.00 USD)
  - BDT: `12_000_000` minor (120,000.00 BDT)
- Credit-card transfers/checkouts can consume `credit_used` up to `credit_card_limit`.
- Student operations include parent-contact metadata and mock SMS telemetry payloads for qualifying events.

---

## 🎓 Student Account Rules

- Student account type requires `parent_name` and `parent_phone`.
- Minimum opening deposit is **currency-aware**:
  - baseline floor is 100 BDT (`MIN_DEPOSIT_BDT_MINOR = 10000`)
  - USD minimum is calculated from live exchange rate
- Student creation enforces fee-waiver fields:
  - `maintenance_fee_waived = 1`
  - `sms_alert_fee_waived = 1`
  - `card_issuance_fee_waived = 1`
- Deduct/transfer paths can include parent notification payloads (`sms_telemetry`) for front-end display.

---

## 🔐 Vault Cash & Physical Assets

- Vault credentials are salted/hashed (`vault_password_hash` + `vault_password_salt`).
- Vault supports cash movements and physical asset records.
- Physical asset categories are constrained to:
  - `gold`
  - `paper_deeds`
  - `corporate_bonds`
  - `heirlooms`
- Vault destruction can optionally transfer vault cash back to account balance.

---

## 📈 Loans, Bonds, Checks, Utility Bills, Taxes

- **Loans**: disburse to account, repay from account, track status and remaining balance.
- **Bonds**: purchase debits account, redemption available only at/after maturity.
- **Checks**: issue (debit/hold), clear (status update), bounce (refund).
- **Utilities/Tax**: payments route to seeded biller/tax authority receiving accounts and log by category.

---

## 🧾 Reporting and Auditability

- Transaction, account, freeze, and pending-transfer records are queryable via admin interfaces.
- `utils_reports.py` generates PDF account statements (up to 50 recent transactions).
- Statements include account summary, limits, freeze state, vault/credit details where applicable.

---

## 📦 Ingestion & Export Engine

### Import formats (`import_file_to_table`)

- CSV
- XLSX
- JSON
- XML
- SQL (guarded by blocked-statement filter)

Behavior notes:

- CSV/XLSX/XML tolerate unknown source fields (non-matching keys dropped).
- JSON import is strict about unknown columns.
- SQL scripts are executed transactionally after blocked-keyword check.
- Import targets existing schema tables discovered dynamically from `sqlite_master`.

### Export formats

- Accounts export to **CSV** and **XLSX** (CLI + Streamlit admin workflows).

---

## ⚙️ Persistence & Runtime Configuration

- Active DB and FX rate are in `config.json`:

```json
{
  "active_database_path": "data/bank.db",
  "usd_to_bdt_rate": 119.9
}
```

- CLI boot path checks config and DB path state before starting login routing.
- Streamlit initializes storage/session connections on app startup.

---

## CHANGELOG — v3 → v4 implementation alignment

1. Updated repository title and version references to **v4**.
2. Corrected interface documentation to include **both** front ends (`main.py` CLI + `app.py` Streamlit).
3. Corrected CLI menu counts/labels (login/admin/customer/database-management) to match current `main.py` output.
4. Corrected schema documentation to match actual table/column names (e.g., `check_id`, `sender_acc_num`, `vault_balance`, lockout fields).
5. Documented true persistence behavior: unified `data/bank.db`; `ensure_log_db()` returns central DB connections despite legacy naming.
6. Added current BDT/USD minor-unit model, live FX from `config.json`, cross-currency conversion, pending-threshold review, and daily-limit enforcement.
7. Updated student-account rules with parent metadata + currency-aware minimum opening deposit.
8. Documented credit-card, vault cash/physical assets, freeze controls, and password/PIN/vault salted hashing as implemented.
9. Synced ingestion/export section with actual CSV/XLSX/JSON/XML/SQL import behavior and CSV/XLSX account export paths.
10. Removed stale “new/fix pass” and over-strong decoupling claims that no longer describe the current code accurately.

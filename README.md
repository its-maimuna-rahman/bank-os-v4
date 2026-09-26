# 🏦 BankOS v3 — Enterprise Financial System (Unified Architecture)

BankOS v3 consolidates every legacy log database from v2 into one relational
SQLite instance, adds a persistent connection layer, multi-format bulk
ingestion, tiered/student accounts with parental monitoring, and a full set
of financial instruments (cards, loans, bonds, checks, utility billing, tax
settlement). The CLI is fully decoupled from business logic so a UI layer
can be added last without touching core functions.

> **v3 correction pass:** this README fixes 8 gaps found in the original
> `implementation_outline.md` — see `CHANGELOG` at the bottom.

---

## 📁 Core System Architecture

```
bank_system/
├── config.json              # Persistent app state (active db path, FX rate)
├── main.py                  # Orchestration, session routers, CLI layouts
├── account_cls.py           # Account hierarchy (Base, Student, CreditCard...)
├── utils_storage.py         # SQLite connection layer, schema, execution wrappers,
│                             # and multi-format (csv/xlsx/json/xml/sql) import adapters
├── utils_logic.py           # Loans, bonds, checks, utility/tax settlement math
├── utils_currency.py        # FX rate lookups / conversions (BDT <-> USD)
├── utils_security.py        # Hashing (salted), password reset, admin auth
├── utils_reports.py         # PDF statement generation
├── utils_validinput.py      # Input scrubbers and exception handlers
└── statements/               # Generated PDF statements
```

> Ingestion adapters live inside `utils_storage.py` alongside the schema
> and connection layer rather than a separate file — keeps table
> definitions and their import logic next to each other, and matches your
> existing file layout (no `utils_ingest.py`).

---

## 📊 Relational Database Schema (Unified `bank.db`)

All financial data, audit ledgers, and operational tables live in one file.
`PRAGMA foreign_keys = ON;` is set on **every** connection open (not just
once at startup) since SQLite enforces it per-connection.

```
                        ┌───────────────┐
                        │    admins     │
                        │ (PK) admin_id │
                        └───────────────┘

                  ┌────────────────────────┐
                  │        accounts        │
                  │  (PK) acc_num          │
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

### 0. `admins` *(new — fixes hardcoded admin password)*
* `admin_id`: `INTEGER` (**PRIMARY KEY** AUTOINCREMENT)
* `username`: `TEXT` (**UNIQUE**)
* `password_hash`: `TEXT` (salted hash — see Security section)
* `password_salt`: `TEXT`
* `security_question`: `TEXT`
* `security_answer_hash`: `TEXT` (salted)
* `security_answer_salt`: `TEXT`

Seed one default admin row on first `Create db` run instead of hardcoding
the password in `main.py`. Admin password reset then uses the same
security-question flow as customers, no separate "terminal deployment"
step required.

### 1. Primary Entity Tables

#### `accounts`
* `acc_num`: `INTEGER` (**PRIMARY KEY**)
* `acc_password_hash`, `acc_password_salt`: `TEXT`
* `security_question`: `TEXT`
* `security_answer_hash`, `security_answer_salt`: `TEXT`
* `acc_balance`: `INTEGER` (minor units: paisa/cents)
* `currency`: `TEXT` (`BDT` or `USD`)
* `acc_type`: `TEXT` (`credit_card`, `non_credit_card`, `student`, `utility_biller`, `tax_authority`)
* `daily_transfer_limit`: `INTEGER`
* `is_frozen`: `INTEGER` (0/1)
* `debit_card_num`, `debit_card_pin_hash`: `TEXT` (nullable)
* `credit_card_num`, `credit_card_pin_hash`: `TEXT` (nullable)
* `credit_card_limit`, `credit_used`: `INTEGER`
* `parent_name`, `parent_phone`: `TEXT` (nullable, required for `student`)
* `maintenance_fee_waived`: `INTEGER` (0/1, default 0) *(new)*
* `sms_alert_fee_waived`: `INTEGER` (0/1, default 0) *(new)*
* `card_issuance_fee_waived`: `INTEGER` (0/1, default 0) *(new)*

> `acc_type = 'utility_biller'` and `'tax_authority'` are internal
> corporate accounts (Titas Gas, DESCO, WASA, NBR) that customer payments
> route into. See `billers` below.

#### `billers` *(new — fixes missing settlement destinations)*
* `biller_id`: `INTEGER` (**PRIMARY KEY** AUTOINCREMENT)
* `biller_name`: `TEXT` (e.g. `"Titas Gas"`, `"DESCO"`, `"WASA"`, `"NBR"`)
* `biller_category`: `TEXT` (`gas`, `electric`, `water`, `tax`)
* `receiving_acc_num`: `INTEGER` (**FOREIGN KEY** → `accounts(acc_num)`)

Utility/tax payment = standard transfer from customer `acc_num` to the
biller's `receiving_acc_num`, logged in `transaction_log` with
`tx_type = 'utility_pay'` / `'tax_pay'`.

#### `vaults`
* `vault_no`: `TEXT` (**PRIMARY KEY**)
* `acc_num`: `INTEGER` (**FOREIGN KEY** → `accounts(acc_num)` ON DELETE CASCADE)
* `vault_password_hash`, `vault_password_salt`: `TEXT`
* `cash_balance`: `INTEGER`

#### `vault_items`
* `item_id`: `INTEGER` (**PRIMARY KEY** AUTOINCREMENT)
* `vault_no`: `TEXT` (**FOREIGN KEY** → `vaults(vault_no)` ON DELETE CASCADE)
* `item_type`: `TEXT` (`gold`, `paper_deeds`, `corporate_bonds`, `heirlooms`)
* `description`: `TEXT`
* `est_value`: `INTEGER`

### 2. Financial Instrument Tables

#### `loans`
* `loan_id`: `INTEGER` (**PRIMARY KEY** AUTOINCREMENT)
* `acc_num`: `INTEGER` (**FOREIGN KEY** → `accounts(acc_num)`)
* `principal`, `remaining_balance`: `INTEGER`
* `interest_rate`: `REAL` (annual %, decimal)
* `next_payment_due`: `TEXT` (ISO 8601)
* `status`: `TEXT` (`active`, `settled`, `defaulted`)

Disbursal credits `principal` to `acc_balance` and logs a `transaction_log`
row (`tx_type = 'loan_disbursal'`). Each repayment debits `acc_balance`,
reduces `remaining_balance`, and logs `tx_type = 'loan_repayment'`.

#### `bonds`
* `bond_id`: `INTEGER` (**PRIMARY KEY** AUTOINCREMENT)
* `acc_num`: `INTEGER` (**FOREIGN KEY** → `accounts(acc_num)`)
* `face_value`: `INTEGER`
* `yield_rate`: `REAL`
* `maturity_timestamp`: `TEXT` (ISO 8601)
* `status`: `TEXT` (`active`, `redeemed`)

Purchase debits `face_value` from `acc_balance`. Redemption (only allowed
once `maturity_timestamp <= now`) credits `face_value * (1 + yield_rate)`
back to `acc_balance`.

#### `checks` *(lifecycle now defined — fixes undefined balance movement)*
* `check_num`: `TEXT` (**PRIMARY KEY**, format `CHK-XXXXXX`)
* `acc_num`: `INTEGER` (**FOREIGN KEY** → `accounts(acc_num)`)
* `payee_name`: `TEXT`
* `amount`: `INTEGER`
* `status`: `TEXT` (`issued`, `cleared`, `bounced`)

Balance rule: `amount` is debited from `acc_balance` **at issuance**
(treated as a hold). On `cleared`, the debit is finalized (no further
balance change). On `bounced`, the held amount is credited back and a
`transaction_log` reversal row is written.

### 3. Audit Log Tables

#### `account_log`
* `log_id`: `INTEGER` (**PRIMARY KEY** AUTOINCREMENT)
* `acc_num`: `INTEGER` (**FOREIGN KEY** → `accounts(acc_num)`)
* `action_type`: `TEXT` (`create`, `password_reset`, `type_conversion`)
* `timestamp`: `TEXT`

#### `freeze_log`
* `log_id`: `INTEGER` (**PRIMARY KEY** AUTOINCREMENT)
* `acc_num`: `INTEGER` (**FOREIGN KEY** → `accounts(acc_num)`)
* `status_change`: `TEXT` (`frozen`, `unfrozen`)
* `reason`: `TEXT`
* `timestamp`: `TEXT`

#### `transaction_log`
* `tx_id`: `INTEGER` (**PRIMARY KEY** AUTOINCREMENT)
* `acc_num`: `INTEGER` (**FOREIGN KEY** → `accounts(acc_num)`)
* `tx_type`: `TEXT` (`deposit`, `withdrawal`, `transfer_out`, `transfer_in`, `utility_pay`, `tax_pay`, `loan_disbursal`, `loan_repayment`, `bond_purchase`, `bond_redemption`, `check_issued`, `check_bounced_reversal`)
* `amount`, `balance_after`: `INTEGER`
* `currency`: `TEXT`
* `category`: `TEXT` (`food`, `bills`, `shopping`, `transfer`, `tax`, `loan`, `bond`, `check`, `other`)
* `status`: `TEXT` (`completed`, `pending`, `rejected`)
* `timestamp`: `TEXT`

#### `pending_transfers`
* `transfer_id`: `INTEGER` (**PRIMARY KEY** AUTOINCREMENT)
* `sender_acc`, `receiver_acc`: `INTEGER` (**FOREIGN KEY** → `accounts(acc_num)`)
* `amount`: `INTEGER`
* `currency`: `TEXT`
* `timestamp`: `TEXT`

---

## 🔐 Security Notes *(fixes unsalted hashing)*

* Every password / security-answer hash gets its own random salt
  (`secrets.token_hex(16)`), stored alongside the hash. Hash as
  `SHA256(salt + plaintext)` at minimum, or `hashlib.pbkdf2_hmac` for
  better resistance to brute force.
* `security_question` stays plaintext (it's a prompt, not a secret);
  only the **answer** is salted + hashed.
* Admin credentials live in the `admins` table, not in source code.
  First-run `Create db` seeds one admin row and forces an immediate
  password change.

## 🔐 SQL Import Safety *(fixes arbitrary code execution risk)*

`.sql` file imports are **not** run with a raw `executescript()` call.
Before execution:
1. Parse statements individually.
2. Reject the file if it contains `DROP`, `ALTER`, `ATTACH`, `DETACH`,
   `PRAGMA`, or `VACUUM` (case-insensitive).
3. Run the remaining `INSERT` statements inside a single transaction with
   `rollback()` on any failure, so a bad file never leaves the db half-updated.

---

## 🛠️ Complete Operational CLI Map

### 1. Boot Verification State
```
python3 main.py
=== BANK MANAGEMENT SYSTEM ===

** No database connected **

[1] Admin login
[2] Customer login
[3] Exit
────────────────────────────────────────
Select:
```
The connected db path persists in `config.json` across process restarts —
once connected, it stays connected until explicitly disconnected via
Database Management → `[3] Disconnect db`.

### 2. Admin Interface Layout
```
────────── Admin Menu ──────────
[1] Account Management
[2] Vault Management
[3] Show Accounts
[4] View Logs
[5] Bank Overview
[6] Freeze / Unfreeze Account
[7] Pending Transfer Approval
[8] Generate PDF Statement
[9] Database management
[0] Logout
────────────────────────────────────────
Select: 9

────────────  Database Management ─────────────
[1] Create db
[2] Connect Existing db
[3] Disconnect db
[4] Import data in the existing tables
[5] Go back
────────────────────────────────────────
Select: 4

─────────────── Select Table  ────────────────
[1]  accounts
[2]  billers
[3]  vaults
[4]  vault_items
[5]  loans
[6]  bonds
[7]  checks
[8]  account_log
[9]  freeze_log
[10] transaction_log
[11] pending_transfers
────────────────────────────────────────
Select: 1

─────────────── Select Option  ───────────────
[1] Import xlsx file
[2] Import csv file
[3] Import json file
[4] Import xml file
[5] Import sql script
────────────────────────────────────────
Select: 3
Enter .json file name: bulk_migration.json
```
*(This is the corrected table list — the original build only showed 4 of
the 11 importable tables.)*

### 3. Customer Interface Layout
```
────────── Customer Menu ──────────
[1]  View Account Details
[2]  Add / Deduct Balance
[3]  Initiate Transfer
[4]  Manage Physical Vault Items
[5]  Card Operations (Debit/Credit)
[6]  Utility Bill Payment
[7]  Government Tax Settlement
[8]  Loan Portfolio Management
[9]  Bond Investment Ledger
[10] Checkbook Registry
[11] Last 10 Transactions
[12] Generate PDF Statement
[13] Reset Account Security Password
[0]  Logout
────────────────────────────────────────
Select:
```

---

## 📦 Ingestion Engine Data Specs

* **CSV / XLSX** — flat row maps straight onto the target table's columns.
* **JSON** — array of objects; each object's keys map to column names.
* **XML** — one `<row>` (or table-named) element per record, child tags map to columns.
* **SQL** — `INSERT`-only scripts, validated and run transactionally (see Security Notes).

---

## 🎓 Student Account Rules

* Minimum initial deposit: **100 BDT, or its USD equivalent** at the
  `usd_to_bdt_rate` stored in `config.json` — not a flat 100 regardless of
  currency.
* `maintenance_fee_waived`, `sms_alert_fee_waived`,
  `card_issuance_fee_waived` set to `1` at account creation.
* Requires `parent_name` and `parent_phone` at signup.
* Every balance-changing transaction on a student account triggers a
  mock SMS log and writes an entry to `transaction_log` as normal — the
  notification itself is print-only (no real SMS gateway), but the
  underlying transaction is always fully logged for audit purposes.

---

## CHANGELOG — v2 → v3 corrections

1. Consolidated 5 separate `.db` files into one `bank.db` with FK constraints.
2. Added `admins` table — removes hardcoded `bankadmin123`.
3. Added `billers` table — gives utility/tax payments a real destination account.
4. Import "Select Table" menu now lists all 11 tables, not 4.
5. All password/security-answer hashes are now salted.
6. `.sql` import restricted to validated `INSERT`-only, transactional execution.
7. Student minimum deposit is now currency-aware.
8. Added fee-waiver columns so Phase 3's "waive fees" logic has something to set.
9. Check lifecycle (`issued` → `cleared`/`bounced`) now has explicit balance rules.
10. Loan/bond disbursal, repayment, and redemption now explicitly move `acc_balance`.

"""
app.py — BankOS Enterprise Web Application
Full Streamlit UI for BankOS v3/v4.

Features:
- Role-based Authentication: Bank Admin (bankadmin/bankadmin), Customer Login, Self-service Password Reset.
- Practice Project Credentials Display: easily inspect demo customer passwords and original passwords.
- Admin Portal: Dashboard Overview, Comprehensive Account CRUD (Create, Freeze, Convert, Daily Limit, Delete),
  Vault Management, Pending Transfers Approval/Rejection Queue, Multi-Log Audit Viewer,
  Unified Database Management (Table Inspector, Schema Viewer, Multi-Format Importer, CSV/XLSX Exporter),
  and PDF Statement Generation.
- Customer Portal: Real-time Account Overview (with dual-currency converted values), Deposit & Withdraw,
  Cross-currency & Threshold-checked Transfers, Credit Card Payback, Secured Vault Operations (Cash & Physical Assets),
  Financial Instruments (Loans, Bonds, Checks), Utility Bill & Government Tax Settlements,
  Transaction History, and Instant PDF Statement Downloads.
"""

from __future__ import annotations

import os
import tempfile
import sqlite3
from datetime import datetime, timedelta
from typing import NamedTuple
import pandas as pd
import streamlit as st

from utils_storage import (
    init_storage,
    ensure_log_db,
    authenticate_admin,
    authenticate_customer,
    get_security_question,
    execute_password_reset,
    list_accounts,
    bank_overview,
    create_account,
    delete_account,
    freeze_account,
    set_daily_limit,
    convert_account_type,
    create_vault,
    destroy_vault,
    vault_add,
    vault_deduct,
    add_vault_item,
    add_funds,
    deduct_funds,
    transfer,
    payback_credit,
    get_pending_transfers,
    review_pending,
    get_logs,
    recent_transactions,
    get_billers_by_category,
    pay_biller,
    get_all_table_names,
    import_file_to_table,
    export_accounts_csv,
    export_accounts_xlsx,
    execute_loan_disbursal,
    execute_loan_repayment,
    execute_bond_purchase,
    execute_bond_redemption,
    execute_check_issuance,
    execute_check_clearing,
    execute_check_bouncing,
    verify_vault_auth,
)
from utils_currency import (
    format_money,
    format_money_dual,
    to_minor,
    from_minor,
    convert_minor,
    get_usd_to_bdt_rate,
    min_deposit_for,
    SUPPORTED,
)
from utils_reports import generate_pdf_statement


class Logs(NamedTuple):
    acc: sqlite3.Connection
    tx: sqlite3.Connection
    freeze: sqlite3.Connection
    pending: sqlite3.Connection


def init_dbs() -> tuple[sqlite3.Connection, Logs]:
    """Ensure active SQLite connection and unified logs exist in session state."""
    if "conn" not in st.session_state or st.session_state.conn is None:
        conn = init_storage()
        st.session_state.conn = conn
        acc, tx, freeze, pending = ensure_log_db(conn)
        st.session_state.logs = Logs(acc, tx, freeze, pending)
    return st.session_state.conn, st.session_state.logs


def logout() -> None:
    st.session_state.logged_in = False
    st.session_state.user_type = None
    st.session_state.identifier = None
    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
#  Authentication Screen
# ─────────────────────────────────────────────────────────────────────────────

def render_login() -> None:
    conn, _ = init_dbs()

    st.markdown(
        """
        <div style="text-align: center; padding: 1.5rem 0 1rem 0;">
            <h1 style="margin-bottom: 0.2rem;">🏦 BankOS Enterprise</h1>
            <p style="color: #6c757d; font-size: 1.1rem;">Secure Digital Core Banking & Asset Management Platform</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tab_cust, tab_adm, tab_reset = st.tabs(["👤 Customer Login", "🛡️ Bank Admin Login", "🔑 Reset Password"])

    with tab_cust:
        st.subheader("Customer Portal Access")
        col_c1, col_c2 = st.columns([1, 1])
        with col_c1:
            acc_num_in = st.number_input("Account Number", min_value=1, step=1, value=1070, key="cust_acc_in")
            password_in = st.text_input("Account Password", type="password", key="cust_pw_in")
            if st.button("Sign In to Customer Account", type="primary", use_container_width=True):
                res = authenticate_customer(conn, int(acc_num_in), password_in)
                if res["status"] == "success":
                    st.session_state.logged_in = True
                    st.session_state.user_type = "customer"
                    st.session_state.identifier = int(acc_num_in)
                    st.success(res["message"])
                    st.rerun()
                else:
                    st.error(res["message"])

        with col_c2:
            with st.expander("💡 Practice Project Credentials (Demo Accounts)", expanded=True):
                st.markdown(
                    """
                    This is a practice database configured with plaintext original passwords alongside salted hashes:
                    - **Account 1070**: Password `pass1070` *(BDT, Credit Card)*
                    - **Account 1184**: Password `pass1184` *(USD, Non-Credit)*
                    - **Account 1205**: Password `pass1205` *(BDT, Credit Card with Vault)*
                    - **Account 1303**: Password `pass1303` *(BDT, Non-Credit)*
                    - **Account 1333**: Password `pass1333` *(USD, Student Account)*
                    """
                )

    with tab_adm:
        st.subheader("System Administrator Portal")
        st.info("Default Bank Admin credentials: Username: **`bankadmin`** | Password: **`bankadmin`**")
        admin_u = st.text_input("Administrator Username", value="bankadmin", key="admin_u_in")
        admin_p = st.text_input("Administrator Password", type="password", value="bankadmin", key="admin_p_in")
        if st.button("Sign In as Administrator", type="primary", use_container_width=True):
            res = authenticate_admin(conn, admin_u, admin_p)
            if res["status"] == "success":
                st.session_state.logged_in = True
                st.session_state.user_type = "admin"
                st.session_state.identifier = admin_u
                st.success(res["message"])
                st.rerun()
            else:
                st.error(res["message"])

    with tab_reset:
        st.subheader("Self-Service Password Reset")
        st.caption("Reset your password securely by verifying your security question answer.")
        role_type = st.radio("Account Role", ["Customer", "Admin"], horizontal=True, key="reset_role")
        is_admin_reset = (role_type == "Admin")

        if is_admin_reset:
            reset_ident = st.text_input("Admin Username", value="bankadmin", key="reset_admin_id")
        else:
            reset_ident = st.number_input("Account Number", min_value=1, step=1, value=1070, key="reset_cust_id")

        q_res = get_security_question(conn, str(reset_ident) if is_admin_reset else int(reset_ident), is_admin=is_admin_reset)
        if q_res["status"] == "success":
            st.markdown(f"**Security Question:** :blue[{q_res['data']['security_question']}]")
            sec_ans = st.text_input("Your Security Answer", type="password", key="reset_sec_ans")
            new_pw = st.text_input("New Desired Password", type="password", key="reset_new_pw")
            if st.button("Execute Password Reset", type="primary"):
                if not sec_ans or not new_pw:
                    st.warning("Please provide both your security answer and a new password.")
                else:
                    exec_res = execute_password_reset(
                        conn,
                        str(reset_ident) if is_admin_reset else int(reset_ident),
                        sec_ans,
                        new_pw,
                        is_admin=is_admin_reset,
                    )
                    if exec_res["status"] == "success":
                        st.success(f"{exec_res['message']} You can now log in with your new password.")
                    else:
                        st.error(exec_res["message"])
        else:
            st.warning(q_res["message"])


# ─────────────────────────────────────────────────────────────────────────────
#  Admin Portal
# ─────────────────────────────────────────────────────────────────────────────

def render_admin_portal() -> None:
    conn, logs = init_dbs()

    with st.sidebar:
        st.markdown(f"### 🛡️ Bank Admin\n**User:** `{st.session_state.identifier}`")
        menu = st.radio(
            "Navigation",
            [
                "📊 Overview",
                "👥 Account Management",
                "🔐 Vault Management",
                "⏳ Pending Transfers",
                "📜 Audit & Transaction Logs",
                "🗄️ Database Management",
                "📄 Generate Statements",
            ],
        )
        st.markdown("---")
        if st.button("Log Out", use_container_width=True):
            logout()

    if menu == "📊 Overview":
        st.title("Platform Statistics & Overview")
        ov = bank_overview(conn, pending_conn=logs.pending)
        totals = ov.get("totals", {})
        frozen = ov.get("frozen_accounts", 0)
        pending = ov.get("pending_count", 0)

        total_accs = conn.execute("SELECT COUNT(*) as c FROM accounts").fetchone()["c"]
        rate = get_usd_to_bdt_rate()

        bdt_pool = totals.get("BDT", 0)
        usd_pool = totals.get("USD", 0)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total Accounts", f"{total_accs:,}")
        m2.metric("Frozen Accounts", f"{frozen:,}")
        m3.metric("Pending Transfers", f"{pending:,}")
        m4.metric("BDT Pool", format_money(bdt_pool, "BDT"))
        m5.metric("USD Pool", format_money(usd_pool, "USD"))

        st.markdown(f"**Live Exchange Rate:** `1 USD = {rate:.2f} BDT` *(Source: `config.json`)*")

        col_a, col_b = st.columns(2)
        with col_a:
            st.subheader("Account Distribution by Type")
            type_rows = conn.execute(
                "SELECT acc_type, COUNT(*) as cnt, SUM(acc_balance) as sum_bal FROM accounts GROUP BY acc_type"
            ).fetchall()
            df_types = pd.DataFrame([dict(r) for r in type_rows])
            if not df_types.empty:
                st.dataframe(df_types, use_container_width=True)
        with col_b:
            st.subheader("System Billers & Authorities")
            billers = conn.execute(
                "SELECT biller_id, biller_name, biller_category, receiving_acc_num FROM billers"
            ).fetchall()
            st.dataframe(pd.DataFrame([dict(b) for b in billers]), use_container_width=True)

    elif menu == "👥 Account Management":
        st.title("Account Management")
        action_tab, list_tab = st.tabs(["⚡ Account Operations", "📋 Accounts Registry"])

        with list_tab:
            filter_choice = st.selectbox(
                "Filter View",
                [
                    "all_account",
                    "credit_card_account",
                    "non_credit_card_account",
                    "vault_account",
                    "non_vault_account",
                    "usd_accounts",
                    "bdt_accounts",
                ],
            )
            raw_accs = list_accounts(conn, filter_choice)
            if raw_accs:
                df = pd.DataFrame([dict(r) for r in raw_accs])
                cols_to_show = [
                    c for c in [
                        "acc_num", "acc_type", "currency", "acc_balance", "original_password",
                        "is_frozen", "daily_transfer_limit", "credit_card_limit", "credit_used",
                        "vault_no", "vault_balance", "parent_name"
                    ] if c in df.columns
                ]
                st.caption(f"Displaying {len(df)} account(s). 'original_password' shown for practice/testing.")
                st.dataframe(df[cols_to_show], use_container_width=True)
            else:
                st.info("No accounts match the selected filter.")

        with action_tab:
            crud_tabs = st.tabs([
                "➕ Create Account", "❄️ Freeze / Unfreeze", "🔄 Convert Type", "⚙️ Daily Limit", "🗑️ Delete Account"
            ])

            with crud_tabs[0]:
                st.subheader("Create New Account")
                c1, c2 = st.columns(2)
                with c1:
                    new_acc = st.number_input("New Account Number", min_value=1000, step=1, value=2001)
                    new_pwd = st.text_input("Account Password", value="pass2001")
                    new_curr = st.selectbox("Currency", list(sorted(SUPPORTED)))
                    new_type = st.selectbox("Account Type", ["non_credit_card", "credit_card", "student"])
                    new_bal = st.number_input("Opening Balance (major units)", min_value=0.0, value=500.0, step=50.0)
                with c2:
                    new_dtl = st.number_input("Daily Transfer Limit (major units)", min_value=100.0, value=5000.0, step=500.0)
                    sec_q = st.text_input("Security Question", value="What is your first pet's name?")
                    sec_a = st.text_input("Security Answer", value="Fluffy")
                    p_name = None
                    p_phone = None
                    if new_type == "student":
                        p_name = st.text_input("Parent/Guardian Name", value="Parent Guardian")
                        p_phone = st.text_input("Parent/Guardian Phone", value="+8801700000000")
                    cc_lim = 0
                    cc_pin = None
                    if new_type == "credit_card":
                        cc_lim = st.number_input("Credit Limit (minor units)", min_value=100000, value=2000000, step=100000)
                        cc_pin = st.text_input("Credit Card PIN (4 digits)", value="1234")

                if st.button("Create Account Now", type="primary"):
                    bal_minor = to_minor(new_bal)
                    dtl_minor = to_minor(new_dtl)
                    res = create_account(
                        conn,
                        int(new_acc),
                        new_pwd,
                        new_curr,
                        new_type,
                        bal_minor,
                        sec_q,
                        sec_a,
                        parent_name=p_name,
                        parent_phone=p_phone,
                        daily_transfer_limit=dtl_minor,
                        credit_card_limit=cc_lim,
                        credit_card_pin=cc_pin,
                    )
                    if res["status"] == "success":
                        st.success(res["message"])
                    else:
                        st.error(res["message"])

            with crud_tabs[1]:
                st.subheader("Freeze or Unfreeze Account")
                accs_list = [r["acc_num"] for r in list_accounts(conn)]
                target_f = st.selectbox("Select Account", accs_list, key="freeze_target")
                freeze_action = st.radio("Action", ["Freeze Account", "Unfreeze Account"], horizontal=True)
                is_freeze = (freeze_action == "Freeze Account")
                reason = st.text_input("Reason / Note", value="Administrative compliance review")
                if st.button("Apply Freeze Status", type="primary"):
                    r = freeze_account(conn, logs.freeze, int(target_f), is_freeze)
                    if r["status"] == "success":
                        st.success(r["message"])
                    else:
                        st.error(r["message"])

            with crud_tabs[2]:
                st.subheader("Convert Account Type")
                accs_list = [r["acc_num"] for r in list_accounts(conn)]
                target_c = st.selectbox("Select Account", accs_list, key="conv_target")
                new_t = st.selectbox("Target Type", ["credit_card", "non_credit_card", "student"], key="conv_t")
                if st.button("Convert Type"):
                    r = convert_account_type(conn, int(target_c), new_t)
                    if r["status"] == "success":
                        st.success(r["message"])
                    else:
                        st.error(r["message"])

            with crud_tabs[3]:
                st.subheader("Modify Daily Transfer Limit")
                accs_list = [r["acc_num"] for r in list_accounts(conn)]
                target_l = st.selectbox("Select Account", accs_list, key="limit_target")
                curr_row = conn.execute("SELECT daily_transfer_limit, currency FROM accounts WHERE acc_num=?", (target_l,)).fetchone()
                if curr_row:
                    st.write(f"Current Limit: {format_money(curr_row['daily_transfer_limit'], curr_row['currency'])}")
                new_lim = st.number_input("New Limit (major units)", min_value=1.0, value=10000.0, step=1000.0)
                if st.button("Update Limit"):
                    r = set_daily_limit(conn, int(target_l), to_minor(new_lim))
                    if r["status"] == "success":
                        st.success(r["message"])
                    else:
                        st.error(r["message"])

            with crud_tabs[4]:
                st.subheader("Delete Account")
                accs_list = [r["acc_num"] for r in list_accounts(conn)]
                target_d = st.selectbox("Select Account to Delete", accs_list, key="del_target")
                confirm = st.checkbox(f"Confirm permanent deletion of account {target_d}")
                if st.button("Permanently Delete Account", type="primary"):
                    if confirm:
                        r = delete_account(conn, logs.acc, int(target_d))
                        if r["status"] == "success":
                            st.success(r["message"])
                        else:
                            st.error(r["message"])
                    else:
                        st.warning("Please check the confirmation box.")

    elif menu == "🔐 Vault Management":
        st.title("Vault Management")
        v_tab1, v_tab2, v_tab3 = st.tabs(["Active Vaults", "Create Vault", "Destroy Vault"])

        with v_tab1:
            st.subheader("Existing Vaults")
            vault_rows = conn.execute(
                "SELECT a.acc_num, a.vault_no, a.vault_balance, a.currency, COUNT(vi.item_id) as items_count "
                "FROM accounts a LEFT JOIN vault_items vi ON a.vault_no = vi.vault_no "
                "WHERE a.vault_no IS NOT NULL GROUP BY a.acc_num"
            ).fetchall()
            if vault_rows:
                st.dataframe(pd.DataFrame([dict(r) for r in vault_rows]), use_container_width=True)
            else:
                st.info("No active vaults found.")

        with v_tab2:
            st.subheader("Create Vault for Customer Account")
            accs_no_v = [r["acc_num"] for r in conn.execute("SELECT acc_num FROM accounts WHERE vault_no IS NULL").fetchall()]
            if accs_no_v:
                v_acc = st.selectbox("Select Account", accs_no_v)
                v_no = st.text_input("Vault Number (e.g. V900)", value=f"V{v_acc}")
                v_pw = st.text_input("Vault Password", type="password")
                if st.button("Create Vault", type="primary"):
                    if not v_no or not v_pw:
                        st.warning("Vault Number and Vault Password are required.")
                    else:
                        r = create_vault(conn, int(v_acc), v_no, v_pw)
                        if r["status"] == "success":
                            st.success(r["message"])
                        else:
                            st.error(r["message"])
            else:
                st.info("All accounts currently have a vault assigned.")

        with v_tab3:
            st.subheader("Destroy Vault")
            accs_v = [r["acc_num"] for r in conn.execute("SELECT acc_num FROM accounts WHERE vault_no IS NOT NULL").fetchall()]
            if accs_v:
                d_acc = st.selectbox("Select Account", accs_v, key="dest_v_acc")
                d_vpw = st.text_input("Enter Vault Password to Authorize", type="password", key="dest_v_pw")
                transfer_bal = st.checkbox("Transfer remaining cash balance to account balance", value=True)
                if st.button("Destroy Vault", type="primary"):
                    r = destroy_vault(conn, logs.tx, int(d_acc), d_vpw, transfer_to_balance=transfer_bal)
                    if r["status"] == "success":
                        st.success(r["message"])
                    else:
                        st.error(r["message"])
            else:
                st.info("No active vaults to destroy.")

    elif menu == "⏳ Pending Transfers":
        st.title("Pending Transfers Approval Queue")
        st.caption("Transfers exceeding the compliance limit are held for administrator approval.")
        rows = get_pending_transfers(conn)
        if not rows:
            st.success("🎉 No pending transfers in queue.")
        else:
            for r in rows:
                tid = r["id"]
                with st.container():
                    st.markdown(
                        f"""
                        **Transfer ID:** {tid} | **Timestamp:** {r['timestamp']}  
                        **Sender:** `{r['sender_acc_num']}` ➔ **Receiver:** `{r['receiver_acc_num']}`  
                        **Amount:** {format_money(r['amount'], r['currency'])} | **Via Credit:** `{'Yes' if r['via_credit'] else 'No'}`
                        """
                    )
                    c_app, c_rej = st.columns([1, 1])
                    with c_app:
                        if st.button(f"✅ Approve Transfer #{tid}", key=f"app_{tid}"):
                            res = review_pending(conn, logs.tx, tid, approve=True, pending_conn=logs.pending)
                            if res["status"] == "success":
                                st.success(res["message"])
                                st.rerun()
                            else:
                                st.error(res["message"])
                    with c_rej:
                        if st.button(f"❌ Reject Transfer #{tid}", key=f"rej_{tid}"):
                            res = review_pending(conn, logs.tx, tid, approve=False, pending_conn=logs.pending)
                            if res["status"] == "success":
                                st.warning(res["message"])
                                st.rerun()
                            else:
                                st.error(res["message"])
                    st.markdown("---")

    elif menu == "📜 Audit & Transaction Logs":
        st.title("Audit Trail & Transaction Logs")
        t_choice = st.selectbox(
            "Log Type",
            ["transaction_log", "account_log", "freeze_log", "pending_transfers"],
        )
        col_f1, col_f2 = st.columns([2, 1])
        with col_f1:
            acc_filter = st.text_input("Filter by Account Number (Optional)", value="")
        with col_f2:
            row_limit = st.slider("Max Rows", min_value=10, max_value=200, value=50, step=10)

        acc_int = int(acc_filter.strip()) if acc_filter.strip().isdigit() else None
        log_rows = get_logs(conn, t_choice, acc_num=acc_int, limit=row_limit)
        if log_rows:
            df_logs = pd.DataFrame([dict(r) for r in log_rows])
            st.dataframe(df_logs, use_container_width=True)
        else:
            st.info("No log entries match the selected parameters.")

    elif menu == "🗄️ Database Management":
        st.title("Unified Database Management")
        st.caption("Inspect, query, import, and export all 12 operational tables in central bank.db.")
        db_tabs = st.tabs(["📋 Table Inspector", "📥 Multi-Format Importer", "📤 Data Exporter"])

        all_tables = get_all_table_names(conn)

        with db_tabs[0]:
            st.subheader("Table Schema & Data Inspector")
            target_t = st.selectbox("Select Database Table", all_tables)
            t_info = conn.execute(f"PRAGMA table_info({target_t})").fetchall()
            df_info = pd.DataFrame([dict(r) for r in t_info])
            st.write("**Schema Definitions:**")
            st.dataframe(df_info[["cid", "name", "type", "notnull", "dflt_value", "pk"]], use_container_width=True)

            t_rows = conn.execute(f"SELECT * FROM {target_t} LIMIT 100").fetchall()
            st.write(f"**Table Records (First 100 of {conn.execute(f'SELECT COUNT(*) as c FROM {target_t}').fetchone()['c']} rows):**")
            if t_rows:
                st.dataframe(pd.DataFrame([dict(r) for r in t_rows]), use_container_width=True)
            else:
                st.info("Table contains 0 records.")

        with db_tabs[1]:
            st.subheader("Multi-Format Data Importer")
            st.write("Supports **CSV, XLSX, JSON, XML, and SQL** ingestion.")
            imp_table = st.selectbox("Destination Table", all_tables, key="imp_t")
            uploaded_file = st.file_uploader("Select File to Ingest", type=["csv", "xlsx", "json", "xml", "sql"])
            if uploaded_file and st.button("Start Ingestion", type="primary"):
                ext = os.path.splitext(uploaded_file.name)[1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                    tmp.write(uploaded_file.getvalue())
                    tmp_path = tmp.name

                count, errors = import_file_to_table(conn, tmp_path, imp_table)
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

                if count > 0:
                    st.success(f"Successfully imported {count} row(s) into '{imp_table}'.")
                if errors:
                    st.warning(f"Encountered {len(errors)} error(s):")
                    for err in errors[:10]:
                        st.write(f"- {err}")

        with db_tabs[2]:
            st.subheader("Export Account Datasets")
            fmt = st.radio("File Format", ["CSV", "XLSX"], horizontal=True)
            exp_flt = st.selectbox(
                "Export Filter",
                ["all_account", "credit_card_account", "non_credit_card_account", "usd_accounts", "bdt_accounts"],
            )
            if st.button("Generate Export File"):
                tmp_out = tempfile.mktemp(suffix=f".{fmt.lower()}")
                if fmt == "CSV":
                    res = export_accounts_csv(conn, tmp_out, exp_flt)
                else:
                    res = export_accounts_xlsx(conn, tmp_out, exp_flt)

                if res["status"] == "success" and os.path.exists(tmp_out):
                    with open(tmp_out, "rb") as f:
                        file_bytes = f.read()
                    st.download_button(
                        label=f"⬇️ Download {fmt} Dataset ({res['data']['count']} rows)",
                        data=file_bytes,
                        file_name=f"bank_accounts_{exp_flt}.{fmt.lower()}",
                        mime="text/csv" if fmt == "CSV" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                    try:
                        os.remove(tmp_out)
                    except Exception:
                        pass
                else:
                    st.error(res["message"])

    elif menu == "📄 Generate Statements":
        st.title("Generate Customer Account Statement")
        accs = [r["acc_num"] for r in list_accounts(conn)]
        target_stmt = st.selectbox("Select Customer Account", accs)
        if st.button("Generate Statement (PDF)", type="primary"):
            try:
                pdf_path = generate_pdf_statement(conn, int(target_stmt), tx_log_conn=logs.tx)
                with open(pdf_path, "rb") as f:
                    pdf_data = f.read()
                st.download_button(
                    label=f"⬇️ Download Statement for #{target_stmt}",
                    data=pdf_data,
                    file_name=os.path.basename(pdf_path),
                    mime="application/pdf",
                )
                st.success("Official statement rendered successfully.")
            except Exception as e:
                st.error(f"Failed to generate statement: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Customer Portal
# ─────────────────────────────────────────────────────────────────────────────

def render_customer_portal() -> None:
    conn, logs = init_dbs()
    acc_num = st.session_state.identifier

    row = conn.execute("SELECT * FROM accounts WHERE acc_num=?", (acc_num,)).fetchone()
    if not row:
        st.error(f"Account {acc_num} not found.")
        if st.button("Logout"):
            logout()
        return

    currency = row["currency"]
    balance = row["acc_balance"]
    is_cc = (row["acc_type"] == "credit_card")
    is_frozen = bool(row["is_frozen"])

    with st.sidebar:
        st.markdown(f"### 👤 Customer Portal\n**Account:** `#{acc_num}`  \n**Type:** `{row['acc_type'].replace('_', ' ').title()}`")
        nav_options = [
            "🏠 Account Overview",
            "💰 Deposit & Withdraw",
            "💸 Transfer Funds",
        ]
        if is_cc:
            nav_options.append("💳 Credit Card Payback")
        nav_options.extend([
            "🔐 Vault Operations",
            "📈 Financial Instruments",
            "💡 Utility Bill Payment",
            "🏛️ Government Tax Settlement",
            "📜 Recent Transactions",
            "📄 PDF Statement",
        ])

        menu = st.radio("Navigation", nav_options)
        st.markdown("---")
        if st.button("Log Out", use_container_width=True):
            logout()

    if is_frozen:
        st.error("⚠️ **ACCOUNT IS FROZEN.** Transactions and outbound withdrawals are restricted.")

    if menu == "🏠 Account Overview":
        st.title("Account Overview")
        col_m1, col_m2 = st.columns(2)
        with col_m1:
            st.metric("Liquid Available Balance", format_money(balance, currency))
        with col_m2:
            st.metric("Dual-Currency Equivalent", format_money_dual(balance, currency).split("≈")[1].replace(")", "").strip())

        st.markdown("---")
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Account Specifications")
            st.write(f"- **Account Number:** `{acc_num}`")
            st.write(f"- **Account Type:** {row['acc_type'].replace('_', ' ').title()}")
            st.write(f"- **Currency:** {currency}")
            st.write(f"- **Status:** {'FROZEN' if is_frozen else 'Active'}")
            st.write(f"- **Daily Transfer Limit:** {format_money(row['daily_transfer_limit'], currency)}")
            if row["acc_type"] == "student":
                st.write(f"- **Parent / Guardian:** {row['parent_name']} ({row['parent_phone']})")
                st.write("- **Fee Waivers:** Maintenance, SMS Alerts, Card Issuance (All 100% Waived)")

        with c2:
            if is_cc:
                st.subheader("Credit Card Facility")
                st.write(f"- **Card Number:** **** **** **** {row['credit_card_num'][-4:] if row['credit_card_num'] else 'N/A'}")
                st.write(f"- **Credit Limit:** {format_money(row['credit_card_limit'], currency)}")
                st.write(f"- **Credit Used:** {format_money(row['credit_used'], currency)}")
                avail = max(0, row['credit_card_limit'] - row['credit_used'])
                st.write(f"- **Credit Available:** {format_money(avail, currency)}")

            if row["vault_no"]:
                st.subheader("Vault Safe")
                st.write(f"- **Vault Number:** `{row['vault_no']}`")
                st.write(f"- **Vault Cash Balance:** {format_money(row['vault_balance'], currency)}")

    elif menu == "💰 Deposit & Withdraw":
        st.title("Deposit & Withdraw Funds")
        d_tab, w_tab = st.tabs(["📥 Deposit Funds", "📤 Withdraw Funds"])

        with d_tab:
            st.subheader("Add Funds to Account")
            d_amt = st.number_input("Deposit Amount (major units)", min_value=1.0, value=100.0, step=10.0, key="dep_amt")
            d_cat = st.selectbox("Category", ["other", "food", "bills", "shopping", "transfer"], key="dep_cat")
            if st.button("Execute Deposit", type="primary"):
                r = add_funds(conn, logs.tx, acc_num, to_minor(d_amt), d_cat)
                if r["status"] == "success":
                    st.success(f"{r['message']} New balance: {format_money(r['data']['new_balance'], currency)}")
                else:
                    st.error(r["message"])

        with w_tab:
            st.subheader("Withdraw Funds from Account")
            w_amt = st.number_input("Withdrawal Amount (major units)", min_value=1.0, value=50.0, step=10.0, key="wd_amt")
            w_cat = st.selectbox("Category", ["other", "food", "bills", "shopping", "transfer"], key="wd_cat")
            if st.button("Execute Withdrawal", type="primary"):
                r = deduct_funds(conn, logs.tx, acc_num, to_minor(w_amt), w_cat)
                if r["status"] == "success":
                    st.success(f"{r['message']} New balance: {format_money(r['data']['new_balance'], currency)}")
                    if "sms_telemetry" in r["data"]:
                        st.info(f"📱 Mock SMS: {r['data']['sms_telemetry']['message']}")
                else:
                    st.error(r["message"])

    elif menu == "💸 Transfer Funds":
        st.title("Inter-Account Transfer")
        rec_acc = st.number_input("Recipient Account Number", min_value=1, step=1, value=1184)
        t_amt = st.number_input("Transfer Amount (major units)", min_value=1.0, value=100.0, step=10.0)
        t_cat = st.selectbox("Transfer Category", ["transfer", "bills", "shopping", "food", "other"])

        via_credit = False
        if is_cc:
            via_credit = st.checkbox("Fund transfer using Credit Card line", value=False)

        rec_row = conn.execute("SELECT currency FROM accounts WHERE acc_num=?", (int(rec_acc),)).fetchone()
        if rec_row and rec_row["currency"] != currency:
            est_conv = convert_minor(to_minor(t_amt), currency, rec_row["currency"])
            st.info(f"💱 Cross-currency transfer: Recipient will receive approximately {format_money(est_conv, rec_row['currency'])}.")

        if st.button("Send Transfer", type="primary"):
            r = transfer(
                conn,
                logs.tx,
                acc_num,
                int(rec_acc),
                to_minor(t_amt),
                category=t_cat,
                via_credit=via_credit,
                pending_conn=logs.pending,
            )
            if r["status"] == "success":
                st.success(r["message"])
            elif r["status"] == "pending":
                st.warning(f"⚠️ {r['message']} It will process upon administrator verification.")
            else:
                st.error(r["message"])

    elif menu == "💳 Credit Card Payback" and is_cc:
        st.title("Credit Card Balance Payback")
        st.write(f"Current Credit Used: **{format_money(row['credit_used'], currency)}**")
        st.write(f"Available Bank Balance: **{format_money(balance, currency)}**")

        p_amt = st.number_input("Payback Amount (major units)", min_value=1.0, value=from_minor(row['credit_used']) if row['credit_used'] > 0 else 10.0)
        source = st.radio("Payment Source", ["Pay from Account Balance", "Direct Cash Payment"], horizontal=True)
        cc_pin = st.text_input("Enter 4-Digit Credit Card PIN", type="password")

        if st.button("Submit Credit Card Payment", type="primary"):
            from_bal = (source == "Pay from Account Balance")
            r = payback_credit(conn, logs.tx, acc_num, to_minor(p_amt), from_bal, cc_pin)
            if r["status"] == "success":
                st.success(r["message"])
            else:
                st.error(r["message"])

    elif menu == "🔐 Vault Operations":
        st.title("Secure Vault Operations")
        if not row["vault_no"]:
            st.info("You do not have a vault attached to your account.")
            new_vno = st.text_input("Choose Vault Number (e.g. V101)", value=f"V{acc_num}")
            new_vpw = st.text_input("Set Vault Password", type="password")
            if st.button("Establish Vault Now", type="primary"):
                if not new_vno or not new_vpw:
                    st.warning("Please provide both vault number and password.")
                else:
                    r = create_vault(conn, acc_num, new_vno, new_vpw)
                    if r["status"] == "success":
                        st.success(r["message"])
                        st.rerun()
                    else:
                        st.error(r["message"])
        else:
            st.write(f"**Vault Assigned:** `{row['vault_no']}` | **Cash Holding:** {format_money(row['vault_balance'], currency)}")
            v_pw_attempt = st.text_input("Unlock Vault with Password", type="password", key="v_unlock_pw")
            unlocked = verify_vault_auth(conn, acc_num, v_pw_attempt)

            if not unlocked and v_pw_attempt:
                st.error("Incorrect vault password.")

            if unlocked:
                st.success("🔓 Vault Unlocked.")
                vt1, vt2, vt3 = st.tabs(["💰 Cash Movements", "💎 Physical Assets", "⚠️ Close Vault"])

                with vt1:
                    st.subheader("Vault Cash Deposit & Withdrawal")
                    c_v1, c_v2 = st.columns(2)
                    with c_v1:
                        v_dep = st.number_input("Deposit Cash to Vault (major units)", min_value=1.0, value=50.0, step=10.0)
                        if st.button("Deposit to Vault"):
                            r = vault_add(conn, logs.tx, acc_num, to_minor(v_dep), v_pw_attempt)
                            if r["status"] == "success":
                                st.success(r["message"])
                                st.rerun()
                            else:
                                st.error(r["message"])
                    with c_v2:
                        v_wd = st.number_input("Withdraw Cash from Vault (major units)", min_value=1.0, value=25.0, step=10.0)
                        to_cc = False
                        if is_cc:
                            to_cc = st.checkbox("Route directly to Credit Card payback", value=False)
                        if st.button("Withdraw from Vault"):
                            r = vault_deduct(conn, logs.tx, acc_num, to_minor(v_wd), v_pw_attempt, to_credit_payback=to_cc)
                            if r["status"] == "success":
                                st.success(r["message"])
                                st.rerun()
                            else:
                                st.error(r["message"])

                with vt2:
                    st.subheader("Physical Asset Inventory")
                    items = conn.execute("SELECT * FROM vault_items WHERE vault_no=?", (row["vault_no"],)).fetchall()
                    if items:
                        df_items = pd.DataFrame([dict(i) for i in items])
                        st.dataframe(df_items[["item_id", "item_type", "description", "est_value_minor", "added_at"]], use_container_width=True)
                    else:
                        st.info("No physical assets committed yet.")

                    st.markdown("#### Deposit New Physical Asset")
                    i_type = st.selectbox("Asset Category", ["gold", "paper_deeds", "corporate_bonds", "heirlooms"])
                    i_desc = st.text_input("Asset Description", value="10 Tolas of 24K Gold Bar")
                    i_val = st.number_input("Estimated Value (major units)", min_value=1.0, value=150000.0, step=5000.0)
                    if st.button("Commit Asset to Vault"):
                        r = add_vault_item(conn, acc_num, v_pw_attempt, i_type, i_desc, to_minor(i_val))
                        if r["status"] == "success":
                            st.success(r["message"])
                            st.rerun()
                        else:
                            st.error(r["message"])

                with vt3:
                    st.subheader("Destroy Vault")
                    st.warning("Closing this vault will permanently delete its record and release cash assets.")
                    to_bal = st.checkbox("Transfer remaining cash to account balance", value=True, key="dest_to_bal")
                    if st.button("Permanently Destroy Vault", type="primary"):
                        r = destroy_vault(conn, logs.tx, acc_num, v_pw_attempt, transfer_to_balance=to_bal)
                        if r["status"] == "success":
                            st.success(r["message"])
                            st.rerun()
                        else:
                            st.error(r["message"])

    elif menu == "📈 Financial Instruments":
        st.title("Financial Derivatives & Instruments")
        f_tabs = st.tabs(["🏦 Loans", "📜 Bonds", "🖋️ Checks"])

        with f_tabs[0]:
            st.subheader("Loan Facility")
            l_col1, l_col2 = st.columns(2)
            with l_col1:
                st.write("**Disburse New Loan**")
                loan_p = st.number_input("Principal Amount (major units)", min_value=100.0, value=5000.0, step=500.0)
                loan_r = st.number_input("Annual Interest Rate (%)", min_value=1.0, max_value=30.0, value=9.5, step=0.5)
                if st.button("Disburse Loan"):
                    res = execute_loan_disbursal(conn, logs.tx, acc_num, to_minor(loan_p), loan_r / 100.0)
                    if res["status"] == "success":
                        st.success(res["message"])
                    else:
                        st.error(res["message"])

            with l_col2:
                st.write("**Active Loans**")
                loans = conn.execute("SELECT * FROM loans WHERE acc_num=? AND status='active'", (acc_num,)).fetchall()
                if loans:
                    st.dataframe(pd.DataFrame([dict(l) for l in loans]), use_container_width=True)
                    st.write("**Repay Loan**")
                    rep_id = st.selectbox("Select Loan ID to Repay", [l["loan_id"] for l in loans])
                    rep_amt = st.number_input("Repayment Amount (major units)", min_value=10.0, value=500.0, step=50.0)
                    if st.button("Submit Loan Repayment"):
                        r = execute_loan_repayment(conn, logs.tx, int(rep_id), to_minor(rep_amt))
                        if r["status"] == "success":
                            st.success(r["message"])
                        else:
                            st.error(r["message"])
                else:
                    st.info("No active loans.")

        with f_tabs[1]:
            st.subheader("Sovereign & Treasury Bonds")
            b_col1, b_col2 = st.columns(2)
            with b_col1:
                st.write("**Purchase Bond**")
                b_face = st.number_input("Face Value (major units)", min_value=100.0, value=1000.0, step=100.0)
                b_yield = st.number_input("Yield Rate (%)", min_value=1.0, max_value=20.0, value=7.0, step=0.5)
                b_days = st.number_input("Maturity Period (days)", min_value=1, value=30, step=5)
                if st.button("Purchase Bond"):
                    mat_ts = (datetime.now() + timedelta(days=int(b_days))).isoformat()
                    res = execute_bond_purchase(conn, logs.tx, acc_num, to_minor(b_face), b_yield / 100.0, mat_ts)
                    if res["status"] == "success":
                        st.success(res["message"])
                    else:
                        st.error(res["message"])

            with b_col2:
                st.write("**Owned Bonds**")
                bonds = conn.execute("SELECT * FROM bonds WHERE acc_num=?", (acc_num,)).fetchall()
                if bonds:
                    st.dataframe(pd.DataFrame([dict(b) for b in bonds]), use_container_width=True)
                    active_b = [b["bond_id"] for b in bonds if b["status"] == "active"]
                    if active_b:
                        red_id = st.selectbox("Select Bond to Redeem", active_b)
                        if st.button("Redeem Bond"):
                            r = execute_bond_redemption(conn, logs.tx, int(red_id))
                            if r["status"] == "success":
                                st.success(r["message"])
                            else:
                                st.error(r["message"])
                else:
                    st.info("No bonds owned.")

        with f_tabs[2]:
            st.subheader("Check Management")
            chk_col1, chk_col2 = st.columns(2)
            with chk_col1:
                st.write("**Issue New Check**")
                chk_amt = st.number_input("Check Amount (major units)", min_value=10.0, value=250.0, step=25.0)
                payee = st.text_input("Payee Full Name", value="Apex Supplies Ltd")
                memo = st.text_input("Memo / Reference", value="Invoice #4401")
                if st.button("Issue Check"):
                    r = execute_check_issuance(conn, logs.tx, acc_num, to_minor(chk_amt), payee, memo)
                    if r["status"] == "success":
                        st.success(r["message"])
                    else:
                        st.error(r["message"])

            with chk_col2:
                st.write("**Issued Checks Register**")
                chks = conn.execute("SELECT * FROM checks WHERE acc_num=?", (acc_num,)).fetchall()
                if chks:
                    st.dataframe(pd.DataFrame([dict(c) for c in chks]), use_container_width=True)
                    issued_ids = [c["check_id"] for c in chks if c["status"] == "issued"]
                    if issued_ids:
                        target_chk = st.selectbox("Select Check to Process", issued_ids)
                        b1, b2 = st.columns(2)
                        with b1:
                            if st.button("Simulate Clearing"):
                                r = execute_check_clearing(conn, logs.tx, int(target_chk))
                                st.success(r["message"])
                        with b2:
                            if st.button("Simulate Bouncing"):
                                r = execute_check_bouncing(conn, logs.tx, int(target_chk))
                                st.warning(r["message"])
                else:
                    st.info("No checks issued yet.")

    elif menu == "💡 Utility Bill Payment":
        st.title("Utility Bill Payments")
        billers = get_billers_by_category(conn)
        available_billers = []
        for cat in ["gas", "electric", "water"]:
            for b in billers.get(cat, []):
                available_billers.append((b["biller_id"], f"{b['biller_name']} [{cat.upper()}] (Receiving Acc: #{b['receiving_acc_num']})"))

        if available_billers:
            b_choice = st.selectbox("Select Utility Provider", available_billers, format_func=lambda x: x[1])
            u_amt = st.number_input("Bill Amount (major units)", min_value=1.0, value=120.0, step=10.0)
            if st.button("Pay Utility Bill", type="primary"):
                r = pay_biller(conn, logs.tx, acc_num, b_choice[0], to_minor(u_amt))
                if r["status"] == "success":
                    st.success(r["message"])
                else:
                    st.error(r["message"])
        else:
            st.info("No utility billers registered.")

    elif menu == "🏛️ Government Tax Settlement":
        st.title("Government Tax Settlement")
        billers = get_billers_by_category(conn)
        tax_billers = billers.get("tax", [])
        if tax_billers:
            t_choice = st.selectbox("Select Tax Authority", [(b["biller_id"], f"{b['biller_name']} (Acc: #{b['receiving_acc_num']})") for b in tax_billers], format_func=lambda x: x[1])
            tax_amt = st.number_input("Tax Amount (major units)", min_value=1.0, value=500.0, step=50.0)
            if st.button("Settle Tax Liability", type="primary"):
                r = pay_biller(conn, logs.tx, acc_num, t_choice[0], to_minor(tax_amt))
                if r["status"] == "success":
                    st.success(r["message"])
                else:
                    st.error(r["message"])
        else:
            st.info("No tax authorities registered.")

    elif menu == "📜 Recent Transactions":
        st.title("Recent Transactions")
        tx_rows = recent_transactions(conn, acc_num, limit=50, tx_log_conn=logs.tx)
        if tx_rows:
            df = pd.DataFrame([dict(r) for r in tx_rows])
            st.dataframe(df, use_container_width=True)
        else:
            st.info("No recorded transactions found.")

    elif menu == "📄 PDF Statement":
        st.title("Official Statement Generation")
        st.write("Generate and download an official stamped account statement.")
        if st.button("Render PDF Statement", type="primary"):
            try:
                pdf_path = generate_pdf_statement(conn, acc_num, tx_log_conn=logs.tx)
                with open(pdf_path, "rb") as f:
                    pdf_data = f.read()
                st.download_button(
                    label="⬇️ Download PDF Statement",
                    data=pdf_data,
                    file_name=os.path.basename(pdf_path),
                    mime="application/pdf",
                )
                st.success("Statement generated.")
            except Exception as e:
                st.error(f"Error generating statement: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Application Root
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(page_title="BankOS Enterprise", page_icon="🏦", layout="wide")
    init_dbs()

    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False
        st.session_state.user_type = None
        st.session_state.identifier = None

    if not st.session_state.logged_in:
        render_login()
    else:
        if st.session_state.user_type == "admin":
            render_admin_portal()
        else:
            render_customer_portal()


if __name__ == "__main__":
    main()

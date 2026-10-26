"""
streamlit_app.py — Smart Retry operator console (MVP).

Screens (prep/05-operator-experience.md §6): Today · Queue · Loan detail · Needs action.
Reads the engine's outputs (data/output/decisions_*.csv) and the DuckDB tables.

Run locally from the project root:  streamlit run streamlit_app.py
"""

# =============================================================================
# Step 1. Imports and paths
# =============================================================================
import pathlib
import subprocess
import sys

import duckdb
import pandas as pd
import streamlit as st

sys.path.insert(0, "src")
import engine  # noqa: E402

DB, OUT = engine.DB, engine.OUT
RAW = [pathlib.Path("data/PaymentsPM_Challenge_loan_info.csv"),
       pathlib.Path("data/PaymentsPM_Challenge_payment_requests.csv")]
OWNER = {
    "NEW_ACCOUNT": "Lender: new account", "RECONSENT": "Lender: re-consent",
    "BLOCKED_AGAIN": "Lender: check account", "CHARGEBACK": "Outreach",
    "NEVER_PAYER": "Outreach", "NOT_WORTH_IT": "Outreach", "UNKNOWN_CODE": "Belvo ops",
}

st.set_page_config(page_title="Belvo Smart Retry", layout="wide")


# =============================================================================
# Step 2. Build the data on the first run (e.g. on Streamlit Cloud)
# =============================================================================
@st.cache_resource(show_spinner="Building the database and decisions (first run only)…")
def ensure_data():
    if not pathlib.Path(DB).exists():
        if not all(p.exists() for p in RAW):
            return False
        subprocess.run([sys.executable, "src/load.py"], check=True)
    for v in ("v1", "v0"):
        if not list(OUT.glob(f"decisions_*_{v}.csv")):
            engine.run(engine.Config(version=v))
    return True


if not ensure_data():
    st.warning("The dataset isn't available in this deployment. Run the app locally to see the demo.")
    st.stop()


# =============================================================================
# Step 3. Load data (cached)
# =============================================================================
@st.cache_data
def load_decisions(version):
    path = sorted(OUT.glob(f"decisions_*_{version}.csv"))[-1]
    return pd.read_csv(path, dtype={"loan_id": str}, parse_dates=["attempt_date"])


@st.cache_resource
def engine_context():
    con = duckdb.connect(DB, read_only=True)
    as_of, loans, recent = engine.load(con)
    odds = engine.OddsBook(con, engine.Config().min_n)
    con.close()
    return as_of, {f["loan_id"]: f for f in loans}, recent, odds


@st.cache_data
def load_history(loan_id):
    con = duckdb.connect(DB, read_only=True)
    df = con.execute("""
        SELECT attempt_day, n_requests, amounts, outcome, failed_reason, n_success, n_chargeback, net_collected
        FROM attempts WHERE loan_id = ? ORDER BY created_utc DESC
    """, [loan_id]).df()
    con.close()
    return df


as_of, loans, recent, odds = engine_context()

# =============================================================================
# Step 4. Sidebar: engine version and decision day
# =============================================================================
version = st.sidebar.radio("Engine version", ["v1", "v0"], captions=["Rules + lookup tables", "Rules only"])
d = load_decisions(version)
st.sidebar.caption(f"Decision day: {as_of:%a %b %d, %Y}")
st.sidebar.caption(f"{len(d):,} loans in the portfolio")

st.title("Smart Retry · operator console")
tab_today, tab_queue, tab_loan, tab_needs = st.tabs(["Today", "Queue", "Loan detail", "Needs action"])

# =============================================================================
# Step 5. Today: what goes out, what needs people, and the export
# =============================================================================
with tab_today:
    today = d[d.decision == "ATTEMPT"]
    c = st.columns(5)
    c[0].metric("Loans to attempt today", f"{len(today):,}")
    c[1].metric("Debits to send", f"{int(today.n_debits.sum()):,}")
    c[2].metric("MXN requested", f"{today.total_amount.sum():,.0f}")
    c[3].metric("Expected value (MXN)", f"{today.expected_value.sum():,.0f}")
    c[4].metric("Needs action", f"{(d.decision == 'NEEDS_ACTION').sum():,}")

    st.subheader("Decision mix")
    left, right = st.columns([1, 1])
    left.bar_chart(d.decision.value_counts())
    right.dataframe(d.groupby(["decision", "rule_fired"]).size().rename("loans").reset_index(), hide_index=True)

    attempts_file = sorted(OUT.glob(f"attempts_*_{version}.csv"))[-1]
    st.download_button("Download today's attempts (CSV)", attempts_file.read_bytes(),
                       file_name=attempts_file.name, mime="text/csv")

# =============================================================================
# Step 6. Queue: every loan with its decision and reason, filterable
# =============================================================================
with tab_queue:
    f1, f2, f3, f4 = st.columns(4)
    dec = f1.multiselect("Decision", sorted(d.decision.unique()), default=["ATTEMPT", "SCHEDULE"])
    rules = f2.multiselect("Rule", sorted(d.rule_fired.unique()))
    banks = f3.multiselect("Bank", sorted(d.bank.unique()))
    segs = f4.multiselect("Days overdue", list(engine.SEGMENTS))
    q = d[d.decision.isin(dec)] if dec else d
    if rules:
        q = q[q.rule_fired.isin(rules)]
    if banks:
        q = q[q.bank.isin(banks)]
    if segs:
        q = q[q.days_overdue.apply(engine.overdue_segment).isin(segs)]
    st.caption(f"{len(q):,} loans")
    show = q.assign(odds=q.p_success * 100).sort_values("expected_value", ascending=False)
    st.dataframe(
        show[["loan_id", "decision", "attempt_date", "option", "amounts", "odds", "expected_value",
              "rule_fired", "reason_text"]],
        hide_index=True,
        column_config={
            "attempt_date": st.column_config.DateColumn("Date"),
            "odds": st.column_config.NumberColumn("Odds", format="%.1f%%"),
            "expected_value": st.column_config.NumberColumn("Expected value (MXN)", format="%.1f"),
            "reason_text": st.column_config.TextColumn("Why", width="large"),
        },
    )

# =============================================================================
# Step 7. Loan detail: the decision, the options considered, and the history
# =============================================================================
with tab_loan:
    ids = d.sort_values("expected_value", ascending=False).loan_id.tolist()
    loan_id = st.selectbox("Loan", ids)
    row = d[d.loan_id == loan_id].iloc[0]
    f = loans[loan_id]
    st.markdown(f"**{row.decision}** · rule `{row.rule_fired}` · {row.reason_text}")

    k = st.columns(5)
    k[0].metric("Owed (MXN)", f"{f['total_amount_outstanding']:,.0f}")
    k[1].metric("Days overdue", f"{f['days_overdue_as_of']:,}")
    k[2].metric("Attempts so far", f"{f['attempts_total']:,}")
    k[3].metric("Ever paid", "Yes" if f["ever_paid"] else "No")
    k[4].metric("Chargebacks", f"{f['chargeback_count']}")

    st.subheader("Options considered")
    cfg = engine.Config(version=version)
    if engine.guardrails(f, cfg, as_of):
        st.info("A guardrail decided this loan, so no days or amounts were scored.")
    else:
        days, _, _ = engine.candidate_days(f, cfg, as_of, recent.get(loan_id, []))
        if not days:
            st.info("The 30-day cap blocks every day in the horizon.")
        else:
            rows = []
            for day in days:
                for code, name, debits in engine.candidate_amounts(f, cfg):
                    ev, parts = engine.score(f, day, debits, odds, cfg)
                    rows.append({"day": day, "option": f"{code} · {name}",
                                 "amounts": ", ".join(f"{a:,.2f}" for a in debits),
                                 "payday window": engine.in_payday_window(day),
                                 "odds per debit": " / ".join(f"{p['p']:.1%}" for p in parts),
                                 "expected value": round(ev, 2)})
            st.dataframe(pd.DataFrame(rows).sort_values("expected value", ascending=False).head(15),
                         hide_index=True)

    st.subheader("Attempt history")
    st.dataframe(load_history(loan_id), hide_index=True)

# =============================================================================
# Step 8. Needs action: who must do what, with a file per owner
# =============================================================================
with tab_needs:
    na = d[d.reason_code.isin(OWNER)].assign(owner=lambda x: x.reason_code.map(OWNER))
    st.dataframe(na.groupby("owner").size().rename("loans").reset_index(), hide_index=True)
    owner = st.selectbox("Show", sorted(na.owner.unique()))
    sel = na[na.owner == owner]
    st.dataframe(sel[["loan_id", "bank", "rule_fired", "reason_text"]], hide_index=True)
    st.download_button(f"Download {len(sel):,} loans (CSV)", sel.to_csv(index=False).encode(),
                       file_name=f"needs_action_{owner.replace(': ', '_').replace(' ', '_').lower()}.csv",
                       mime="text/csv")

import duckdb

MIN_N = 30
L3 = ["prev_outcome", "ever_paid_before", "overdue_segment", "attempt_bucket", "payday_window", "gap_bucket", "amount_bucket"]

def p_success(con, key: dict) -> tuple[float, str, int]:
    for table, cols in (("odds_l3", L3), ("odds_l2", L3[:6]), ("odds_l1", L3[:3])):
        where = " AND ".join(f"{c} = ?" for c in cols)
        row = con.execute(f"SELECT p_success, n FROM {table} WHERE {where}", [key[c] for c in cols]).fetchone()
        if row and row[1] >= MIN_N:
            return row[0], table, row[1]
    row = con.execute("SELECT p_success, n FROM odds_l0").fetchone()
    return row[0], "odds_l0", row[1]

def p_chargeback(con, bank: str, amount_bucket: str) -> float:
    row = con.execute("SELECT p_cb, n FROM cb_l2 WHERE bank = ? AND amount_bucket = ?", [bank, amount_bucket]).fetchone()
    if row and row[1] >= MIN_N:
        return row[0]
    row = con.execute("SELECT p_cb, n FROM cb_l1 WHERE bank = ?", [bank]).fetchone()
    if row and row[1] >= MIN_N:
        return row[0]
    return con.execute("SELECT p_cb FROM cb_l0").fetchone()[0]
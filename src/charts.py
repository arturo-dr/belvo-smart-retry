"""
charts.py — slide-ready charts for the Belvo Smart Retry deck.

Reads the tables built by src/load.py (data/belvo.duckdb) and saves seven PNGs
to charts/. Each chart prints its headline numbers so you can check them
against prep/06-data-profile.md.

Run from the project root:  python src/charts.py
"""

# =============================================================================
# Step 1. Imports and output folder
# =============================================================================
import pathlib

import duckdb
import matplotlib
import pandas as pd

matplotlib.use("Agg")  # render to files, no window needed
import matplotlib.pyplot as plt

OUT = pathlib.Path("charts")
OUT.mkdir(exist_ok=True)

# =============================================================================
# Step 2. Connect to the database (read-only, so it can't change anything)
# =============================================================================
con = duckdb.connect("data/belvo.duckdb", read_only=True)

# =============================================================================
# Step 3. Colors and style (light theme; blue + orange pass colorblind checks)
# =============================================================================
SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, BAND = "#e1e0d9", "#c3c2b7", "#f0efec"
BLUE, ORANGE = "#2a78d6", "#eb6834"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 12, "text.color": INK, "axes.labelcolor": INK2,
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.edgecolor": AXIS,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.axisbelow": True,
    "axes.titlesize": 15, "axes.titleweight": "bold", "axes.titlelocation": "left",
})

# =============================================================================
# Step 3b. Cost assumptions (decided Oct 20)
#   MXN 4 per debit request (sensitivity 2 / 6), MXN 20 handling per chargeback
# =============================================================================
COST_ATTEMPT = 4
COST_CHARGEBACK = 20


# =============================================================================
# Step 4. Small helpers
# =============================================================================
def q(sql: str):
    """Run a query and return a pandas DataFrame."""
    return con.sql(sql).df()


def pct(ax, axis: str = "y"):
    """Show an axis as whole percentages (values are already 0–100)."""
    fmt = lambda v, _: f"{v:.0f}%"
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(fmt)


def fmt_pct(v: float) -> str:
    """One decimal above 1%, two decimals below (so 0.08% doesn't show as 0.1%)."""
    return f"{v:.1f}%" if v >= 1 else f"{v:.2f}%"


def save(fig, name: str, footnote: str | None = None):
    """Tidy the layout, add an optional footnote, save at slide resolution, and free memory."""
    if footnote:
        fig.text(0.01, 0.01, footnote, color=MUTED, fontsize=10)
        fig.tight_layout(rect=(0, 0.04, 1, 1))  # leave room at the bottom for the footnote
    else:
        fig.tight_layout()
    fig.savefig(OUT / name, dpi=200)
    plt.close(fig)
    print(f"saved charts/{name}")


def stop_rule_breakdown():
    """One row per stop rule: requests stopped, successes, reversed, stuck, MXN stuck.

    A debit request counts as stopped if, before it was sent, the same loan had
    already hit a dead account (HARD), a revoked authorization (REVOKED) or a
    chargeback. The first matching rule wins, in that order.
    """
    return q("""
        WITH flags AS (
            SELECT loan_id, created_utc,
                   max(CASE WHEN outcome = 'HARD'    THEN 1 ELSE 0 END) OVER w AS prior_hard,
                   max(CASE WHEN outcome = 'REVOKED' THEN 1 ELSE 0 END) OVER w AS prior_revoked,
                   max(CASE WHEN n_chargeback > 0    THEN 1 ELSE 0 END) OVER w AS prior_cb
            FROM attempts
            WINDOW w AS (PARTITION BY loan_id ORDER BY created_utc
                         ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
        ),
        f AS (
            SELECT r.*,
                   CASE WHEN prior_hard = 1    THEN 'dead account'
                        WHEN prior_revoked = 1 THEN 'revoked'
                        WHEN prior_cb = 1      THEN 'after chargeback'
                        ELSE 'not stopped' END AS rule
            FROM requests r JOIN flags USING (loan_id, created_utc)
        )
        SELECT rule,
               count(*)                                            AS requests,
               sum((status IN ('successful', 'chargeback'))::INT)  AS successes,
               sum((status = 'chargeback')::INT)                   AS later_reversed,
               sum((status = 'successful')::INT)                   AS stuck,
               round(sum(CASE WHEN status = 'successful' THEN amount ELSE 0 END)) AS stuck_mxn
        FROM f
        GROUP BY 1
        ORDER BY requests DESC
    """)


# =============================================================================
# Step 5. Chart 1 — hit rate by attempt number
#   Expect: 10.8% on the 1st attempt → 0.46% after 100
# =============================================================================
def chart_01_decay_by_attempt():
    df = q("""
        SELECT CASE WHEN attempt_no = 1   THEN '1'
                    WHEN attempt_no = 2   THEN '2'
                    WHEN attempt_no <= 5  THEN '3-5'
                    WHEN attempt_no <= 10 THEN '6-10'
                    WHEN attempt_no <= 20 THEN '11-20'
                    WHEN attempt_no <= 50 THEN '21-50'
                    WHEN attempt_no <= 100 THEN '51-100'
                    ELSE '100+' END                                   AS bucket,
               min(attempt_no)                                        AS ord,
               100.0 * avg(CASE WHEN n_success > 0 THEN 1 ELSE 0 END) AS hit
        FROM attempts
        GROUP BY 1
        ORDER BY ord
    """)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(df.bucket, df.hit, width=0.6, color=BLUE)
    for i in (0, len(df) - 1):  # label only the first and last bar
        ax.annotate(fmt_pct(df.hit[i]), (i, df.hit[i]), xytext=(0, 4),
                    textcoords="offset points", ha="center")
    ax.set_title("Every retry is less likely to work")
    ax.set_xlabel("Attempt number on the loan")
    ax.set_ylabel("Attempts that succeeded")
    pct(ax)
    save(fig, "01_decay_by_attempt.png")
    print(df[["bucket", "hit"]].round(2).to_string(index=False))


# =============================================================================
# Step 6. Chart 2 — next-attempt hit rate by previous outcome
#   Expect: success 27.8% · first 10.8% · code 11 4.2% · funds 2.3%
#           revoked ~0.4% · blocked ~0.10% · dead account ~0.08%
# =============================================================================
def chart_02_next_by_previous():
    labels = {
        "SUCCESS": "After a success",
        "FIRST":   "First attempt",
        "WAIT":    "After 'not authorized' (11)",
        "FUNDS":   "After insufficient funds (4)",
        "REVOKED": "After stop-payment or cancellation (8, 10)",
        "BLOCKED": "After a blocked account (2)",
        "HARD":    "After a dead account (1, 3, 6)",
    }
    df = q("""
        SELECT prev_outcome,
               count(*)                                               AS n,
               100.0 * avg(CASE WHEN n_success > 0 THEN 1 ELSE 0 END) AS hit
        FROM attempts
        GROUP BY 1
    """)
    df = df[df.prev_outcome.isin(labels)].sort_values("hit").reset_index(drop=True)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.barh([labels[o] for o in df.prev_outcome], df.hit, height=0.6, color=BLUE)
    for y, (h, n) in enumerate(zip(df.hit, df.n)):
        ax.annotate(f"{fmt_pct(h)}  (n={n:,})", (h, y), xytext=(4, 0),
                    textcoords="offset points", va="center", color=INK2, fontsize=11)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", visible=True)
    ax.set_title("The previous result predicts the next one")
    ax.set_xlabel("Next attempt succeeded")
    pct(ax, "x")
    save(fig, "02_next_by_previous.png")
    print(df[["prev_outcome", "n", "hit"]].round(2).to_string(index=False))


# =============================================================================
# Step 7. Chart 3 — hit rate by day of the month, payday windows shaded
#   Expect: 13th–15th ≈ 3.9–4.3% · 6th–7th and 20th–21st ≈ 1.6–1.9%
#           (window vs. other days ≈ 1.37×, not 2×)
# =============================================================================
def chart_03_day_of_month():
    df = q("""
        SELECT day(attempt_day)                                       AS dom,
               count(*)                                               AS n,
               100.0 * avg(CASE WHEN n_success > 0 THEN 1 ELSE 0 END) AS hit
        FROM attempts
        GROUP BY 1
        ORDER BY 1
    """)

    fig, ax = plt.subplots(figsize=(12, 5))
    for lo, hi in ((0.5, 2.5), (11.5, 15.5), (27.5, 31.5)):  # days 1–2, 12–15, 28–31
        ax.axvspan(lo, hi, color=BAND, zorder=0)
    ax.bar(df.dom, df.hit, width=0.7, color=BLUE)
    top = df.hit.max()
    ax.set_ylim(0, top * 1.2)
    for x in (13.5, 29.5):
        ax.text(x, top * 1.1, "payday window", ha="center", color=INK2, fontsize=11)
    ax.set_xticks(range(1, 32))
    ax.tick_params(axis="x", labelsize=9)
    ax.set_title("Attempts around payday work better")
    ax.set_xlabel("Day of the month the attempt was created (Mexico City)")
    ax.set_ylabel("Attempts that succeeded")
    pct(ax)
    save(fig, "03_day_of_month.png")

    in_window = df.dom.between(12, 15) | (df.dom >= 28) | (df.dom <= 2)
    w = (df.hit[in_window] * df.n[in_window]).sum() / df.n[in_window].sum()
    o = (df.hit[~in_window] * df.n[~in_window]).sum() / df.n[~in_window].sum()
    print(f"window {w:.2f}% vs other days {o:.2f}% → lift {w / o:.2f}x")


# =============================================================================
# Step 8. Chart 4 — amount: hit rate and chargeback share (two panels)
#   Expect hit: <100 ≈ 4.5% · 100–499 ≈ 2.4% · 500–1,999 ≈ 1.2% · 2,000+ ≈ 0.3%
#   Expect charged back: <100 ≈ 17% · above ≈ 25–30%
# =============================================================================
def chart_04_amount():
    df = q("""
        SELECT amount_bucket,
               min(amount)                                                      AS ord,
               100.0 * avg(is_success::INT)                                     AS hit,
               100.0 * sum(is_chargeback::INT) / nullif(sum(is_success::INT), 0) AS cb
        FROM debits
        GROUP BY 1
        ORDER BY ord
    """)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 5))
    a1.bar(df.amount_bucket, df.hit, width=0.6, color=BLUE)
    a1.set_title("Smaller debits succeed more")
    a1.set_ylabel("Debits that succeeded")
    a2.bar(df.amount_bucket, df.cb, width=0.6, color=ORANGE)
    a2.set_title("Small ones get charged back less")
    a2.set_ylabel("Successes later charged back")
    for ax in (a1, a2):
        ax.set_xlabel("Requested amount (MXN)")
        pct(ax)
    save(fig, "04_amount.png")
    print(df[["amount_bucket", "hit", "cb"]].round(2).to_string(index=False))


# =============================================================================
# Step 9. Chart 5 — what the three stop rules remove
#   (debit requests after a dead account, a revoked authorization or a chargeback)
#   Expect: ≈ 20% of debit requests · ≈ 57% of chargebacks · ≈ 7% of net MXN
# =============================================================================
def chart_05_stop_rules():
    df = q("""
        WITH flags AS (
            SELECT loan_id, created_utc,
                   max(CASE WHEN outcome = 'HARD'    THEN 1 ELSE 0 END) OVER w AS prior_hard,
                   max(CASE WHEN outcome = 'REVOKED' THEN 1 ELSE 0 END) OVER w AS prior_revoked,
                   max(CASE WHEN n_chargeback > 0    THEN 1 ELSE 0 END) OVER w AS prior_cb
            FROM attempts
            WINDOW w AS (PARTITION BY loan_id ORDER BY created_utc
                         ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
        )
        SELECT (coalesce(prior_hard, 0) + coalesce(prior_revoked, 0) + coalesce(prior_cb, 0)) > 0 AS stop_rule,
               count(*)                                                    AS requests,
               sum((status = 'chargeback')::INT)                           AS chargebacks,
               sum(CASE WHEN status = 'successful' THEN amount ELSE 0 END) AS net_mxn
        FROM requests
        JOIN flags USING (loan_id, created_utc)
        GROUP BY 1
    """)
    cols = ["requests", "chargebacks", "net_mxn"]
    share = df[df.stop_rule][cols].iloc[0] / df[cols].sum() * 100

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(["Debit requests", "Chargebacks", "Net MXN recovered"], share.values, width=0.55, color=BLUE)
    for i, v in enumerate(share.values):
        ax.annotate(f"{v:.0f}%", (i, v), xytext=(0, 4), textcoords="offset points",
                    ha="center", fontsize=13)
    ax.set_ylim(0, 100)
    pct(ax)
    r, c, m = share.values
    ax.set_title(f"Three stop rules: {r:.0f}% of debits, {c:.0f}% of chargebacks, {m:.0f}% of the money")
    ax.set_ylabel("Share of history affected")
    save(fig, "05_stop_rules.png")
    print(share.round(1).to_string())


# =============================================================================
# Step 10. Chart 6 — monthly volume and in-flight overlap (two stacked panels)
#   Expect: ≈ 85k requests in Oct 2025 · overlap ≈ 0% until mid-2025,
#           ≈ 87% in Oct, ≈ 97% in Nov (Nov covers the 1st–7th only)
# =============================================================================
def chart_06_volume_and_overlap():
    vol = q("""
        SELECT strftime(created_utc, '%Y-%m') AS month, count(*) AS requests
        FROM requests
        WHERE created_utc >= '2024-07-01'
        GROUP BY 1 ORDER BY 1
    """)
    ovl = q("""
        SELECT strftime(created_utc, '%Y-%m') AS month,
               100.0 * avg(CASE WHEN sent_while_prev_pending THEN 1 ELSE 0 END) AS overlap
        FROM attempts
        WHERE attempt_no > 1 AND created_utc >= '2024-07-01'
        GROUP BY 1 ORDER BY 1
    """)

    # 10a. Mark the last month as partial if the data stops before month end
    last_day = pd.Timestamp(q("SELECT max(CAST(created_cdmx AS DATE)) AS d FROM requests").d[0])
    month_end = last_day + pd.offsets.MonthEnd(0)
    footnote = None
    if last_day < month_end:
        last_month = last_day.strftime("%Y-%m")
        partial = f"{last_month} (1–{last_day.day})"
        vol["month"] = vol.month.replace(last_month, partial)
        ovl["month"] = ovl.month.replace(last_month, partial)
        footnote = (f"{last_day:%B %Y} covers {last_day:%b} 1–{last_day.day} only (the data ends "
                    f"{last_day:%b} {last_day.day}), so its volume bar is not a full month.")

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    a1.bar(vol.month, vol.requests, width=0.6, color=BLUE)
    a1.set_title("October 2025: volume explodes")
    a1.set_ylabel("Debit requests")
    a1.yaxis.set_major_formatter(lambda v, _: f"{v / 1000:.0f}k")
    a2.plot(ovl.month, ovl.overlap, color=ORANGE, linewidth=2, marker="o", markersize=8)
    a2.set_title("…and most attempts go out before the previous result is back")
    a2.set_ylabel("Sent while previous pending")
    a2.set_ylim(0, 100)
    pct(a2)
    plt.setp(a2.get_xticklabels(), rotation=45, ha="right")
    save(fig, "06_volume_and_overlap.png", footnote=footnote)
    print(vol.merge(ovl, on="month").tail(6).round(1).to_string(index=False))


# =============================================================================
# Step 11. Chart 7 — net effect of the three stop rules (waterfall + sensitivity)
#   Expect: +204k fees · +16k chargeback handling · −84k given up ≈ +135k net
#           side panel: ≈ +33k at MXN 2 · +135k at MXN 4 · +237k at MXN 6
# =============================================================================
def chart_07_stop_rules_net():
    # 11a. Totals for the debits the rules would have stopped
    df = stop_rule_breakdown()
    stopped = df[df.rule != "not stopped"]
    n_requests = stopped.requests.sum()
    n_chargebacks = stopped.later_reversed.sum()
    given_up = stopped.stuck_mxn.sum()

    # 11b. The three components and the net (base case: COST_ATTEMPT per request)
    fees_saved = n_requests * COST_ATTEMPT
    cb_avoided = n_chargebacks * COST_CHARGEBACK
    net = fees_saved + cb_avoided - given_up
    steps = [("Fees saved", fees_saved),
             ("Chargeback\nhandling avoided", cb_avoided),
             ("Recoveries\ngiven up", -given_up)]

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(13, 5.5), gridspec_kw={"width_ratios": [3, 1.3]})
    k = lambda v: f"{v / 1000:,.0f}k"

    # 11c. Waterfall: each bar starts where the previous one ended
    running = 0
    for i, (label, v) in enumerate(steps):
        bottom = running if v >= 0 else running + v
        ax.bar(i, abs(v), bottom=bottom, width=0.6, color=BLUE if v >= 0 else ORANGE)
        ax.annotate(f"{'+' if v >= 0 else '−'}MXN {k(abs(v))}", (i, bottom + abs(v)),
                    xytext=(0, 4), textcoords="offset points", ha="center")
        running += v
    ax.bar(len(steps), net, width=0.6, color=INK)
    ax.annotate(f"{'+' if net >= 0 else '−'}MXN {k(abs(net))}", (len(steps), max(net, 0)),
                xytext=(0, 4), textcoords="offset points", ha="center", fontweight="bold")
    ax.set_xticks(range(len(steps) + 1), [s[0] for s in steps] + ["Net effect"])
    ax.set_ylim(min(0, net) * 1.15, (fees_saved + cb_avoided) * 1.15)  # headroom for the labels
    ax.axhline(0, color=AXIS, linewidth=1)
    ax.yaxis.set_major_formatter(lambda v, _: k(v))
    ax.set_ylabel("MXN")
    ax.set_title(f"Three stop rules pay for themselves (MXN {COST_ATTEMPT} per request)")

    # 11d. Sensitivity: the same net at MXN 2 / 4 / 6 per request
    costs = [2, 4, 6]
    nets = [n_requests * c + cb_avoided - given_up for c in costs]
    ax2.bar([f"MXN {c}" for c in costs], nets, width=0.6,
            color=[INK if c == COST_ATTEMPT else MUTED for c in costs])
    for i, v in enumerate(nets):
        ax2.annotate(f"{'+' if v >= 0 else '−'}{k(abs(v))}", (i, max(v, 0)),
                     xytext=(0, 4), textcoords="offset points", ha="center")
    ax2.set_ylim(min(0, min(nets)) * 1.15, max(0, max(nets)) * 1.15)  # headroom for the labels
    ax2.axhline(0, color=AXIS, linewidth=1)
    ax2.yaxis.set_major_formatter(lambda v, _: k(v))
    ax2.set_title("Net by cost per request")

    # 11e. Footnote with the honest caveat (saved here, not via save(), to leave room for it)
    fig.text(0.01, 0.01,
             "Recoveries given up is an upper bound: some may still come back through re-consent, "
             "a new account or outreach.", color=MUTED, fontsize=10)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(OUT / "07_stop_rules_net.png", dpi=200)
    plt.close(fig)
    print("saved charts/07_stop_rules_net.png")

    # 11f. Print the breakdown table (for the slide) and the totals
    print(df.to_string(index=False))
    print(f"fees saved {fees_saved:,.0f} | chargeback handling {cb_avoided:,.0f} | "
          f"given up {given_up:,.0f} | net {net:,.0f}")


# =============================================================================
# Step 12. Run all charts
# =============================================================================
if __name__ == "__main__":
    chart_01_decay_by_attempt()
    chart_02_next_by_previous()
    chart_03_day_of_month()
    chart_04_amount()
    chart_05_stop_rules()
    chart_06_volume_and_overlap()
    chart_07_stop_rules_net()
    con.close()
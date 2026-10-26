"""
backtest.py — replay the history through the engine's rules.

Goes through every historical attempt in time order, loan by loan, and asks:
would the rules have allowed it, given only the attempts the policy itself kept?
Each blocked attempt is labeled with the first rule that stopped it.

Two kinds of rules, reported separately:
  - Stop rules end automatic collection (G3, G4, G5, G6, blocked twice).
    Their "MXN given up" is a real upper bound.
  - Timing rules only move an attempt later (G2, G7, G8, cooldowns). The replay
    can't credit money collected later, so their MXN is NOT a loss estimate.

Run from the project root:  python src/backtest.py
Outputs: data/output/backtest_by_rule.csv and charts/08_backtest_stop_rules.png
"""

# =============================================================================
# Step 1. Imports and settings
# =============================================================================
import collections
import datetime as dt
import pathlib

import duckdb
import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import engine  # reuses Config, SEGMENTS, overdue_segment, DB

CFG = engine.Config()
CAP = dict(zip(engine.SEGMENTS, CFG.caps_30d))
STOP_RULES = {"G3 dead account", "G4 revoked", "G5 after chargeback", "G6 never-payer cap", "T2 blocked twice"}
CHARTS = pathlib.Path("charts")

# Same style as charts.py (kept local so this script doesn't open a second DB connection)
SURFACE, INK, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE = "#2a78d6", "#eb6834"
plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": "sans-serif", "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 12, "xtick.color": MUTED, "ytick.color": MUTED, "axes.edgecolor": AXIS,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "axes.grid.axis": "y",
    "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
    "axes.titlesize": 15, "axes.titleweight": "bold", "axes.titlelocation": "left",
})


# =============================================================================
# Step 2. Load every attempt in time order, with when its chargeback happened
# =============================================================================
def load_events():
    con = duckdb.connect(engine.DB, read_only=True)
    rel = con.sql("""
        SELECT a.loan_id, a.created_utc, a.attempt_day, a.n_requests, a.n_success, a.n_chargeback,
               a.net_collected, a.result_utc, a.outcome, a.days_overdue_at_attempt,
               cb.first_chargeback_utc
        FROM attempts a
        LEFT JOIN (SELECT loan_id, created_utc, min(chargeback_utc) AS first_chargeback_utc
                   FROM requests WHERE chargeback_utc IS NOT NULL
                   GROUP BY 1, 2) cb USING (loan_id, created_utc)
        ORDER BY a.loan_id, a.created_utc
    """)
    rows = [dict(zip(rel.columns, r)) for r in rel.fetchall()]
    con.close()
    return rows


# =============================================================================
# Step 3. Replay one loan: state comes only from the attempts the policy kept
# =============================================================================
def replay_loan(events):
    kept_days, cb_times = [], []
    last, n_kept, paid, blocked, last_success = None, 0, False, 0, None
    labels = []
    for e in events:
        t, day = e["created_utc"], e["attempt_day"]
        rule = None
        if last and last["result_utc"] > t:
            rule = "G2 result pending"
        elif last and last["outcome"] == "HARD":
            rule = "G3 dead account"
        elif last and last["outcome"] == "REVOKED":
            rule = "G4 revoked"
        elif any(c <= t for c in cb_times):                      # a chargeback already happened
            rule = "G5 after chargeback"
        elif not paid and n_kept >= CFG.never_payer_cap:
            rule = "G6 never-payer cap"
        elif last and last["outcome"] == "BLOCKED" and blocked >= 2:
            rule = "T2 blocked twice"
        elif last_success and t - last_success < dt.timedelta(days=3):  # ≈ 2 business days
            rule = "G8 recent success"
        elif last and last["outcome"] == "BLOCKED" and (day - last["attempt_day"]).days < CFG.blocked_cooldown_days:
            rule = "T2 blocked cooldown"
        elif last and last["outcome"] == "WAIT" and (day - last["attempt_day"]).days < CFG.wait_cooldown_days:
            rule = "T3 code 11 wait"
        else:
            cap = CAP[engine.overdue_segment(e["days_overdue_at_attempt"])]
            if sum(1 for x in kept_days if day - dt.timedelta(days=30) <= x < day) >= cap:
                rule = "G7 30-day cap"
        labels.append(rule or "kept")
        if rule:
            continue
        kept_days.append(day)                                     # the policy keeps this attempt
        n_kept += 1
        last = e
        if e["n_success"] > 0:
            paid, last_success = True, e["result_utc"]
        if e["outcome"] == "BLOCKED":
            blocked += 1
        if e["first_chargeback_utc"]:
            cb_times.append(e["first_chargeback_utc"])
    return labels


# =============================================================================
# Step 4. Run the replay and summarize by rule
# =============================================================================
def run():
    rows = load_events()
    by_loan = collections.defaultdict(list)
    for r in rows:
        by_loan[r["loan_id"]].append(r)
    for events in by_loan.values():
        for r, label in zip(events, replay_loan(events)):
            r["rule"] = label

    df = pd.DataFrame(rows)
    by_rule = (df.groupby("rule")
                 .agg(attempts=("rule", "size"), requests=("n_requests", "sum"),
                      successes=("n_success", "sum"), chargebacks=("n_chargeback", "sum"),
                      net_mxn=("net_collected", "sum"))
                 .sort_values("requests", ascending=False))
    by_rule["kind"] = ["kept" if r == "kept" else "stop" if r in STOP_RULES else "timing" for r in by_rule.index]
    engine.OUT.mkdir(parents=True, exist_ok=True)
    by_rule.to_csv(engine.OUT / "backtest_by_rule.csv")
    print(by_rule.round(0).to_string())

    # 4a. Totals by kind
    tot = by_rule[["requests", "successes", "chargebacks", "net_mxn"]].sum()
    kinds = by_rule.groupby("kind")[["requests", "successes", "chargebacks", "net_mxn"]].sum()
    stop, kept = kinds.loc["stop"], kinds.loc["kept"]
    print(f"\nStop rules: {stop.requests / tot.requests:.1%} of requests · {stop.chargebacks / tot.chargebacks:.1%} "
          f"of chargebacks · {stop.net_mxn / tot.net_mxn:.1%} of net MXN (upper bound)")
    for c in (2, 4, 6):
        net = stop.requests * c + stop.chargebacks * CFG.cost_chargeback - stop.net_mxn
        print(f"  net effect at MXN {c} per request: {net:+,.0f}")
    print(f"Timing rules move {kinds.loc['timing'].requests:,.0f} requests later (MXN not a loss estimate).")
    print(f"Kept attempts: hit per request {tot.successes / tot.requests:.2%} → {kept.successes / kept.requests:.2%} · "
          f"chargebacks per success {tot.chargebacks / tot.successes:.1%} → {kept.chargebacks / kept.successes:.1%} · "
          f"fees per peso recovered {tot.requests * 4 / tot.net_mxn:.2f} → {kept.requests * 4 / kept.net_mxn:.2f}")

    chart(stop)
    return by_rule


# =============================================================================
# Step 5. Chart 8 — net effect of the stop rules in the replay (waterfall + sensitivity)
# =============================================================================
def chart(stop):
    k = lambda v: f"{v / 1000:,.0f}k"
    fees = stop.requests * CFG.cost_attempt
    cb = stop.chargebacks * CFG.cost_chargeback
    lost = stop.net_mxn
    net = fees + cb - lost
    steps = [("Fees saved", fees), ("Chargeback\nhandling avoided", cb), ("Recoveries\ngiven up", -lost)]

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(13, 5.5), gridspec_kw={"width_ratios": [3, 1.3]})
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
    ax.set_ylim(min(0, net) * 1.15, (fees + cb) * 1.15)
    ax.axhline(0, color=AXIS, linewidth=1)
    ax.yaxis.set_major_formatter(lambda v, _: k(v))
    ax.set_title(f"Replay: the stop rules pay for themselves (MXN {CFG.cost_attempt:.0f} per request)")

    costs = [2, 4, 6]
    nets = [stop.requests * c + cb - lost for c in costs]
    ax2.bar([f"MXN {c}" for c in costs], nets, width=0.6,
            color=[INK if c == CFG.cost_attempt else MUTED for c in costs])
    for i, v in enumerate(nets):
        ax2.annotate(f"{'+' if v >= 0 else '−'}{k(abs(v))}", (i, max(v, 0)),
                     xytext=(0, 4), textcoords="offset points", ha="center")
    ax2.set_ylim(min(0, min(nets)) * 1.15, max(0, max(nets)) * 1.15)
    ax2.axhline(0, color=AXIS, linewidth=1)
    ax2.yaxis.set_major_formatter(lambda v, _: k(v))
    ax2.set_title("Net by cost per request")

    fig.text(0.01, 0.01, "Stop rules only: dead account, revoked, after a chargeback, never-payer cap, blocked twice. "
             "Recoveries given up is an upper bound.", color=MUTED, fontsize=10)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    CHARTS.mkdir(exist_ok=True)
    fig.savefig(CHARTS / "08_backtest_stop_rules.png", dpi=200)
    plt.close(fig)
    print("saved charts/08_backtest_stop_rules.png")


# =============================================================================
# Step 6. Run
# =============================================================================
if __name__ == "__main__":
    run()
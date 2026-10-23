"""
engine.py — Smart Retry decision engine (spec: prep/02-retry-logic-spec.md).

For every loan in the portfolio, decides ATTEMPT / SCHEDULE / HOLD / STOP / NEEDS_ACTION
for the decision day (params.as_of), with date, amount(s), odds, expected value and a reason.

Run from the project root:
    python src/engine.py                 # v1: rules + lookup tables (default)
    python src/engine.py --version v0    # v0: rules only

Outputs (data/output/, ignored by git):
    decisions_<as_of>_<version>.csv      one row per loan
    attempts_<as_of>_<version>.csv       one row per debit to send on the decision day
    needs_action_<as_of>_<version>.csv   loans the lender must act on
"""

# =============================================================================
# Step 1. Imports and paths
# =============================================================================
import argparse
import dataclasses
import datetime as dt
import pathlib
from math import prod

import duckdb
import pandas as pd

DB = "data/belvo.duckdb"
OUT = pathlib.Path("data/output")  # inside data/, so git ignores it


# =============================================================================
# Step 2. Settings (all decided Oct 20–22; see spec §10)
# =============================================================================
@dataclasses.dataclass(frozen=True)
class Config:
    version: str = "v1"                       # v0 = rules only, v1 = rules + lookup tables
    cost_attempt: float = 4.0                 # MXN per debit request
    cost_chargeback: float = 20.0             # MXN handling per chargeback
    never_payer_cap: int = 20                 # G6
    caps_30d: tuple = (8, 6, 4, 3)            # G7: 1-90 / 91-180 / 181-365 / 365+ days overdue
    success_hold_bdays: int = 2               # G8
    blocked_cooldown_days: int = 30           # T2: one retry; blocked twice -> needs action
    wait_cooldown_days: int = 15              # T3: code 11, counted from the latest wait
    horizon_bdays: int = 10                   # T1: business days to consider
    small_share: float = 0.06                 # A2
    medium_share: float = 0.27                # A3
    split_shares: tuple = (0.65, 0.27, 0.06)  # A4
    min_n: int = 30                           # odds fallback threshold


# =============================================================================
# Step 3. Business days (Mexican bank holidays; verify against the CNBV calendar)
# =============================================================================
HOLIDAYS = {dt.date(2025, 11, 17), dt.date(2025, 12, 12), dt.date(2025, 12, 25), dt.date(2026, 1, 1)}


def is_business_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def next_business_days(start: dt.date, n: int) -> list[dt.date]:
    """The first n business days on or after start."""
    days, d = [], start
    while len(days) < n:
        if is_business_day(d):
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def business_days_before(day: dt.date, n: int) -> dt.date:
    """The business day that is n business days before `day`."""
    d, k = day, 0
    while k < n:
        d -= dt.timedelta(days=1)
        if is_business_day(d):
            k += 1
    return d


def as_date(x):
    """DuckDB returns DATE as date and TIMESTAMP as datetime; normalize to date (None stays None)."""
    if x is None:
        return None
    return x.date() if isinstance(x, dt.datetime) else x


# =============================================================================
# Step 4. Buckets (must match 04_lookup.sql exactly)
# =============================================================================
SEGMENTS = ("1-90", "91-180", "181-365", "365+")


def overdue_segment(days: int) -> str:
    if days <= 90:
        return "1-90"
    if days <= 180:
        return "91-180"
    if days <= 365:
        return "181-365"
    return "365+"


def attempt_bucket(n: int) -> str:
    if n == 1:
        return "1"
    if n <= 5:
        return "2-5"
    if n <= 20:
        return "6-20"
    return "21+"


def gap_bucket(days) -> str:
    if days is None:
        return "first"
    if days < 4:
        return "<4"
    if days < 10:
        return "4-9"
    return "10+"


def amount_bucket(a: float) -> str:
    if a < 100:
        return "<100"
    if a < 500:
        return "100-499"
    if a < 2000:
        return "500-1999"
    return "2000+"


def in_payday_window(d: dt.date) -> bool:
    return 12 <= d.day <= 15 or d.day >= 28 or d.day <= 2


# =============================================================================
# Step 5. Odds book: lookup tables loaded once into memory (same fallback as src/odds.py)
# =============================================================================
class OddsBook:
    KEY = ["prev_outcome", "ever_paid_before", "overdue_segment", "attempt_bucket",
           "payday_window", "gap_bucket", "amount_bucket"]

    def __init__(self, con, min_n: int):
        self.min_n = min_n
        self.levels = []
        for table, cols in (("odds_l3", self.KEY), ("odds_l2", self.KEY[:6]), ("odds_l1", self.KEY[:3])):
            rows = con.sql(f"SELECT {', '.join(cols)}, n, p_success FROM {table}").fetchall()
            self.levels.append((table, len(cols), {tuple(r[:-2]): (r[-1], r[-2]) for r in rows}))
        self.p0, self.n0 = con.execute("SELECT p_success, n FROM odds_l0").fetchall()[0]
        self.cb2 = {(b, a): (p, n) for b, a, n, p in con.sql("SELECT bank, amount_bucket, n, p_cb FROM cb_l2").fetchall()}
        self.cb1 = {b: (p, n) for b, n, p in con.sql("SELECT bank, n, p_cb FROM cb_l1").fetchall()}
        self.cb0 = con.execute("SELECT p_cb FROM cb_l0").fetchall()[0][0]

    def p_success(self, key: dict):
        """Most detailed level with at least min_n past debits → (p, table used, n)."""
        full = tuple(key[c] for c in self.KEY)
        for table, k, lookup in self.levels:
            hit = lookup.get(full[:k])
            if hit and hit[1] >= self.min_n:
                return hit[0], table, hit[1]
        return self.p0, "odds_l0", self.n0

    def p_chargeback(self, bank: str, bucket: str) -> float:
        hit = self.cb2.get((bank, bucket))
        if hit and hit[1] >= self.min_n:
            return hit[0]
        hit = self.cb1.get(bank)
        if hit and hit[1] >= self.min_n:
            return hit[0]
        return self.cb0


# =============================================================================
# Step 6. Load inputs: decision day, one row per loan, recent attempt days
# =============================================================================
def load(con):
    as_of = con.execute("SELECT as_of FROM params").fetchall()[0][0]
    rel = con.sql("SELECT * FROM features ORDER BY loan_id")
    loans = [dict(zip(rel.columns, r)) for r in rel.fetchall()]
    recent = dict(con.sql("""
        SELECT loan_id, list(attempt_day ORDER BY attempt_day)
        FROM attempts
        WHERE attempt_day >= (SELECT as_of FROM params) - INTERVAL 30 DAY
          AND attempt_day <  (SELECT as_of FROM params)
        GROUP BY 1
    """).fetchall())
    return as_of, loans, recent


# =============================================================================
# Step 7. One decision row (same columns for every outcome)
# =============================================================================
def decision(f, kind, rule, reason_code, reason_text, day=None, option=None,
             amounts=(), p=None, pcb=None, ev=None, level=None):
    return {
        "loan_id": f["loan_id"], "decision": kind, "attempt_date": day,
        "option": option, "amounts": list(amounts), "n_debits": len(amounts),
        "total_amount": round(sum(amounts), 2),
        "p_success": p, "p_chargeback": pcb, "expected_value": ev, "odds_level": level,
        "rule_fired": rule, "reason_code": reason_code, "reason_text": reason_text,
        "bank": f["bank"], "total_amount_outstanding": f["total_amount_outstanding"],
        "days_overdue": f["days_overdue_as_of"], "last_outcome": f["last_outcome"] or "FIRST",
    }


# =============================================================================
# Step 8. Guardrails (spec §4), in order; the first one that fires decides
# =============================================================================
def guardrails(f, cfg, as_of):
    last = f["last_outcome"]
    if f["total_amount_outstanding"] <= 0:
        return decision(f, "STOP", "G1", "NOTHING_OWED", "Nothing owed in today's file.")
    if f["has_pending"]:
        return decision(f, "HOLD", "G2", "RESULT_PENDING", "The previous attempt's result isn't back yet.")
    if last == "HARD":
        return decision(f, "NEEDS_ACTION", "G3", "NEW_ACCOUNT",
                        "Dead account (code 1, 3 or 6). Ask the lender for a new account.")
    if last == "REVOKED":
        return decision(f, "NEEDS_ACTION", "G4", "RECONSENT",
                        "Authorization revoked (code 8 or 10). Ask the lender for re-consent.")
    if f["chargeback_count"] > 0:
        return decision(f, "STOP", "G5", "CHARGEBACK",
                        f"{f['chargeback_count']} chargeback(s) on this loan. Stop auto-attempts; send to outreach.")
    if not f["ever_paid"] and f["attempts_total"] >= cfg.never_payer_cap:
        return decision(f, "STOP", "G6", "NEVER_PAYER",
                        f"{f['attempts_total']} attempts and never paid. Stop auto-attempts; send to outreach.")
    if last == "BLOCKED" and f["blocked_count"] >= 2:
        return decision(f, "NEEDS_ACTION", "T2", "BLOCKED_AGAIN",
                        f"Account blocked {f['blocked_count']} times (code 2). Ask the lender to check the account.")
    last_success = as_date(f["last_success_result_utc"])
    if last_success and last_success >= business_days_before(as_of, cfg.success_hold_bdays):
        return decision(f, "HOLD", "G8", "RECENT_SUCCESS",
                        f"A debit succeeded on {last_success}. Hold until the lender's file reflects it.")
    if last == "UNKNOWN":
        return decision(f, "HOLD", "G9", "UNKNOWN_CODE", "Unknown failure code. Hold until ops maps it.")
    return None


# =============================================================================
# Step 9. Candidate days (spec §5): cooldowns, horizon, 30-day cap (G7)
# =============================================================================
def earliest_day(f, cfg, as_of):
    """Decision day, pushed later by the blocked (T2) or code 11 (T3) cooldown."""
    if f["last_outcome"] == "BLOCKED":
        return max(as_of, as_date(f["last_blocked_day"]) + dt.timedelta(days=cfg.blocked_cooldown_days)), "blocked cooldown"
    if f["last_outcome"] == "WAIT":
        return max(as_of, as_date(f["last_wait_day"]) + dt.timedelta(days=cfg.wait_cooldown_days)), "code 11 wait"
    return as_of, None


def under_cap(f, cfg, day, recent_days) -> bool:
    cap = dict(zip(SEGMENTS, cfg.caps_30d))[overdue_segment((day - f["overdue_at"]).days)]
    used = sum(1 for x in recent_days if day - dt.timedelta(days=30) <= x < day)
    return used < cap


def candidate_days(f, cfg, as_of, recent_days):
    start, why = earliest_day(f, cfg, as_of)
    window = next_business_days(start, cfg.horizon_bdays)
    return [d for d in window if under_cap(f, cfg, d, recent_days)], window[0], why


def next_free_day(f, cfg, start, recent_days):
    """First business day the 30-day cap allows (attempts age out within 30 days)."""
    return next((d for d in next_business_days(start, 60) if under_cap(f, cfg, d, recent_days)), None)


# =============================================================================
# Step 10. Candidate amounts (spec §5): A1–A4, every debit ≤ what's owed
# =============================================================================
def candidate_amounts(f, cfg):
    owed = f["total_amount_outstanding"]
    opts = []
    if f["ever_paid"] and f["last_success_amount"]:
        opts.append(("A1", "last successful amount", [round(min(f["last_success_amount"], owed), 2)]))
    opts.append(("A2", f"{cfg.small_share:.0%} of the balance", [round(owed * cfg.small_share, 2)]))
    opts.append(("A3", f"{cfg.medium_share:.0%} of the balance", [round(owed * cfg.medium_share, 2)]))
    opts.append(("A4", "65/27/6 split of the balance", [round(owed * s, 2) for s in cfg.split_shares]))
    return [(c, n, [a for a in d if a >= 1]) for c, n, d in opts if any(a >= 1 for a in d)]


# =============================================================================
# Step 11. Odds and expected value for one option (spec §6)
# =============================================================================
def odds_key(f, day, amount):
    last_day = as_date(f["last_attempt_day"])
    return {
        "prev_outcome": f["last_outcome"] or "FIRST",
        "ever_paid_before": bool(f["ever_paid"]),
        "overdue_segment": overdue_segment((day - f["overdue_at"]).days),
        "attempt_bucket": attempt_bucket(f["attempts_total"] + 1),
        "payday_window": in_payday_window(day),
        "gap_bucket": gap_bucket((day - last_day).days if last_day else None),
        "amount_bucket": amount_bucket(amount),
    }


def score(f, day, debits, odds, cfg):
    """Expected net MXN of sending these debits on this day. Split pieces are treated as independent."""
    ev, parts = 0.0, []
    for a in debits:
        p, level, n = odds.p_success(odds_key(f, day, a))
        pcb = odds.p_chargeback(f["bank"], amount_bucket(a))
        ev += p * (a * (1 - pcb) - pcb * cfg.cost_chargeback) - cfg.cost_attempt
        parts.append({"amount": a, "p": p, "p_cb": pcb, "level": level, "n": n})
    return ev, parts


# =============================================================================
# Step 12. Decide one loan: guardrails → days → amounts → pick (v0 rules / v1 EV)
# =============================================================================
def decide(f, ctx):
    cfg, as_of, odds, recent = ctx["cfg"], ctx["as_of"], ctx["odds"], ctx["recent"]

    # 12a. Guardrails
    hit = guardrails(f, cfg, as_of)
    if hit:
        return hit

    # 12b. Allowed days (G7 cap); if none in the horizon, schedule the first free day
    recent_days = recent.get(f["loan_id"], [])
    days, first_day, why = candidate_days(f, cfg, as_of, recent_days)
    if not days:
        free = next_free_day(f, cfg, first_day, recent_days)
        return decision(f, "SCHEDULE", "G7", "CAP_30D",
                        f"30-day attempt cap reached for this days-overdue segment. Next allowed day: {free}.", day=free)

    # 12c. Pick the day and amount
    options = candidate_amounts(f, cfg)
    if cfg.version == "v0":
        day = next((d for d in days if in_payday_window(d)), days[0])  # first payday-window day
        code, name, debits = options[0]                                  # A1 if ever paid, else A2
        ev, parts = score(f, day, debits, odds, cfg)                     # for reporting only
    else:
        best = None
        for d in days:
            for c, n, deb in options:
                e, pts = score(f, d, deb, odds, cfg)
                if best is None or e > best[0]:
                    best = (e, d, c, n, deb, pts)
        ev, day, code, name, debits, parts = best
        if ev <= 0:
            return decision(f, "STOP", "EV", "NOT_WORTH_IT",
                            f"No allowed day or amount has positive expected value (best MXN {ev:.1f}). Send to outreach.",
                            ev=round(ev, 2))

    # 12d. Attempt today, or schedule a later day; explain why
    kind = "ATTEMPT" if day == as_of else "SCHEDULE"
    rule = "T2" if why == "blocked cooldown" else "T3" if why == "code 11 wait" else "T1"
    p_any = 1 - prod(1 - x["p"] for x in parts)  # chance at least one debit succeeds
    lead = max(parts, key=lambda x: x["amount"])
    text = (f"Last result: {f['last_outcome'] or 'first attempt'}. {name.capitalize()} on {day:%a %b %d}"
            f"{' (payday window)' if in_payday_window(day) else ''}{f', after the {why}' if why else ''}: "
            f"{lead['p']:.1%} odds per debit ({lead['level']}, n={lead['n']:,}), expected value MXN {ev:.1f}.")
    return decision(f, kind, rule, f"BEST_{code}", text, day=day, option=code, amounts=debits,
                    p=round(p_any, 4), pcb=round(max(x["p_cb"] for x in parts), 4),
                    ev=round(ev, 2), level=lead["level"])


# =============================================================================
# Step 13. Write the three output files
# =============================================================================
def write_outputs(df, as_of, cfg):
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"{as_of}_{cfg.version}"
    out = df.copy()
    out["amounts"] = out["amounts"].apply(lambda xs: ";".join(f"{a:.2f}" for a in xs))
    out.to_csv(OUT / f"decisions_{tag}.csv", index=False)

    today = df[df.decision == "ATTEMPT"].explode("amounts").rename(columns={"amounts": "amount"})
    today[["loan_id", "attempt_date", "amount", "option", "reason_text"]].to_csv(OUT / f"attempts_{tag}.csv", index=False)

    df[df.decision == "NEEDS_ACTION"][["loan_id", "bank", "rule_fired", "reason_code", "reason_text"]] \
        .to_csv(OUT / f"needs_action_{tag}.csv", index=False)
    print(f"wrote data/output/*_{tag}.csv")


# =============================================================================
# Step 14. Print a summary
# =============================================================================
def summarize(df, as_of, cfg):
    print(f"\nDecisions for {as_of} ({cfg.version}): {len(df):,} loans")
    print(df.groupby(["decision", "rule_fired"]).size().rename("loans").to_string())
    today = df[df.decision == "ATTEMPT"]
    print(f"\nToday: {len(today):,} loans · {int(today.n_debits.sum()):,} debits · "
          f"MXN {today.total_amount.sum():,.0f} requested · expected value MXN {today.expected_value.sum():,.0f}")
    print(df[df.decision.isin(["ATTEMPT", "SCHEDULE"])].groupby("option").size().rename("loans").to_string())


# =============================================================================
# Step 15. Run
# =============================================================================
def run(cfg: Config):
    con = duckdb.connect(DB, read_only=True)
    as_of, loans, recent = load(con)
    odds = OddsBook(con, cfg.min_n)
    con.close()
    ctx = {"cfg": cfg, "as_of": as_of, "odds": odds, "recent": recent}
    df = pd.DataFrame([decide(f, ctx) for f in loans])
    write_outputs(df, as_of, cfg)
    summarize(df, as_of, cfg)
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smart Retry decision engine")
    parser.add_argument("--version", choices=["v0", "v1"], default="v1")
    run(Config(version=parser.parse_args().version))
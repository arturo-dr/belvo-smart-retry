"""
Edge-case tests for src/engine.py (spec §4, §5, §12).

Each test builds a small hand-made loan, runs one decision with a fake odds
book (fixed odds, no database), and checks the rule that fired.

Run from the project root:  python -m pytest -q
"""

# =============================================================================
# Step 1. Imports
# =============================================================================
import datetime as dt
import pathlib

import pandas as pd
import pytest

import engine

AS_OF = dt.date(2025, 11, 12)  # Wednesday, the decision day


# =============================================================================
# Step 2. Test helpers: a default loan, fake odds, and a context
# =============================================================================
def loan(**overrides):
    """A normal loan: 91-180 days overdue, paid once, last result insufficient funds."""
    base = dict(
        loan_id="L1", bank="mx_banorte", loan_amount=1000.0, total_amount_outstanding=3000.0,
        overdue_at=dt.date(2025, 6, 1), days_overdue_as_of=164, overdue_segment="91-180",
        attempts_total=5, attempts_30d=1, last_attempt_day=dt.date(2025, 11, 3),
        days_since_last_attempt=9, last_outcome="FUNDS", last_result_utc=dt.datetime(2025, 11, 5, 9),
        ever_paid=True, success_count=1, last_success_day=dt.date(2025, 9, 15),
        last_success_amount=500.0, last_success_result_utc=dt.datetime(2025, 9, 17, 9),
        chargeback_count=0, last_chargeback_utc=None,
        blocked_count=0, last_blocked_day=None, wait_count=0, last_wait_day=None,
        hard_count=0, revoked_count=0, has_pending=False, success_days_of_month=[15], as_of=AS_OF,
    )
    base.update(overrides)
    return base


class FakeOdds:
    """Fixed odds so results are predictable (no database needed)."""

    def __init__(self, p=0.05, pcb=0.10):
        self.p, self.pcb = p, pcb

    def p_success(self, key):
        return self.p, "fake", 1000

    def p_chargeback(self, bank, bucket):
        return self.pcb


def ctx(version="v1", as_of=AS_OF, p=0.05, recent=None):
    return {"cfg": engine.Config(version=version), "as_of": as_of,
            "odds": FakeOdds(p=p), "recent": recent or {}}


def days(*md):
    """Shorthand for 2025 dates: days((10, 28), (11, 3)) → [Oct 28, Nov 3]."""
    return [dt.date(2025, m, d) for m, d in md]


# =============================================================================
# Step 3. Guardrails (G1–G9 and the blocked-twice rule)
# =============================================================================
def test_g1_nothing_owed_stops():
    d = engine.decide(loan(total_amount_outstanding=0.0), ctx())
    assert (d["decision"], d["rule_fired"]) == ("STOP", "G1")


def test_g2_pending_result_holds():
    d = engine.decide(loan(has_pending=True), ctx())
    assert (d["decision"], d["rule_fired"]) == ("HOLD", "G2")


def test_g3_dead_account_needs_new_account():
    d = engine.decide(loan(last_outcome="HARD"), ctx())
    assert (d["decision"], d["rule_fired"]) == ("NEEDS_ACTION", "G3")


def test_g4_revoked_needs_reconsent():
    d = engine.decide(loan(last_outcome="REVOKED"), ctx())
    assert (d["decision"], d["rule_fired"]) == ("NEEDS_ACTION", "G4")


def test_g5_any_chargeback_stops():
    d = engine.decide(loan(chargeback_count=1), ctx())
    assert (d["decision"], d["rule_fired"]) == ("STOP", "G5")


def test_g6_never_payer_stops_at_20_attempts():
    never = dict(ever_paid=False, success_count=0, last_success_day=None,
                 last_success_amount=None, last_success_result_utc=None)
    assert engine.decide(loan(attempts_total=20, **never), ctx())["rule_fired"] == "G6"
    assert engine.decide(loan(attempts_total=19, **never), ctx())["rule_fired"] != "G6"


def test_blocked_twice_needs_action():
    d = engine.decide(loan(last_outcome="BLOCKED", blocked_count=2, last_blocked_day=dt.date(2025, 11, 3)), ctx())
    assert (d["decision"], d["rule_fired"]) == ("NEEDS_ACTION", "T2")


def test_g8_recent_success_holds_to_avoid_double_charge():
    d = engine.decide(loan(last_success_result_utc=dt.datetime(2025, 11, 10, 9)), ctx())
    assert (d["decision"], d["rule_fired"]) == ("HOLD", "G8")
    # A success that came back before Mon Nov 10 is old enough: no hold
    assert engine.decide(loan(last_success_result_utc=dt.datetime(2025, 11, 7, 9)), ctx())["rule_fired"] != "G8"


def test_g9_unknown_code_holds():
    d = engine.decide(loan(last_outcome="UNKNOWN"), ctx())
    assert (d["decision"], d["rule_fired"]) == ("HOLD", "G9")


# =============================================================================
# Step 4. Cooldowns (blocked once → 30 days; code 11 → 15 days from the latest wait)
# =============================================================================
def test_blocked_once_waits_30_days():
    d = engine.decide(loan(last_outcome="BLOCKED", blocked_count=1, last_blocked_day=dt.date(2025, 11, 3)), ctx())
    assert d["rule_fired"] == "T2" and d["decision"] == "SCHEDULE"
    assert d["attempt_date"] >= dt.date(2025, 12, 3)


def test_code_11_waits_15_days_from_latest_wait():
    d = engine.decide(loan(last_outcome="WAIT", wait_count=3, last_wait_day=dt.date(2025, 11, 5)), ctx())
    assert d["rule_fired"] == "T3" and d["attempt_date"] >= dt.date(2025, 11, 20)


# =============================================================================
# Step 5. 30-day cap (G7): 8 per 30 days in the 1-90 segment
# =============================================================================
def test_cap_pushes_the_day_until_attempts_age_out():
    recent = {"L1": days((10, 20), (10, 21), (10, 22), (10, 23), (10, 24), (10, 27), (10, 28), (10, 29))}
    d = engine.decide(loan(overdue_at=dt.date(2025, 10, 1)), ctx(recent=recent))
    assert d["decision"] in ("ATTEMPT", "SCHEDULE") and d["attempt_date"] >= dt.date(2025, 11, 20)


def test_cap_full_for_whole_horizon_schedules_first_free_day():
    recent = {"L1": days((10, 28), (10, 29), (10, 30), (10, 31), (11, 3), (11, 4), (11, 5), (11, 6))}
    d = engine.decide(loan(overdue_at=dt.date(2025, 10, 1)), ctx(recent=recent))
    assert (d["decision"], d["rule_fired"]) == ("SCHEDULE", "G7")
    assert d["attempt_date"] == dt.date(2025, 11, 28)  # Oct 28 ages out on Nov 28


# =============================================================================
# Step 6. Calendar: weekends and bank holidays are never attempt days
# =============================================================================
def test_business_days_skip_weekend_and_holiday():
    assert engine.next_business_days(dt.date(2025, 11, 15), 2) == days((11, 18), (11, 19))  # Sat → skip Mon 17


def test_decision_on_a_saturday_never_lands_on_a_non_business_day():
    d = engine.decide(loan(), ctx(as_of=dt.date(2025, 11, 15)))
    assert d["attempt_date"].weekday() < 5 and d["attempt_date"] != dt.date(2025, 11, 17)


# =============================================================================
# Step 7. Amounts: never above what's owed; tiny balances aren't worth it
# =============================================================================
def test_amounts_never_exceed_outstanding():
    f = loan(total_amount_outstanding=1234.5, last_success_amount=5000.0)
    for code, name, debits in engine.candidate_amounts(f, engine.Config()):
        assert all(a <= 1234.5 for a in debits) and sum(debits) <= 1234.5


def test_tiny_balance_stops_on_negative_expected_value():
    d = engine.decide(loan(total_amount_outstanding=30.0), ctx())
    assert (d["decision"], d["rule_fired"]) == ("STOP", "EV")


# =============================================================================
# Step 8. Versions and first attempts
# =============================================================================
def test_v0_prefers_the_first_payday_window_day():
    d = engine.decide(loan(), ctx(version="v0", as_of=dt.date(2025, 11, 18)))
    assert d["attempt_date"] == dt.date(2025, 11, 28)  # next window day after Nov 18


def test_loan_with_no_history_gets_a_first_attempt():
    f = loan(attempts_total=0, attempts_30d=0, last_attempt_day=None, days_since_last_attempt=None,
             last_outcome=None, last_result_utc=None, ever_paid=False, success_count=0,
             last_success_day=None, last_success_amount=None, last_success_result_utc=None)
    d = engine.decide(f, ctx())
    assert d["decision"] in ("ATTEMPT", "SCHEDULE") and d["last_outcome"] == "FIRST"


# =============================================================================
# Step 9. Integration: the real run matches the expected guardrail counts
#   (skipped if the decisions file hasn't been generated yet)
# =============================================================================
DECISIONS = pathlib.Path("data/output/decisions_2025-11-12_v1.csv")


@pytest.mark.skipif(not DECISIONS.exists(), reason="run python src/engine.py first")
def test_real_run_matches_expected_guardrail_counts():
    counts = pd.read_csv(DECISIONS).rule_fired.value_counts()
    expected = {"G3": 259, "G4": 236, "G5": 375, "G6": 1148, "T2": 18, "G8": 13}
    # T2 also covers "blocked once, scheduled after the cooldown", so only check it as a minimum
    assert counts.get("T2", 0) >= expected.pop("T2")
    for rule, n in expected.items():
        assert counts.get(rule, 0) == n, rule
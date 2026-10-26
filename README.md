# Belvo Smart Retry: MVP

A daily decision engine for Belvo's Managed Collections. Every morning, for each overdue loan, it decides **whether, when and how much** to debit, explains the decision in plain words, and hands what it can't solve back to the lender.

**Demo:** [belvo-arturo.streamlit.app](https://belvo-arturo.streamlit.app/)

## The problem, from the data
253,006 debit requests on 3,062 overdue loans (Dec 2023 – Nov 2025):
- **83 requests per loan, 97.6% failed.** 23% of the successful debits were charged back later.
- **47% of loans never paid, yet got 49% of all requests.**
- **Fees eat the recovery:** at MXN 4 per request, fees (MXN 1.01M) equal 85% of the MXN 1.19M recovered net of chargebacks.
- **Two clocks out of sync:** the lender's file updates daily, but bank results take 2–4 days. Since Oct 2025, 87–97% of attempts went out before the previous result was back.

## Approach
**Principle: the daily snapshot updates the loan's state; it never triggers an attempt by itself.**

1. **Guardrails (rules, always win):** wait for the previous result; stop dead accounts (codes 1, 3, 6) and revoked mandates (8, 10) and send them back to the lender; stop after a chargeback; stop never-payers after 20 tries; cap tries per 30 days (8 / 6 / 4 / 3 by days overdue).
2. **Candidate days and amounts (rules):** the next 10 business days (30 days after a block, 15 after code 11); the last amount paid, 6% or 27% of the balance, or a 65 / 27 / 6 split.
3. **Odds (lookup tables):** the chance of success and of chargeback for each option, taken from 193,629 past attempts with a similar history (previous result, attempt number, payday window, gap, days overdue, amount relative to the loan).
4. **Pick by expected value:** for each debit, `p × (amount × (1 − p_cb) − p_cb × 20) − 4`. If no option is worth its fee, the loan goes to outreach.

Two versions: **v0** = rules only, **v1** = rules + tables. Tables come first because they are explainable, quick to audit and robust on thin data; a trained model can replace step 3 later without touching any rule.

## Results
- **Replay of the history:** stop rules remove 55% of requests and 43% of chargebacks, for at most 20% of net MXN. The net effect is **+MXN 329k at MXN 4 per request** (+51k at MXN 2). On the attempts the rules keep, the hit rate rises from 2.40% to 4.25%, and fees per peso recovered fall from 0.85 to 0.48.
- **Decision day, Nov 12, 2025 (v1):** 81 loans attempted with 243 debits (vs. ~4,400 requests a day in early November), 921 scheduled for later, 513 sent back to the lender, 1,534 stopped.
- **Limits:** timing rules (wait for results, caps, cooldowns) only move attempts later, and the replay can't price that. v1's preference for the 3-way split is probably overstated, because the tables score each piece as independent. Both points need a live test: 80/20 by loan, north star = net MXN per loan per 30 days.

## Repo and how to run
| Path | What it is |
|---|---|
| `data/*.csv` | The challenge dataset (portfolio snapshot + payment requests) |
| `src/sql/01–04_*.sql`, `src/load.py` | DuckDB feature layer: attempts, per-loan features, lookup tables |
| `src/odds.py`, `src/engine.py` | Odds lookup with fallback groups; the decision engine |
| `src/backtest.py`, `src/charts.py` | History replay and the deck charts (`charts/`) |
| `streamlit_app.py` | Operator console: Today, Queue, Loan detail, Needs action |
| `tests/test_engine.py` | 20 edge-case tests (in-flight results, double charges, codes, caps…) |

```bash
pip install -r requirements.txt
python src/load.py            # build data/belvo.duckdb
python src/engine.py          # v1 decisions (add --version v0 for rules only)
streamlit run streamlit_app.py
python -m pytest -q
```

Assumptions: MXN 4 per request (2 / 6 as sensitivity) and MXN 20 to handle each chargeback.
